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

All output is JSON on stdout. Errors go to stderr; exit code != 0 on failure.

Credentials come from env (preferred) or ~/.zeppelin/config.json:
  ZEPPELIN_BASE_URL=http://host:port[/basePath]
  ZEPPELIN_USERNAME=user
  ZEPPELIN_PASSWORD=pass
  ZEPPELIN_TIMEOUT_SECONDS=300        # default poll cap
  ZEPPELIN_POLL_INTERVAL_SECONDS=1.5
"""
from __future__ import annotations

import argparse
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
DEFAULT_TIMEOUT = float(os.environ.get("ZEPPELIN_TIMEOUT_SECONDS", "300"))
DEFAULT_INTERVAL = float(os.environ.get("ZEPPELIN_POLL_INTERVAL_SECONDS", "1.5"))
DEFAULT_NOTE_DIR = os.environ.get("ZEPPELIN_NOTE_DIR", "__skill/zeppelin").strip().strip("/")
DEFAULT_KEEP_NOTES = os.environ.get("ZEPPELIN_KEEP_NOTES", "").strip().lower() in ("1", "true", "yes", "on")
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


@dataclass
class Creds:
    base_url: str
    username: str
    password: str


def load_creds() -> Creds:
    base = os.environ.get("ZEPPELIN_BASE_URL", "")
    user = os.environ.get("ZEPPELIN_USERNAME", "")
    pw = os.environ.get("ZEPPELIN_PASSWORD", "")
    if not (base and user and pw) and os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                blob = json.load(f)
            base = base or blob.get("base_url", "")
            user = user or blob.get("username", "")
            pw = pw or blob.get("password", "")
        except (OSError, ValueError) as e:
            die(f"failed to read {CONFIG_PATH}: {e}")
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


def _default_name() -> str:
    base = DEFAULT_NOTE_DIR or "__skill/zeppelin"
    return f"{base}/{int(time.time())}-{os.getpid()}"


def cmd_submit(args: argparse.Namespace) -> None:
    c = Client(load_creds())
    c.login()
    name = args.name or _default_name()
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
    name = args.name or _default_name()
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Zeppelin REST CLI for Claude Code skill")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login").set_defaults(func=cmd_login)
    sub.add_parser("test-conn").set_defaults(func=cmd_test_conn)

    sp = sub.add_parser("submit", help="create note + paragraph + start job (does not wait)")
    sp.add_argument("--magic", required=True, help="leading magic, e.g. %%spark.sql or %%pyspark")
    sp.add_argument("--code", required=True, help="paragraph body (SQL or code)")
    sp.add_argument("--name", default="", help="optional note name; default: __skill/zeppelin/<ts>")
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
    sp.add_argument("--name", default="")
    sp.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sp.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    sp.add_argument("--keep-note", action=argparse.BooleanOptionalAction, default=DEFAULT_KEEP_NOTES,
                    help="keep the note after run (default from ZEPPELIN_KEEP_NOTES; use --no-keep-note to force delete)")
    sp.set_defaults(func=cmd_exec)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
