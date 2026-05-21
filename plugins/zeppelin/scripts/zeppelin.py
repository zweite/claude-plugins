#!/usr/bin/env python3
"""
zeppelin.py — minimal Apache Zeppelin REST CLI for the Claude Code `zeppelin`
skill. Stdlib-only; no pip install required.

Auth: Shiro form login → ticket + JSESSIONID cookie, retried on 401.

Commands:
  login                                 — verify credentials work
  test-conn                             — login + /api/security/ticket probe
  submit  --magic M --code C [--name N] — create note + paragraph + run
                                          + poll until terminal, print JSON
  fetch   --note N --para P             — one-shot paragraph status/result
  poll    --note N --para P [--timeout S] [--interval S]
                                          — poll an existing paragraph
  list-notes                            — list every visible note
  delete-note --note N                  — drop a note
  exec    --magic M --code C [--name N] [--keep-note]
                                          — convenience: submit + cleanup
  cache get   --table db.t [--max-age-days N]   — read cached schema+sample
  cache put   --table db.t [--force] [--limit N] — cache schema + N-row sample
  cache list                                    — cached tables + freshness
  cache clear --table db.t | --all              — evict cache entries

All output is JSON on stdout. Errors go to stderr; exit code != 0 on failure.

Settings come from env (preferred) or ~/.zeppelin/config.json. For each one
the env var wins; if unset, the config.json key is used; else the default.

  env var                          config.json key          default
  ZEPPELIN_BASE_URL                base_url                 (required)
  ZEPPELIN_USERNAME                username                 (required)
  ZEPPELIN_PASSWORD                password                 (required)
  ZEPPELIN_NOTE_DIR                note_dir                 __skill/zeppelin
  ZEPPELIN_KEEP_NOTES              keep_notes               false
  ZEPPELIN_TIMEOUT_SECONDS         timeout_seconds          300
  ZEPPELIN_POLL_INTERVAL_SECONDS   poll_interval_seconds    1.5
  ZEPPELIN_CACHE_DIR               cache_dir                ~/.zeppelin/cache
  ZEPPELIN_CACHE_TTL_DAYS          cache_ttl_days           30

Multiple environments: the config may hold a `profiles` map instead of flat
keys, selected with --profile / ZEPPELIN_PROFILE (else default_profile, else
the sole profile). Top-level keys are shared defaults merged into each profile;
the cache is namespaced per profile. A flat config (no `profiles`) is the
implicit "default" profile and keeps its cache un-namespaced. Example:

  { "default_profile": "prod",
    "profiles": {
      "prod": {"base_url": "...", "username": "...", "password": "..."},
      "stg":  {"base_url": "...", "username": "...", "password": "..."} },
    "cache_ttl_days": 30 }
"""
from __future__ import annotations

import argparse
import datetime
import http.client
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

CONFIG_PATH = os.path.expanduser("~/.zeppelin/config.json")
TERMINAL = {"FINISHED", "ERROR", "ABORT"}

_TICKET_JSON = re.compile(r'"ticket"\s*:\s*"[^"]*"')
_TICKET_ATTR = re.compile(r'ticket\s*=\s*"[^"]*"', re.IGNORECASE)


def redact(s: str) -> str:
    s = _TICKET_JSON.sub('"ticket":"[REDACTED]"', s)
    s = _TICKET_ATTR.sub('ticket="[REDACTED]"', s)
    return s


def die(msg: str, code: int = 1) -> None:
    print(redact(msg), file=sys.stderr)
    sys.exit(code)


