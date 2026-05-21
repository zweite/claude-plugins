#!/usr/bin/env python3
"""
alidocs.py — download a DingTalk / alidocs document (or a whole folder) from a
shared link.

Thin wrapper over dl_alidocs.py: give it a node link and it mirrors that subtree
to disk — a single doc downloads just that doc, a folder downloads everything
under it (folder vs leaf is auto-detected). Online docs export to .docx/.xlsx
via Playwright when it's installed, otherwise they're saved as .url shortcuts.

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

def parse_link(url: str) -> tuple[str, str | None]:
    """Extract (dentryUuid, spaceId|None) from an alidocs node link, e.g.
    https://alidocs.dingtalk.com/i/nodes/<uuid>?... — works for both a single
    doc and a folder node."""
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
    if not uuid:
        die(f"could not extract a dentry UUID from {url!r}. Expected an alidocs "
            "node link like https://alidocs.dingtalk.com/i/nodes/<uuid>")
    space = None
    for k in ("spaceId", "workspaceId"):
        if qs.get(k):
            space = qs[k][0]
            break
    return uuid, space


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


def _resolve_space(session, uuid: str, space: str | None) -> str:
    space = space or (_PROFILE.get("space_id") or None)
    if space:
        return str(space)
    r = session.get(f"{dl.BASE}/i/nodes/{uuid}", timeout=20)
    m = re.search(r'"spaceId":"([^"]+)"', r.text)
    if not m:
        if "统一身份认证" in r.text or "<title>" in r.text and "登录" in r.text:
            die("not logged in — the cookie looks expired. Refresh it in "
                f"{CONFIG_PATH}: open alidocs.dingtalk.com in a logged-in browser, "
                "copy the full Cookie request header (DevTools → Network → any request "
                "→ Request Headers → Cookie), and set it as `cookie`.")
        die("could not discover spaceId from the page; add `space_id` to the config "
            "or use a link that includes spaceId.")
    return m.group(1)


# ── commands ───────────────────────────────────────────────────────────────

def cmd_check(args: argparse.Namespace) -> None:
    cookie = _resolve_cookie()
    a_token = _setting("ALIDOCS_A_TOKEN", "a_token") or None
    uuid, space = parse_link(args.url)
    session = dl.make_session(cookie, a_token)
    space = _resolve_space(session, uuid, space)
    root = dl.fetch_info(session, uuid, space)
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
    space = _resolve_space(session, uuid, space)
    root = dl.fetch_info(session, uuid, space)
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

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
