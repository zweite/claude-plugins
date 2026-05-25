#!/usr/bin/env python3
"""
alidocs.py — download a DingTalk / alidocs document (or a whole folder) from a
shared link.

Thin wrapper over dl_alidocs.py: give it a link and it mirrors that subtree
to disk — a single doc downloads just that doc, a folder downloads everything
under it, and a knowledge-base overview link
(https://alidocs.dingtalk.com/i/spaces/<spaceId>/overview) downloads the whole
space. Online docs export to .docx/.xlsx via Playwright when it's installed,
otherwise they're saved as .url shortcuts.

Credentials and the default output directory come from env or
~/.taku/alidocs.json (legacy ~/.alidocs/config.json is still read). The config
may be flat or hold a `profiles` map for multiple accounts:

  {
    "cookie": "<full Cookie header from a logged-in browser>",
    "default_out_dir": "~/Downloads/alidocs",
    "use_playwright": true
  }

Usage:
  alidocs.py fetch --url <doc-or-folder-link> [--out DIR] [--no-playwright]
  alidocs.py check --url <link>          # verify cookie + resolve the node
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import dl_alidocs as dl  # same scripts/ directory


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


# ── config (unified ~/.taku/<tool>.json, legacy fallback, profiles) ────────

def _config_path(tool: str, legacy: str) -> str:
    base = os.environ.get("TAKU_DIR", "").strip() or os.path.expanduser("~/.taku")
    unified = os.path.join(base, f"{tool}.json")
    if os.path.exists(unified):
        return unified
    if os.path.exists(os.path.expanduser(legacy)):
        return os.path.expanduser(legacy)
    return unified


CONFIG_PATH = _config_path("alidocs", "~/.alidocs/config.json")


def _load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError) as e:
        die(f"failed to read {CONFIG_PATH}: {e}")
    if not isinstance(blob, dict):
        die(f"{CONFIG_PATH}: expected a JSON object")
    return blob


def _early_profile() -> str:
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return os.environ.get("ALIDOCS_PROFILE", "").strip()


def _resolve_profile(config: dict[str, Any], selected: str) -> tuple[dict[str, Any], str]:
    profiles = config.get("profiles")
    if isinstance(profiles, dict) and profiles:
        base = {k: v for k, v in config.items() if k not in ("profiles", "default_profile")}
        name = selected or config.get("default_profile", "")
        if not name:
            name = next(iter(profiles)) if len(profiles) == 1 else die(
                "multiple profiles defined; pass --profile or set default_profile. "
                f"Available: {', '.join(sorted(profiles))}") or ""
        if name not in profiles:
            die(f"profile {name!r} not found in {CONFIG_PATH}. Available: {', '.join(sorted(profiles))}")
        merged = dict(base)
        merged.update(profiles[name])
        return merged, name
    return dict(config), (selected or "default")


_CONFIG = _load_config()
_PROFILE, PROFILE_NAME = _resolve_profile(_CONFIG, _early_profile())


def _setting(env_key: str, key: str, default: str = "") -> str:
    v = os.environ.get(env_key, "").strip()
    if v:
        return v
    cv = _PROFILE.get(key)
    return str(cv).strip() if cv not in (None, "") else default


def _resolve_cookie() -> str:
    inline = os.environ.get("ALIDOCS_COOKIE", "") or _PROFILE.get("cookie", "")
    if inline:
        return inline.strip()
    cf = _PROFILE.get("cookie_file")
    if cf:
        return dl.load_cookie(os.path.expanduser(str(cf)))
    die(f"no cookie configured. Put `cookie` (or `cookie_file`) in {CONFIG_PATH} "
        f"(profile {PROFILE_NAME!r}).")


# ── link parsing ───────────────────────────────────────────────────────────

def parse_link(url: str) -> tuple[str | None, str | None]:
    """Extract (dentryUuid|None, spaceId|None) from an alidocs link.
    Recognised forms:
      - /i/nodes/<uuid>[?spaceId=...]                  → (uuid, space?)
      - ?dentryUuid=<uuid> or ?nodeId=<uuid>           → (uuid, space?)
      - /i/spaces/<spaceId>/overview                   → (None, spaceId)
        (knowledge-base root page; root dentry is discovered later)
    """
    u = (url or "").strip()
    if not u:
        die("empty --url")
    parsed = urlparse(u)
    qs = parse_qs(parsed.query)
    uuid = ""
    # explicit query param wins
    for k in ("dentryUuid", "nodeId"):
        if qs.get(k):
            uuid = qs[k][0]
            break
    if not uuid:
        m = re.search(r"/nodes/([^/?#]+)", parsed.path)
        if m:
            uuid = m.group(1)
    space = None
    for k in ("spaceId", "workspaceId"):
        if qs.get(k):
            space = qs[k][0]
            break
    if not space:
        m = re.search(r"/spaces/([^/?#]+)", parsed.path)
        if m:
            space = m.group(1)
    if not uuid and not space:
        die(f"could not extract a dentry UUID or space ID from {url!r}. Expected "
            "an alidocs link like https://alidocs.dingtalk.com/i/nodes/<uuid> "
            "or https://alidocs.dingtalk.com/i/spaces/<spaceId>/overview")
    return (uuid or None), space


def _decide_playwright(no_playwright_flag: bool) -> bool:
    if no_playwright_flag:
        return False
    cv = _PROFILE.get("use_playwright")
    if isinstance(cv, bool):
        return cv
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _die_login_expired() -> None:
    die("not logged in — the cookie looks expired. Refresh it in "
        f"{CONFIG_PATH}: open alidocs.dingtalk.com in a logged-in browser, "
        "copy the full Cookie request header (DevTools → Network → any request "
        "→ Request Headers → Cookie), and set it as `cookie`.")


def _looks_like_login_page(text: str) -> bool:
    return "统一身份认证" in text or ("<title>" in text and "登录" in text)


def _resolve_node(session, uuid: str | None, space: str | None) -> tuple[str, str, str | None]:
    """Returns (space_id, dentry_uuid, root_name_override).
    `root_name_override` is the knowledge-base display name when the link is a
    space overview (so the top-level folder mirrors the space's UI name instead
    of the internal "#ROOT#" sentinel); None for /i/nodes/<uuid> links."""
    space = space or (_PROFILE.get("space_id") or None)
    if uuid and space:
        return str(space), uuid, None
    if uuid:
        r = session.get(f"{dl.BASE}/i/nodes/{uuid}", timeout=20)
        m = re.search(r'"spaceId":"([^"]+)"', r.text)
        if not m:
            if _looks_like_login_page(r.text):
                _die_login_expired()
            die("could not discover spaceId from the page; add `space_id` to the config "
                "or use a link that includes spaceId.")
        return m.group(1), uuid, None
    # uuid missing — must have space; fetch the root dentry from overview HTML
    if not space:
        die("link has neither a dentry UUID nor a space ID")
    r = session.get(f"{dl.BASE}/i/spaces/{space}/overview", timeout=20)
    m = re.search(r'"rootDentryUuid":"([^"]+)"', r.text)
    if not m:
        if _looks_like_login_page(r.text):
            _die_login_expired()
        die(f"could not discover rootDentryUuid for space {space!r}; the space "
            "may not exist or the cookie may lack access.")
    root_uuid = m.group(1)
    # Pull the space's display name out of the same JSON object — `name` sits
    # near `rootDentryUuid` and the trailing `"id":"<space>"` confirms it's
    # the space record (not a child dentry).
    nm = re.search(
        r'"rootDentryUuid":"' + re.escape(root_uuid)
        + r'"[^{}]*?"name":"([^"\\]+)"[^{}]*?"id":"' + re.escape(str(space)) + r'"',
        r.text,
    )
    space_name = nm.group(1) if nm else None
    return str(space), root_uuid, space_name


# ── commands ───────────────────────────────────────────────────────────────

def cmd_check(args: argparse.Namespace) -> None:
    cookie = _resolve_cookie()
    a_token = _setting("ALIDOCS_A_TOKEN", "a_token") or None
    uuid, space = parse_link(args.url)
    session = dl.make_session(cookie, a_token)
    space, uuid, name_override = _resolve_node(session, uuid, space)
    root = dl.fetch_info(session, uuid, space)
    if name_override:
        root.name = name_override
    print(json.dumps({
        "ok": True, "profile": PROFILE_NAME, "space_id": space,
        "dentry_uuid": uuid, "name": root.name, "type": root.dentry_type,
        "is_folder": root.is_folder, "has_children": root.has_children,
    }, ensure_ascii=False, indent=2))


def cmd_fetch(args: argparse.Namespace) -> None:
    cookie = _resolve_cookie()
    a_token = _setting("ALIDOCS_A_TOKEN", "a_token") or None
    out_dir = (args.out or _setting("ALIDOCS_OUT_DIR", "default_out_dir", "~/Downloads/alidocs"))
    out_root = Path(os.path.expanduser(out_dir)).resolve()

    uuid, space = parse_link(args.url)
    session = dl.make_session(cookie, a_token)
    space, uuid, name_override = _resolve_node(session, uuid, space)
    root = dl.fetch_info(session, uuid, space)
    if name_override:
        root.name = name_override
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"[root]   {root.name} ({root.dentry_type}) → {out_root}", flush=True)

    exporter = None
    if _decide_playwright(args.no_playwright):
        try:
            exporter = dl.Exporter(cookie)
        except Exception as e:  # playwright missing / browser not installed
            print(f"[warn]   Playwright unavailable ({e.__class__.__name__}: {e}); "
                  "online docs will be saved as .url shortcuts. "
                  "Install with: pip install playwright && playwright install chromium",
                  file=sys.stderr, flush=True)

    stats = {"folders": 0, "files": 0, "exported": 0, "online_docs": 0, "skipped": 0, "errors": 0}
    try:
        dl.walk(session, exporter, root, space, out_root, stats)
    finally:
        if exporter is not None:
            exporter.close()
    print(json.dumps({"ok": stats["errors"] == 0, "profile": PROFILE_NAME,
                      "root": root.name, "out_dir": str(out_root),
                      "playwright": exporter is not None, "stats": stats},
                     ensure_ascii=False))


# ── upload / sync (local → alidocs) ────────────────────────────────────────

import fnmatch  # noqa: E402
from dataclasses import dataclass  # noqa: E402

DEFAULT_EXCLUDES = (".*", "__pycache__", "node_modules", "*.pyc", ".DS_Store")


def _glob_match(rel: str, patterns: tuple[str, ...]) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat): return True
        if any(fnmatch.fnmatch(p, pat) for p in parts): return True
    return False


def _walk_local(src: Path, includes: tuple[str, ...], excludes: tuple[str, ...]):
    """Yield (rel_posix, abs_path, is_dir) for every entry under src.
    Symlinked dirs are skipped (avoid loops); symlinked files are followed."""
    src = src.resolve()
    if src.is_file():
        rel = src.name
        if includes and not _glob_match(rel, includes): return
        if _glob_match(rel, excludes): return
        yield rel, src, False
        return
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        # Prune excluded dirs in-place
        dirnames[:] = [d for d in dirnames if not _glob_match(prefix + d, excludes)]
        for d in dirnames:
            yield prefix + d, Path(dirpath) / d, True
        for fn in filenames:
            rel = prefix + fn
            if includes and not _glob_match(rel, includes): continue
            if _glob_match(rel, excludes): continue
            yield rel, Path(dirpath) / fn, False


def _list_remote_tree(session, root_uuid: str, space_id: str) -> dict[str, dl.Node]:
    """Recursive map of remote tree keyed by posix-relative path (folder paths
    end without trailing slash; root entry is keyed by '')."""
    out: dict[str, dl.Node] = {}
    def walk(uuid: str, prefix: str) -> None:
        for c in dl.list_children(session, uuid, space_id):
            rel = (prefix + "/" + c.name).lstrip("/")
            out[rel] = c
            if c.is_folder:
                walk(c.uuid, rel)
    walk(root_uuid, "")
    return out


@dataclass
class Action:
    kind: str          # "mkdir" / "upload" / "overwrite" / "skip" / "warn" / "prune"
    rel: str           # posix path relative to dest root
    local: Path | None
    remote: dl.Node | None
    detail: str = ""

    def fmt(self) -> str:
        mark = {"mkdir":"+", "upload":"+", "overwrite":"~", "skip":"=",
                "warn":"!", "prune":"-"}.get(self.kind, "?")
        kind = "dir " if self.kind == "mkdir" else "file"
        size = ""
        if self.local and self.local.is_file():
            size = f" ({self.local.stat().st_size}B)"
        return f"  {mark} {kind:<4}  {self.rel:<50} {self.detail}{size}"


def _plan_push(session, src: Path, dest_uuid: str, space_id: str,
               on_conflict: str, includes: tuple[str, ...], excludes: tuple[str, ...],
               max_size: int | None, prune: bool) -> list[Action]:
    remote = _list_remote_tree(session, dest_uuid, space_id)
    actions: list[Action] = []
    local_keys: set[str] = set()
    for rel, p, is_dir in _walk_local(src, includes, excludes):
        local_keys.add(rel)
        existing = remote.get(rel)
        if is_dir:
            if existing and existing.is_folder:
                actions.append(Action("skip", rel, p, existing, "folder exists"))
            elif existing:
                actions.append(Action("warn", rel, p, existing, "remote is a file"))
            else:
                actions.append(Action("mkdir", rel, p, None))
            continue
        size = p.stat().st_size
        if max_size and size > max_size:
            actions.append(Action("warn", rel, p, existing, f"size {size} > max"))
            continue
        if existing and not existing.is_folder:
            r_size = existing.raw.get("fileSize")
            if on_conflict == "skip-if-same":
                if r_size == size:
                    actions.append(Action("skip", rel, p, existing, f"same size {size}"))
                else:
                    actions.append(Action("overwrite", rel, p, existing,
                                          f"size {r_size}→{size}"))
            elif on_conflict == "overwrite":
                actions.append(Action("overwrite", rel, p, existing, "force"))
            elif on_conflict == "rename":
                actions.append(Action("upload", rel, p, existing, "auto-rename"))
            elif on_conflict == "error":
                actions.append(Action("warn", rel, p, existing, "collision (error mode)"))
            else:
                actions.append(Action("warn", rel, p, existing,
                                      f"unknown on_conflict={on_conflict!r}"))
        elif existing:
            actions.append(Action("warn", rel, p, existing, "remote is a folder"))
        else:
            actions.append(Action("upload", rel, p, None))
    if prune:
        for rel, node in remote.items():
            if rel not in local_keys:
                actions.append(Action("prune", rel, None, node,
                                      "missing locally — will recycle"))
    return actions


def _create_dest_path(session, space_id: str, root_uuid: str, segments: list[str]) -> str:
    """Walk-or-create a chain of folders under root_uuid; return leaf uuid."""
    cur = root_uuid
    cur_children: dict[str, dl.Node] = {n.name: n for n in dl.list_children(session, cur, space_id)}
    for seg in segments:
        if not seg: continue
        existing = cur_children.get(seg)
        if existing and existing.is_folder:
            cur = existing.uuid
        elif existing:
            die(f"--create-dest segment {seg!r} exists as a non-folder")
        else:
            n = dl.create_folder(session, cur, seg, space_id)
            cur = n.uuid
            print(f"[mkdir]  {seg}  uuid={cur}", flush=True)
        cur_children = {n.name: n for n in dl.list_children(session, cur, space_id)}
    return cur


def _resolve_push_dest(session, args) -> tuple[str, str, str]:
    """Return (space_id, dest_folder_uuid, dest_name)."""
    if args.dest:
        uuid, space = parse_link(args.dest)
        space_id, dest_uuid, name_override = _resolve_node(session, uuid, space)
        root = dl.fetch_info(session, dest_uuid, space_id)
        if not root.is_folder:
            die(f"--dest must be a folder link, got {root.dentry_type!r} ({root.name})")
        return space_id, dest_uuid, (name_override or root.name)
    if args.create_dest:
        # form: "<folder-link-or-uuid> :: path/with/slashes"  (split on '::') OR
        #       "<folder-link>/<path>"  — for plain links we slice at the
        #       node-uuid boundary so the remainder is the to-create path.
        spec = args.create_dest
        if "::" in spec:
            head, _, tail = spec.partition("::")
            head = head.strip(); tail = tail.strip()
        else:
            # find the dentry-link prefix, treat rest as the path to create
            m = re.search(r"(https?://[^/]+/i/(?:nodes|spaces)/[^/?#]+(?:\?[^#/]*)?)(/.*)?", spec)
            if m:
                head = m.group(1)
                tail = (m.group(2) or "").lstrip("/")
            else:
                # treat as raw "<uuid>/path"
                head, _, tail = spec.partition("/")
        uuid, space = parse_link(head)
        space_id, base_uuid, name_override = _resolve_node(session, uuid, space)
        base = dl.fetch_info(session, base_uuid, space_id)
        if not base.is_folder:
            die(f"--create-dest base must be a folder, got {base.dentry_type!r}")
        segments = [s for s in tail.split("/") if s]
        if not segments:
            return space_id, base_uuid, base.name
        leaf = _create_dest_path(session, space_id, base_uuid, segments)
        return space_id, leaf, segments[-1]
    die("either --dest <folder-link> or --create-dest <base-link>/path/segments is required")


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _do_push(args, default_conflict: str) -> None:
    cookie = _resolve_cookie()
    a_token = _setting("ALIDOCS_A_TOKEN", "a_token") or None
    on_conflict = args.on_conflict or default_conflict
    if on_conflict not in ("skip-if-same", "overwrite", "rename", "error"):
        die("--on-conflict must be one of skip-if-same / overwrite / rename / error")
    src = Path(os.path.expanduser(args.src)).resolve()
    if not src.exists():
        die(f"--src {src} does not exist")
    excludes = tuple(args.exclude) if args.exclude else DEFAULT_EXCLUDES
    includes = tuple(args.include) if args.include else ()
    session = dl.make_session(cookie, a_token)
    space_id, dest_uuid, dest_name = _resolve_push_dest(session, args)
    print(f"[dest]   {dest_name} (uuid={dest_uuid}, space={space_id})", flush=True)

    actions = _plan_push(session, src, dest_uuid, space_id, on_conflict,
                         includes, excludes, args.max_size or None, args.prune)
    print(f"[plan]   {len(actions)} action(s):", flush=True)
    for a in actions: print(a.fmt(), flush=True)
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "profile": PROFILE_NAME,
                          "actions": len(actions)}, ensure_ascii=False))
        return
    if not args.yes and not _confirm("apply?"):
        die("aborted")

    # uuid map: posix rel → remote dentry uuid (folders we just made, plus
    # already-existing remote nodes from the plan). The plan ran against a
    # snapshot, so we trust those uuids unless conflict-overwrite paths see
    # the remote dentry change mid-run.
    rel_to_uuid: dict[str, str] = {"": dest_uuid}
    # seed from plan's "existing" remotes
    for a in actions:
        if a.remote: rel_to_uuid[a.rel] = a.remote.uuid

    def parent_uuid(rel: str) -> str:
        parent = "/".join(rel.split("/")[:-1])
        return rel_to_uuid.get(parent, dest_uuid)

    stats = {k: 0 for k in ("mkdir", "upload", "overwrite", "skip", "warn", "prune", "errors")}
    for a in sorted(actions, key=lambda a: (a.rel.count("/"), a.rel)):
        try:
            if a.kind == "mkdir":
                n = dl.create_folder(session, parent_uuid(a.rel), a.local.name, space_id)
                rel_to_uuid[a.rel] = n.uuid
                print(f"[mkdir]  {a.rel}", flush=True)
            elif a.kind == "upload":
                conflict = dl.CONFLICT_AUTO_RENAME if on_conflict == "rename" else dl.CONFLICT_AUTO_RENAME
                n = dl.upload_file(session, a.local, parent_uuid(a.rel), space_id,
                                   conflict=conflict, target_name=a.local.name)
                rel_to_uuid[a.rel] = n.uuid
                print(f"[upload] {a.rel}  ({a.local.stat().st_size}B → {n.name})", flush=True)
            elif a.kind == "overwrite":
                n = dl.overwrite_file(session, a.remote.uuid, a.local,
                                      target_name=a.local.name)
                rel_to_uuid[a.rel] = n.uuid
                print(f"[update] {a.rel}  v→{n.raw.get('version','?')}", flush=True)
            elif a.kind == "prune":
                dl.recycle_dentry(session, a.remote.uuid, space_id)
                print(f"[prune]  {a.rel}", flush=True)
            elif a.kind == "skip":
                pass
            elif a.kind == "warn":
                print(f"[warn]   {a.rel}: {a.detail}", flush=True)
            stats[a.kind] = stats.get(a.kind, 0) + 1
        except Exception as e:
            stats["errors"] += 1
            print(f"[err]    {a.rel}: {e}", flush=True)

    print(json.dumps({"ok": stats["errors"] == 0, "profile": PROFILE_NAME,
                      "dest": dest_name, "space_id": space_id,
                      "dest_uuid": dest_uuid, "stats": stats}, ensure_ascii=False))


def cmd_push(args: argparse.Namespace) -> None:
    _do_push(args, default_conflict="skip-if-same")


def cmd_sync(args: argparse.Namespace) -> None:
    _do_push(args, default_conflict="skip-if-same")


def _add_push_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--src", required=True, help="local file or directory to upload")
    sp.add_argument("--dest", default="", help="alidocs folder link (or use --create-dest)")
    sp.add_argument("--create-dest", default="",
                    help="auto-create folder path under an existing link, e.g. "
                         "'<folder-link>::sub/dir/leaf' (also accepts a plain "
                         "'<folder-link>/sub/dir/leaf')")
    sp.add_argument("--on-conflict", default="",
                    choices=("", "skip-if-same", "overwrite", "rename", "error"),
                    help="how to handle name collisions (default: skip-if-same)")
    sp.add_argument("--include", action="append", default=[],
                    help="glob to include (repeatable; default: include all)")
    sp.add_argument("--exclude", action="append", default=[],
                    help=f"glob to exclude (repeatable; default: {' '.join(DEFAULT_EXCLUDES)})")
    sp.add_argument("--max-size", type=int, default=0,
                    help="skip files larger than this many bytes")
    sp.add_argument("--prune", action="store_true",
                    help="recycle remote dentries with no local counterpart")
    sp.add_argument("--dry-run", "-n", action="store_true",
                    help="print plan and exit without writing")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="apply without confirmation prompt")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download alidocs / DingTalk docs from a link")
    p.add_argument("--profile", default="", help="config profile (else ALIDOCS_PROFILE / default_profile)")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download a doc or a whole folder from its link")
    f.add_argument("--url", required=True, help="alidocs node link (doc or folder)")
    f.add_argument("--out", default="", help="output dir (else config default_out_dir, else ~/Downloads/alidocs)")
    f.add_argument("--no-playwright", action="store_true", help="don't export online docs; save .url shortcuts only")
    f.set_defaults(func=cmd_fetch)

    c = sub.add_parser("check", help="verify cookie + resolve the node (no download)")
    c.add_argument("--url", required=True)
    c.set_defaults(func=cmd_check)

    ph = sub.add_parser("push", help="upload local file/dir to an alidocs folder")
    _add_push_args(ph)
    ph.set_defaults(func=cmd_push)

    sy = sub.add_parser("sync", help="one-way sync local → alidocs (same as push, see --prune)")
    _add_push_args(sy)
    sy.set_defaults(func=cmd_sync)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