def _load_config() -> dict[str, Any]:
    """Read ~/.zeppelin/config.json once. Missing file is fine; malformed
    file is fatal so a typo doesn't silently fall back to defaults."""
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
    """Peek --profile out of argv before argparse runs (settings resolve at
    import time and can be profile-specific)."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return os.environ.get("ZEPPELIN_PROFILE", "").strip()


def _resolve_profile(config: dict[str, Any], selected: str) -> tuple[dict[str, Any], str, bool]:
    """Return (effective settings dict, profile name, uses_profiles). A config
    with a non-empty `profiles` map selects one (via --profile/env, else
    default_profile, else the sole profile); top-level keys are shared defaults.
    A flat config is the single implicit 'default' profile."""
    profiles = config.get("profiles")
    if isinstance(profiles, dict) and profiles:
        base = {k: v for k, v in config.items() if k not in ("profiles", "default_profile")}
        name = selected or config.get("default_profile", "")
        if not name:
            if len(profiles) == 1:
                name = next(iter(profiles))
            else:
                die("multiple profiles defined; pass --profile or set default_profile. "
                    f"Available: {', '.join(sorted(profiles))}")
        if name not in profiles:
            die(f"profile {name!r} not found in {CONFIG_PATH}. Available: {', '.join(sorted(profiles))}")
        if not isinstance(profiles[name], dict):
            die(f"profile {name!r} must be a JSON object")
        merged = dict(base)
        merged.update(profiles[name])
        return merged, name, True
    return dict(config), (selected or "default"), False


_CONFIG = _load_config()
_PROFILE, PROFILE_NAME, USES_PROFILES = _resolve_profile(_CONFIG, _early_profile())


def _cfg_str(env_key: str, config_key: str, default: str) -> str:
    """Resolve a string setting: env var wins, then profile/config, then default."""
    v = os.environ.get(env_key, "").strip()
    if v:
        return v
    cv = _PROFILE.get(config_key)
    if cv not in (None, ""):
        return str(cv).strip()
    return default


def _cfg_bool(env_key: str, config_key: str, default: bool) -> bool:
    truthy = ("1", "true", "yes", "on")
    v = os.environ.get(env_key, "").strip().lower()
    if v:
        return v in truthy
    cv = _PROFILE.get(config_key)
    if isinstance(cv, bool):
        return cv
    if isinstance(cv, str) and cv.strip():
        return cv.strip().lower() in truthy
    return default


def _cfg_float(env_key: str, config_key: str, default: float) -> float:
    v = os.environ.get(env_key, "").strip()
    raw = v if v else _PROFILE.get(config_key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        die(f"invalid number for {env_key}/{config_key}: {raw!r}")


DEFAULT_TIMEOUT = _cfg_float("ZEPPELIN_TIMEOUT_SECONDS", "timeout_seconds", 300.0)
DEFAULT_INTERVAL = _cfg_float("ZEPPELIN_POLL_INTERVAL_SECONDS", "poll_interval_seconds", 1.5)
DEFAULT_NOTE_DIR = _cfg_str("ZEPPELIN_NOTE_DIR", "note_dir", "__skill/zeppelin").strip("/")
DEFAULT_KEEP_NOTES = _cfg_bool("ZEPPELIN_KEEP_NOTES", "keep_notes", False)
_BASE_CACHE_DIR = os.path.expanduser(
    _cfg_str("ZEPPELIN_CACHE_DIR", "cache_dir", "~/.zeppelin/cache"))
DEFAULT_CACHE_TTL_DAYS = _cfg_float("ZEPPELIN_CACHE_TTL_DAYS", "cache_ttl_days", 30.0)
# namespace the cache per profile only when profiles are in use; a flat config
# keeps the un-namespaced layout so existing caches aren't orphaned.
DEFAULT_CACHE_DIR = os.path.join(_BASE_CACHE_DIR, PROFILE_NAME) if USES_PROFILES else _BASE_CACHE_DIR
DEFAULT_SAMPLE_ROWS = 10
_TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.]*$")


@dataclass
class Creds:
    base_url: str
    username: str
    password: str


def load_creds() -> Creds:
    base = os.environ.get("ZEPPELIN_BASE_URL", "") or _PROFILE.get("base_url", "")
    user = os.environ.get("ZEPPELIN_USERNAME", "") or _PROFILE.get("username", "")
    pw = os.environ.get("ZEPPELIN_PASSWORD", "") or _PROFILE.get("password", "")
    missing = [k for k, v in (("ZEPPELIN_BASE_URL", base), ("ZEPPELIN_USERNAME", user), ("ZEPPELIN_PASSWORD", pw)) if not v]
    if missing:
        die(
            "missing zeppelin credentials: "
            + ", ".join(missing)
            + f". Set the env vars or populate {CONFIG_PATH}."
        )
    return Creds(base_url=base.rstrip("/"), username=user, password=pw)


class Client:
    """Shiro-authenticated Zeppelin REST client. Each Client owns its own
    cookie jar and ticket; safe to reuse across calls but not across processes."""

    def __init__(self, creds: Creds, timeout: float = 15.0) -> None:
        self.creds = creds
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._ticket = ""

    # ── low-level ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(self.creds.base_url + path, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        if self._ticket:
            req.add_header("ticket", self._ticket)
        try:
            resp = self._opener.open(req, timeout=self.timeout)
            return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() or b""
        except urllib.error.URLError as e:
            die(f"network error talking to {self.creds.base_url}{path}: {e.reason}")
        except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as e:
            # e.g. RemoteDisconnected when the server drops the connection under
            # load — urllib doesn't wrap these in URLError, so catch them here
            # and fail cleanly instead of crashing with a traceback.
            die(f"connection error talking to {self.creds.base_url}{path}: {e}")

    def _request_retry401(self, method: str, path: str, body: bytes | None = None,
                          content_type: str | None = None) -> tuple[int, bytes]:
        code, raw = self._request(method, path, body, content_type)
        if code != 401:
            return code, raw
        self.login()
        return self._request(method, path, body, content_type)

    # ── auth ─────────────────────────────────────────────────────────────
    def login(self) -> None:
        form = urllib.parse.urlencode(
            {"userName": self.creds.username, "password": self.creds.password}
        ).encode()
        code, raw = self._request(
            "POST", "/api/login", form, "application/x-www-form-urlencoded"
        )
        if code == 403:
            die("login failed: bad username or password (HTTP 403)")
        if code >= 400:
            die(f"login failed: HTTP {code}: {redact(raw[:200].decode('utf-8', 'replace'))}")
        try:
            parsed = json.loads(raw)
        except ValueError:
            die(f"login: non-JSON response: {redact(raw[:200].decode('utf-8', 'replace'))}")
        if parsed.get("status") != "OK":
            die(f"login: status={parsed.get('status')}")
        self._ticket = parsed.get("body", {}).get("ticket", "") or ""

    def check_session(self) -> dict[str, Any]:
        code, raw = self._request_retry401("GET", "/api/security/ticket")
        if code >= 400:
            die(f"check session: HTTP {code}: {redact(raw[:200].decode('utf-8', 'replace'))}")
        body = json.loads(raw or b"{}")
        if body.get("status") != "OK" or not body.get("body", {}).get("principal"):
            die(f"session not authenticated: {body}")
        return body["body"]

    # ── notebook ops ─────────────────────────────────────────────────────
    def _get_json(self, path: str) -> Any:
        code, raw = self._request_retry401("GET", path)
        if code >= 400:
            die(f"GET {path}: HTTP {code}: {redact(raw[:200].decode('utf-8', 'replace'))}")
        return json.loads(raw or b"{}")

    def _post_json(self, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        code, raw = self._request_retry401("POST", path, data, "application/json" if data else None)
        if code >= 400:
            die(f"POST {path}: HTTP {code}: {redact(raw[:200].decode('utf-8', 'replace'))}")
        return json.loads(raw or b"{}") if raw else {}

    def submit(self, name: str, magic: str, code: str) -> tuple[str, str]:
        text = magic.strip()
        if code:
            text = (text + "\n" + code) if text else code
        create = self._post_json(
            "/api/notebook",
            {"name": name, "paragraphs": [{"text": text}]},
        )
        note_id = create.get("body")
        if not note_id:
            die(f"create note: empty id in response: {create}")
        detail = self._get_json(f"/api/notebook/{note_id}")
        paras = detail.get("body", {}).get("paragraphs", [])
        if not paras:
            die(f"note {note_id} has no paragraphs after create")
        para_id = paras[0].get("id")
        if not para_id:
            die(f"note {note_id} paragraph has no id: {paras[0]}")
        self._post_json(f"/api/notebook/job/{note_id}/{para_id}")
        return note_id, para_id

    def fetch(self, note_id: str, para_id: str) -> dict[str, Any]:
        body = self._get_json(f"/api/notebook/{note_id}/paragraph/{para_id}").get("body", {})
        status = body.get("status", "")
        msgs = (body.get("results", {}) or {}).get("msg", []) or []
        rows: list[dict[str, str]] | None = None
        text_parts: list[str] = []
        for m in msgs:
            if m.get("type") == "TABLE" and rows is None:
                rows = parse_tsv(m.get("data", ""))
                continue
            d = m.get("data", "")
            if d:
                text_parts.append(d)
        return {
            "status": status,
            "is_table": rows is not None,
            "rows": rows,
            "text": "\n".join(text_parts),
        }

    def poll(self, note_id: str, para_id: str, timeout: float, interval: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while True:
            last = self.fetch(note_id, para_id)
            if last["status"] in TERMINAL:
                return last
            if time.monotonic() >= deadline:
                last["timed_out"] = True
                return last
            time.sleep(interval)

    def list_notes(self) -> list[dict[str, Any]]:
        return self._get_json("/api/notebook").get("body", []) or []

    def delete_note(self, note_id: str) -> None:
        code, raw = self._request_retry401("DELETE", f"/api/notebook/{note_id}")
        if code >= 400:
            die(f"DELETE /api/notebook/{note_id}: HTTP {code}: {redact(raw[:200].decode('utf-8', 'replace'))}")


def parse_tsv(data: str) -> list[dict[str, str]]:
    data = data.rstrip("\n")
    if not data:
        return []
    lines = data.split("\n")
    headers = lines[0].split("\t")
    out: list[dict[str, str]] = []
    for line in lines[1:]:
        cols = line.split("\t")
        out.append({h: (cols[i] if i < len(cols) else "") for i, h in enumerate(headers)})
    return out


def emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def cmd_login(_args: argparse.Namespace) -> None:
    c = Client(load_creds())
    c.login()
    emit({"ok": True})


def cmd_test_conn(_args: argparse.Namespace) -> None:
    started = time.monotonic()
    c = Client(load_creds())
    c.login()
    principal = c.check_session()
    emit({
        "ok": True,
        "principal": principal,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    })


def _note_name(name: str) -> str:
    """Full workspace path for a new note: <note_dir>/<label>-<timestamp>.

    The caller (the Agent driving the skill) supplies a short, business-
    meaningful <label> via --name, e.g. `dau-check` or `order-revenue`. We
    place it under the configured note_dir and append a timestamp so repeated
    runs of the same query don't collide. Falls back to `query` when no name
    is given (and strips any timestamp the caller already tacked on)."""
    base = DEFAULT_NOTE_DIR or "__skill/zeppelin"
    label = (name or "query").strip().strip("/") or "query"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{base}/{label}-{stamp}"


def cmd_submit(args: argparse.Namespace) -> None:
    c = Client(load_creds())
    c.login()
    name = _note_name(args.name)
    note_id, para_id = c.submit(name, args.magic, args.code)
    emit({"note_id": note_id, "paragraph_id": para_id, "note_name": name})


def cmd_fetch(args: argparse.Namespace) -> None:
    c = Client(load_creds())
    c.login()
    emit(c.fetch(args.note, args.para))


def cmd_poll(args: argparse.Namespace) -> None:
    c = Client(load_creds())
    c.login()
    result = c.poll(args.note, args.para, args.timeout, args.interval)
    emit(result)


def cmd_list_notes(_args: argparse.Namespace) -> None:
    c = Client(load_creds())
    c.login()
    emit(c.list_notes())


def cmd_delete_note(args: argparse.Namespace) -> None:
    c = Client(load_creds())
    c.login()
    c.delete_note(args.note)
    emit({"ok": True, "note_id": args.note})


def cmd_exec(args: argparse.Namespace) -> None:
    """submit + poll until terminal; on success, optionally delete the note."""
    c = Client(load_creds())
    c.login()
    name = _note_name(args.name)
    note_id, para_id = c.submit(name, args.magic, args.code)
    result = c.poll(note_id, para_id, args.timeout, args.interval)
    result["note_id"] = note_id
    result["paragraph_id"] = para_id
    result["note_name"] = name
    if not args.keep_note and result.get("status") in TERMINAL:
        try:
            c.delete_note(note_id)
            result["note_deleted"] = True
        except SystemExit:
            result["note_deleted"] = False
    emit(result)


# ── metadata cache ───────────────────────────────────────────────────────
# Persist each reviewed table's schema + a small data sample under cache_dir
# so later runs can confirm a table without re-querying Zeppelin. Entries
# carry their own TTL (default 30d); reads past the TTL report "stale".

def _table_ok(table: str) -> str:
    """Validate and normalize a db.table identifier (guards SQL interpolation)."""
    t = (table or "").strip()
    if not _TABLE_RE.match(t):
        die(f"invalid table name {table!r}: expected db.table (letters, digits, _ and .)")
    return t


def _cache_path(table: str) -> str:
    safe = table.replace(os.sep, "_")
    return os.path.join(DEFAULT_CACHE_DIR, f"{safe}.json")


def _read_cache(table: str) -> dict[str, Any] | None:
    path = _cache_path(table)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _entry_age_days(entry: dict[str, Any]) -> float | None:
    try:
        cached = datetime.datetime.fromisoformat(entry["cached_at"])
    except (KeyError, ValueError, TypeError):
        return None
    return (datetime.datetime.now() - cached).total_seconds() / 86400.0


def _freshness(entry: dict[str, Any], max_age_days: float) -> tuple[str, float | None]:
    age = _entry_age_days(entry)
    if age is None:
        return "stale", None
    return ("hit" if age <= max_age_days else "stale"), round(age, 2)


def _run_select(c: Client, code: str, timeout: float, interval: float) -> dict[str, Any]:
    """Run a read-only paragraph for cache population; always clean up the note."""
    note_id, para_id = c.submit(_note_name("cache"), "%spark.sql", code)
    try:
        result = c.poll(note_id, para_id, timeout, interval)
    finally:
        try:
            c.delete_note(note_id)
        except SystemExit:
            pass
    return result


def _fetch_table_meta(c: Client, table: str, limit: int,
                      timeout: float, interval: float) -> dict[str, Any]:
    desc = _run_select(c, f"DESCRIBE {table}", timeout, interval)
    if desc.get("status") != "FINISHED":
        die(f"DESCRIBE {table} failed ({desc.get('status')}): {desc.get('text', '')[:300]}")
    columns: list[dict[str, str]] = []
    for row in desc.get("rows") or []:
        name = (row.get("col_name") or "").strip()
        if not name or name.startswith("#"):
            break  # partition-info / detailed-table section starts here
        columns.append({
            "name": name,
            "type": (row.get("data_type") or "").strip(),
            "comment": (row.get("comment") or "").strip(),
        })
    sample = _run_select(c, f"SELECT * FROM {table} LIMIT {limit}", timeout, interval)
    if sample.get("status") != "FINISHED":
        die(f"SELECT from {table} failed ({sample.get('status')}): {sample.get('text', '')[:300]}")
    rows = sample.get("rows") or []
    return {
        "table": table,
        "cached_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ttl_days": DEFAULT_CACHE_TTL_DAYS,
        "columns": columns,
        "sample": rows,
        "sample_row_count": len(rows),
    }


def cmd_cache_get(args: argparse.Namespace) -> None:
    table = _table_ok(args.table)
    max_age = args.max_age_days if args.max_age_days is not None else DEFAULT_CACHE_TTL_DAYS
    entry = _read_cache(table)
    if entry is None:
        emit({"table": table, "status": "miss"})
        return
    status, age = _freshness(entry, max_age)
    entry["status"] = status
    entry["age_days"] = age
    emit(entry)


def cmd_cache_put(args: argparse.Namespace) -> None:
    table = _table_ok(args.table)
    if not args.force:
        entry = _read_cache(table)
        if entry is not None:
            status, age = _freshness(entry, DEFAULT_CACHE_TTL_DAYS)
            if status == "hit":
                entry["status"] = "fresh"  # already cached and within TTL; skip re-query
                entry["age_days"] = age
                emit(entry)
                return
    c = Client(load_creds())
    c.login()
    entry = _fetch_table_meta(c, table, args.limit, args.timeout, args.interval)
    os.makedirs(DEFAULT_CACHE_DIR, exist_ok=True)
    path = _cache_path(table)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2, default=str)
    entry["status"] = "written"
    entry["path"] = path
    emit(entry)


def cmd_cache_list(_args: argparse.Namespace) -> None:
    out: list[dict[str, Any]] = []
    if os.path.isdir(DEFAULT_CACHE_DIR):
        for fn in sorted(os.listdir(DEFAULT_CACHE_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(DEFAULT_CACHE_DIR, fn), "r", encoding="utf-8") as f:
                    entry = json.load(f)
            except (OSError, ValueError):
                continue
            status, age = _freshness(entry, entry.get("ttl_days", DEFAULT_CACHE_TTL_DAYS))
            out.append({
                "table": entry.get("table", fn[:-5]),
                "cached_at": entry.get("cached_at"),
                "age_days": age,
                "status": status,
                "columns": len(entry.get("columns") or []),
                "sample_rows": entry.get("sample_row_count", len(entry.get("sample") or [])),
            })
    emit({"cache_dir": DEFAULT_CACHE_DIR, "ttl_days": DEFAULT_CACHE_TTL_DAYS, "tables": out})


def cmd_cache_clear(args: argparse.Namespace) -> None:
    if not args.table and not args.all:
        die("cache clear: pass --table <db.table> or --all")
    removed: list[str] = []
    if args.all:
        if os.path.isdir(DEFAULT_CACHE_DIR):
            for fn in os.listdir(DEFAULT_CACHE_DIR):
                if fn.endswith(".json"):
                    os.remove(os.path.join(DEFAULT_CACHE_DIR, fn))
                    removed.append(fn[:-5])
    else:
        table = _table_ok(args.table)
        path = _cache_path(table)
        if os.path.exists(path):
            os.remove(path)
            removed.append(table)
    emit({"ok": True, "removed": removed})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Zeppelin REST CLI for Claude Code skill")
    p.add_argument("--profile", default="", help="config profile to use (else ZEPPELIN_PROFILE / default_profile)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login").set_defaults(func=cmd_login)
    sub.add_parser("test-conn").set_defaults(func=cmd_test_conn)

    sp = sub.add_parser("submit", help="create note + paragraph + start job (does not wait)")
    sp.add_argument("--magic", required=True, help="leading magic, e.g. %%spark.sql or %%pyspark")
    sp.add_argument("--code", required=True, help="paragraph body (SQL or code)")
    sp.add_argument("--name", default="", help="short business label, e.g. 'dau-check'; placed under note_dir with a -<timestamp> suffix. Default label: 'query'")
    sp.set_defaults(func=cmd_submit)

    sp = sub.add_parser("fetch", help="one-shot status+result for an existing paragraph")
    sp.add_argument("--note", required=True)
    sp.add_argument("--para", required=True)
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("poll", help="poll until FINISHED/ERROR/ABORT or timeout")
    sp.add_argument("--note", required=True)
    sp.add_argument("--para", required=True)
    sp.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sp.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    sp.set_defaults(func=cmd_poll)

    sp = sub.add_parser("list-notes")
    sp.set_defaults(func=cmd_list_notes)

    sp = sub.add_parser("delete-note")
    sp.add_argument("--note", required=True)
    sp.set_defaults(func=cmd_delete_note)

    sp = sub.add_parser("exec", help="submit + poll + (default) delete the note when done")
    sp.add_argument("--magic", required=True)
    sp.add_argument("--code", required=True)
    sp.add_argument("--name", default="", help="short business label, e.g. 'dau-check'; placed under note_dir with a -<timestamp> suffix. Default label: 'query'")
    sp.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sp.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    sp.add_argument("--keep-note", action=argparse.BooleanOptionalAction, default=DEFAULT_KEEP_NOTES,
                    help="keep the note after run (default from ZEPPELIN_KEEP_NOTES; use --no-keep-note to force delete)")
    sp.set_defaults(func=cmd_exec)

    cache = sub.add_parser("cache", help="schema + data-sample cache for reviewed tables")
    csub = cache.add_subparsers(dest="cache_cmd", required=True)

    cg = csub.add_parser("get", help="read a cached table entry (status: hit/stale/miss)")
    cg.add_argument("--table", required=True, help="db.table")
    cg.add_argument("--max-age-days", type=float, default=None,
                    help=f"freshness window; default cache_ttl_days ({DEFAULT_CACHE_TTL_DAYS:g})")
    cg.set_defaults(func=cmd_cache_get)

    cp = csub.add_parser("put", help="cache a table's schema + sample (skips if fresh unless --force)")
    cp.add_argument("--table", required=True, help="db.table")
    cp.add_argument("--force", action="store_true", help="re-query and overwrite even if the entry is still fresh")
    cp.add_argument("--limit", type=int, default=DEFAULT_SAMPLE_ROWS, help="sample row count (default 10)")
    cp.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    cp.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    cp.set_defaults(func=cmd_cache_put)

    csub.add_parser("list", help="list cached tables with freshness").set_defaults(func=cmd_cache_list)

    cc = csub.add_parser("clear", help="evict cache entries")
    cc.add_argument("--table", default="", help="db.table to evict")
    cc.add_argument("--all", action="store_true", help="evict every cached table")
    cc.set_defaults(func=cmd_cache_clear)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
