#!/usr/bin/env python3
"""
mysql.py — minimal MySQL CLI for the Claude Code `mysql` skill. Stdlib-only;
no pip install, no `mysql` client binary required — it speaks the MySQL wire
protocol directly over a socket and supports both auth plugins:

  * mysql_native_password  (MySQL 5.7 / explicitly-set accounts)
  * caching_sha2_password  (MySQL 8.0 default)

For caching_sha2_password the *fast path* (password already cached on the
server) works over a plain socket. The first-ever login for an account hits
"full auth", which requires a secure channel; this client upgrades to TLS
(stdlib `ssl`) and sends the password over the encrypted connection. The
RSA-public-key path is NOT implemented (RSA isn't in the stdlib) — without TLS
a cold-cache caching_sha2 login will error with a clear message.

Commands:
  test-conn                       — connect + SELECT 1
  query   --sql 'SELECT ...'      — run one statement, print JSON result
  cache get   --table db.t [--max-age-days N]   — read cached schema+sample
  cache put   --table db.t [--force] [--limit N] — cache schema + N-row sample
  cache list                                    — cached tables + freshness
  cache clear --table db.t | --all              — evict cache entries

All output is JSON on stdout. Errors go to stderr; exit code != 0 on failure.

Settings come from env (preferred) or ~/.taku/mysql.json (legacy
~/.mysql/config.json still read; override the dir with TAKU_DIR). The config
may hold a single flat connection OR a `profiles` map for multiple environments:

  {
    "default_profile": "prod",
    "profiles": {
      "prod":    {"host": "db-prod",    "port": 3306, "user": "u", "password": "p"},
      "staging": {"host": "db-staging", "user": "u", "password": "p", "ssl": true}
    },
    "cache_ttl_days": 30
  }

Top-level keys (outside `profiles`) are shared defaults merged into every
profile. A flat config with no `profiles` key is treated as the single
"default" profile (and its cache stays un-namespaced). Pick a profile with
--profile NAME or MYSQL_PROFILE; otherwise default_profile, else the sole
profile.

  env var                      profile/config key       default
  MYSQL_HOST                   host                     (required)
  MYSQL_PORT                   port                     3306
  MYSQL_USER                   user                     (required)
  MYSQL_PASSWORD               password                 (required)
  MYSQL_DATABASE               database                 (optional)
  MYSQL_PROFILE                —                        (profile selector)
  MYSQL_SSL                    ssl                      false  (force TLS from start)
  MYSQL_SSL_VERIFY             ssl_verify               true   (verify server cert)
  MYSQL_TIMEOUT_SECONDS        timeout_seconds          30
  MYSQL_CACHE_DIR              cache_dir                ~/.mysql/cache
  MYSQL_CACHE_TTL_DAYS         cache_ttl_days           30
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import sys
from dataclasses import dataclass
from typing import Any


def _config_path(tool: str, legacy: str) -> str:
    """Unified config location ~/.taku/<tool>.json (override base dir with
    TAKU_DIR). Falls back to the legacy per-tool path if the unified file
    isn't there yet, so existing setups keep working."""
    base = os.environ.get("TAKU_DIR", "").strip() or os.path.expanduser("~/.taku")
    unified = os.path.join(base, f"{tool}.json")
    if os.path.exists(unified):
        return unified
    if os.path.exists(os.path.expanduser(legacy)):
        return os.path.expanduser(legacy)
    return unified


CONFIG_PATH = _config_path("mysql", "~/.mysql/config.json")
DEFAULT_SAMPLE_ROWS = 10
_TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.]*$")

NATIVE = "mysql_native_password"
SHA2 = "caching_sha2_password"

# MySQL capability flags
CLIENT_LONG_PASSWORD = 0x00000001
CLIENT_LONG_FLAG = 0x00000004
CLIENT_CONNECT_WITH_DB = 0x00000008
CLIENT_SSL = 0x00000800
CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_PLUGIN_AUTH = 0x00080000

_PWD_RE = re.compile(r'("password"\s*:\s*")[^"]*(")')


def redact(s: str) -> str:
    return _PWD_RE.sub(r"\1[REDACTED]\2", s)


def die(msg: str, code: int = 1) -> None:
    print(redact(msg), file=sys.stderr)
    sys.exit(code)


class _NeedTLS(Exception):
    """Raised mid-auth when caching_sha2 demands full auth but the socket is
    not encrypted. The caller reconnects once with TLS enabled."""


# ── config + profiles ────────────────────────────────────────────────────

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
    """Peek --profile out of argv before argparse runs (settings resolve at
    import time and may be profile-specific)."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return os.environ.get("MYSQL_PROFILE", "").strip()


def _resolve_profile(config: dict[str, Any], selected: str) -> tuple[dict[str, Any], str, bool]:
    """Return (effective settings dict, profile name, uses_profiles)."""
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
        merged = dict(base)
        if not isinstance(profiles[name], dict):
            die(f"profile {name!r} must be a JSON object")
        merged.update(profiles[name])
        return merged, name, True
    # flat config → single implicit default profile (cache stays un-namespaced)
    return dict(config), (selected or "default"), False


_CONFIG = _load_config()
_PROFILE, PROFILE_NAME, USES_PROFILES = _resolve_profile(_CONFIG, _early_profile())


def _cfg_str(env_key: str, key: str, default: str) -> str:
    v = os.environ.get(env_key, "").strip()
    if v:
        return v
    cv = _PROFILE.get(key)
    return str(cv).strip() if cv not in (None, "") else default


def _cfg_float(env_key: str, key: str, default: float) -> float:
    v = os.environ.get(env_key, "").strip()
    raw = v if v else _PROFILE.get(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        die(f"invalid number for {env_key}/{key}: {raw!r}")


def _cfg_bool(env_key: str, key: str, default: bool) -> bool:
    v = os.environ.get(env_key, "").strip()
    if v:
        return v.lower() in ("1", "true", "yes", "on")
    cv = _PROFILE.get(key)
    if cv in (None, ""):
        return default
    if isinstance(cv, bool):
        return cv
    return str(cv).strip().lower() in ("1", "true", "yes", "on")


DEFAULT_TIMEOUT = _cfg_float("MYSQL_TIMEOUT_SECONDS", "timeout_seconds", 30.0)
DEFAULT_CACHE_TTL_DAYS = _cfg_float("MYSQL_CACHE_TTL_DAYS", "cache_ttl_days", 30.0)
SSL_ENABLED = _cfg_bool("MYSQL_SSL", "ssl", False)
SSL_VERIFY = _cfg_bool("MYSQL_SSL_VERIFY", "ssl_verify", True)
_BASE_CACHE_DIR = os.path.expanduser(_cfg_str("MYSQL_CACHE_DIR", "cache_dir", "~/.mysql/cache"))
# namespace per profile only when profiles are actually in use
CACHE_DIR = os.path.join(_BASE_CACHE_DIR, PROFILE_NAME) if USES_PROFILES else _BASE_CACHE_DIR


@dataclass
class Creds:
    host: str
    port: int
    user: str
    password: str
    database: str


def load_creds() -> Creds:
    host = os.environ.get("MYSQL_HOST", "") or _PROFILE.get("host", "")
    user = os.environ.get("MYSQL_USER", "") or _PROFILE.get("user", "")
    pw = os.environ.get("MYSQL_PASSWORD", "")
    if not pw and _PROFILE.get("password") is not None:
        pw = str(_PROFILE.get("password"))
    db = os.environ.get("MYSQL_DATABASE", "") or str(_PROFILE.get("database", "") or "")
    port_raw = os.environ.get("MYSQL_PORT", "") or _PROFILE.get("port", 3306)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        die(f"invalid port: {port_raw!r}")
    missing = [k for k, v in (("MYSQL_HOST", host), ("MYSQL_USER", user)) if not v]
    if missing:
        die("missing mysql credentials: " + ", ".join(missing)
            + f". Set the env vars or populate {CONFIG_PATH} (profile {PROFILE_NAME!r}).")
    return Creds(host=host, port=port, user=user, password=pw, database=db)


# ── auth scrambles ─────────────────────────────────────────────────────────

def _sha1(b: bytes) -> bytes:
    return hashlib.sha1(b).digest()


def _scramble_native(password: bytes, salt: bytes) -> bytes:
    """mysql_native_password: SHA1(pw) XOR SHA1(salt + SHA1(SHA1(pw)))."""
    if not password:
        return b""
    s1 = _sha1(password)
    s3 = _sha1(salt + _sha1(s1))
    return bytes(a ^ b for a, b in zip(s1, s3))


def _scramble_sha2(password: bytes, salt: bytes) -> bytes:
    """caching_sha2_password fast-auth scramble:
    SHA256(pw) XOR SHA256(SHA256(SHA256(pw)) + salt)."""
    if not password:
        return b""
    p1 = hashlib.sha256(password).digest()
    p2 = hashlib.sha256(p1).digest()
    p3 = hashlib.sha256(p2 + salt).digest()
    return bytes(a ^ b for a, b in zip(p1, p3))


# ── MySQL wire protocol ─────────────────────────────────────────────────────

def _lenc_int(data: bytes, pos: int) -> tuple[int, int]:
    n = data[pos]
    if n < 0xFB:
        return n, pos + 1
    if n == 0xFC:
        return struct.unpack_from("<H", data, pos + 1)[0], pos + 3
    if n == 0xFD:
        return int.from_bytes(data[pos + 1:pos + 4], "little"), pos + 4
    if n == 0xFE:
        return struct.unpack_from("<Q", data, pos + 1)[0], pos + 9
    return 0, pos + 1  # 0xFB (NULL) — not expected where ints are read


def _lenc_str(data: bytes, pos: int) -> tuple[bytes, int]:
    length, pos = _lenc_int(data, pos)
    return data[pos:pos + length], pos + length


class Client:
    """Single-connection MySQL client. Speaks just enough of the protocol to
    authenticate (native + caching_sha2, optionally over TLS) and run
    text-protocol queries."""

    def __init__(self, creds: Creds, timeout: float = 30.0,
                 use_ssl: bool = False, ssl_verify: bool = True) -> None:
        self.creds = creds
        self.timeout = timeout
        self.use_ssl = use_ssl
        self.ssl_verify = ssl_verify
        self.is_tls = False
        self.sock: socket.socket | None = None
        self._seq = 0

    # ── framing ──
    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                die("connection closed by server mid-packet")
            buf += chunk
        return buf

    def _read_packet(self) -> bytes:
        header = self._recv(4)
        length = header[0] | (header[1] << 8) | (header[2] << 16)
        self._seq = header[3]
        payload = self._recv(length)
        while length == 0xFFFFFF:  # multi-packet payload
            h = self._recv(4)
            length = h[0] | (h[1] << 8) | (h[2] << 16)
            self._seq = h[3]
            payload += self._recv(length)
        return payload

    def _write_packet(self, payload: bytes, seq: int) -> None:
        n = len(payload)
        header = bytes([n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, seq & 0xFF])
        self.sock.sendall(header + payload)

    def _err(self, pkt: bytes) -> str:
        code = struct.unpack_from("<H", pkt, 1)[0]
        rest = pkt[3:]
        if rest[:1] == b"#":  # '#SQLSTATE' marker
            rest = pkt[9:]
        return f"MySQL error {code}: {rest.decode('utf-8', 'replace')}"

    # ── connect + auth ──
    def _wrap_tls(self) -> None:
        ctx = ssl.create_default_context()
        if not self.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            self.sock = ctx.wrap_socket(self.sock, server_hostname=self.creds.host)
        except ssl.SSLError as e:
            die(f"TLS handshake with {self.creds.host} failed: {e}. "
                "For a self-signed cert set MYSQL_SSL_VERIFY=0 (or ssl_verify:false).")
        self.is_tls = True

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.creds.host, self.creds.port), timeout=self.timeout)
        except OSError as e:
            die(f"cannot connect to {self.creds.host}:{self.creds.port}: {e}")
        self.sock.settimeout(self.timeout)

        data = self._read_packet()
        if data[:1] == b"\xff":
            die(self._err(data))
        i = 1  # skip protocol version
        end = data.index(b"\x00", i)
        i = end + 1                                   # server version
        i += 4                                        # connection id
        auth1 = data[i:i + 8]; i += 8
        i += 1                                        # filler
        cap_low = struct.unpack_from("<H", data, i)[0]; i += 2
        auth2 = b""
        server_plugin = NATIVE
        if len(data) > i:
            i += 1                                    # charset
            i += 2                                    # status
            cap_high = struct.unpack_from("<H", data, i)[0]; i += 2
            caps = cap_low | (cap_high << 16)
            auth_len = data[i]; i += 1
            i += 10                                   # reserved
            if caps & CLIENT_SECURE_CONNECTION:
                ln = max(13, auth_len - 8)
                auth2 = data[i:i + ln]; i += ln
            if caps & CLIENT_PLUGIN_AUTH:
                z = data.find(b"\x00", i)
                server_plugin = data[i:(z if z >= 0 else len(data))].decode("utf-8", "replace")
        salt = (auth1 + auth2)[:20]

        flags = (CLIENT_PROTOCOL_41 | CLIENT_SECURE_CONNECTION | CLIENT_PLUGIN_AUTH
                 | CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG)
        if self.creds.database:
            flags |= CLIENT_CONNECT_WITH_DB

        seq = self._seq  # 0 after reading the handshake
        if self.use_ssl:
            flags |= CLIENT_SSL
            # SSLRequest: caps + max packet + charset + 23 reserved, no username
            ssl_req = struct.pack("<I", flags) + struct.pack("<I", 16 * 1024 * 1024) + bytes([45]) + b"\x00" * 23
            self._write_packet(ssl_req, seq + 1)
            seq += 1
            self._wrap_tls()

        # Always respond with mysql_native_password in the initial handshake
        # (using the handshake nonce). If the account actually uses
        # caching_sha2_password the server replies with an AuthSwitchRequest,
        # which _finish_auth handles. This mirrors the proven starrocks flow
        # and avoids a class of switch-ordering issues.
        del server_plugin
        plugin = NATIVE
        auth = self._scramble_for(plugin, salt)
        resp = struct.pack("<I", flags) + struct.pack("<I", 16 * 1024 * 1024) + bytes([45]) + b"\x00" * 23
        resp += self.creds.user.encode() + b"\x00"
        resp += bytes([len(auth)]) + auth
        if self.creds.database:
            resp += self.creds.database.encode() + b"\x00"
        resp += plugin.encode() + b"\x00"
        self._write_packet(resp, seq + 1)

        self._finish_auth()

    def _scramble_for(self, plugin: str, salt: bytes) -> bytes:
        if plugin == SHA2:
            return _scramble_sha2(self.creds.password.encode(), salt)
        if plugin == NATIVE:
            return _scramble_native(self.creds.password.encode(), salt)
        die(f"unsupported auth plugin {plugin!r}; this client only handles "
            f"{NATIVE} and {SHA2}.")

    def _finish_auth(self) -> None:
        """Drive the auth handshake to completion: handle AuthSwitchRequest,
        caching_sha2 fast/full-auth, OK and error packets."""
        while True:
            pkt = self._read_packet()
            head = pkt[:1]
            if head == b"\xff":
                die(self._err(pkt))
            if head == b"\x00":            # OK → authenticated
                return
            if head == b"\xfe":            # AuthSwitchRequest
                z = pkt.index(b"\x00", 1)
                new_plugin = pkt[1:z].decode("utf-8", "replace")
                new_salt = pkt[z + 1:].rstrip(b"\x00")[:20]
                self._write_packet(self._scramble_for(new_plugin, new_salt), self._seq + 1)
                continue
            if head == b"\x01":            # AuthMoreData (caching_sha2)
                marker = pkt[1] if len(pkt) > 1 else 0
                if marker == 0x03:         # fast_auth_success → OK follows
                    continue
                if marker == 0x04:         # full auth required → needs secure channel
                    if not self.is_tls:
                        raise _NeedTLS()
                    self._write_packet(self.creds.password.encode() + b"\x00", self._seq + 1)
                    continue
                die(f"unexpected caching_sha2 auth-more-data marker {marker:#x}")
            die(f"unexpected auth packet header {head!r}")

    def query(self, sql: str) -> dict[str, Any]:
        self._write_packet(b"\x03" + sql.encode("utf-8"), 0)
        pkt = self._read_packet()
        if pkt[:1] == b"\xff":
            die(self._err(pkt))
        if pkt[:1] == b"\x00":  # OK packet, no result set
            pos = 1
            affected, pos = _lenc_int(pkt, pos)
            return {"is_table": False, "columns": [], "rows": None, "affected_rows": affected, "text": ""}
        if pkt[0] == 0xFB:
            die("LOCAL INFILE responses are not supported")
        col_count, _ = _lenc_int(pkt, 0)
        columns: list[str] = []
        for _ in range(col_count):
            cdef = self._read_packet()
            pos = 0
            for _ in range(4):  # catalog, schema, table, org_table
                _, pos = _lenc_str(cdef, pos)
            name, pos = _lenc_str(cdef, pos)
            columns.append(name.decode("utf-8", "replace"))
        self._read_packet()  # EOF after column defs
        rows: list[dict[str, Any]] = []
        while True:
            rp = self._read_packet()
            if rp[:1] == b"\xfe" and len(rp) < 9:  # EOF
                break
            if rp[:1] == b"\xff":
                die(self._err(rp))
            vals: list[Any] = []
            pos = 0
            for _ in range(col_count):
                if rp[pos] == 0xFB:
                    vals.append(None); pos += 1
                else:
                    raw, pos = _lenc_str(rp, pos)
                    vals.append(raw.decode("utf-8", "replace"))
            rows.append(dict(zip(columns, vals)))
        return {"is_table": True, "columns": columns, "rows": rows, "text": ""}

    def close(self) -> None:
        if self.sock:
            try:
                self._write_packet(b"\x01", 0)  # COM_QUIT
            except OSError:
                pass
            self.sock.close()
            self.sock = None


def connect_client() -> Client:
    """Connect and authenticate, transparently retrying once over TLS if a
    caching_sha2 cold-cache login demands full auth on a plain socket."""
    creds = load_creds()
    c = Client(creds, DEFAULT_TIMEOUT, use_ssl=SSL_ENABLED, ssl_verify=SSL_VERIFY)
    try:
        c.connect()
        return c
    except _NeedTLS:
        c.close()
        c2 = Client(creds, DEFAULT_TIMEOUT, use_ssl=True, ssl_verify=SSL_VERIFY)
        try:
            c2.connect()
        except _NeedTLS:
            die("caching_sha2_password full auth still required after TLS upgrade — "
                "unexpected; check the account and server TLS config.")
        return c2


def emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


# ── commands ──────────────────────────────────────────────────────────────

def cmd_test_conn(_args: argparse.Namespace) -> None:
    import time
    started = time.monotonic()
    c = connect_client()
    res = c.query("SELECT 1")
    tls = c.is_tls
    c.close()
    emit({"ok": True, "profile": PROFILE_NAME, "tls": tls, "result": res.get("rows"),
          "elapsed_seconds": round(time.monotonic() - started, 3)})


def cmd_query(args: argparse.Namespace) -> None:
    c = connect_client()
    res = c.query(args.sql)
    c.close()
    emit(res)


# ── metadata cache (per-profile namespaced; see CACHE_DIR) ────────────────

def _table_ok(table: str) -> str:
    t = (table or "").strip()
    if not _TABLE_RE.match(t):
        die(f"invalid table name {table!r}: expected db.table (letters, digits, _ and .)")
    return t


def _cache_path(table: str) -> str:
    return os.path.join(CACHE_DIR, f"{table.replace(os.sep, '_')}.json")


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


def _fetch_table_meta(c: Client, table: str, limit: int) -> dict[str, Any]:
    desc = c.query(f"DESCRIBE {table}")
    columns = []
    for row in desc.get("rows") or []:
        # MySQL DESCRIBE → Field / Type / Null / Key / Default / Extra
        columns.append({
            "name": (row.get("Field") or "").strip(),
            "type": (row.get("Type") or "").strip(),
            "key": (row.get("Key") or "").strip(),
        })
    sample = c.query(f"SELECT * FROM {table} LIMIT {limit}")
    rows = sample.get("rows") or []
    return {
        "table": table,
        "profile": PROFILE_NAME,
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
        emit({"table": table, "profile": PROFILE_NAME, "status": "miss"})
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
                entry["status"] = "fresh"
                entry["age_days"] = age
                emit(entry)
                return
    c = connect_client()
    entry = _fetch_table_meta(c, table, args.limit)
    c.close()
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(table)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2, default=str)
    entry["status"] = "written"
    entry["path"] = path
    emit(entry)


def cmd_cache_list(_args: argparse.Namespace) -> None:
    out: list[dict[str, Any]] = []
    if os.path.isdir(CACHE_DIR):
        for fn in sorted(os.listdir(CACHE_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(CACHE_DIR, fn), "r", encoding="utf-8") as f:
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
    emit({"cache_dir": CACHE_DIR, "profile": PROFILE_NAME, "ttl_days": DEFAULT_CACHE_TTL_DAYS, "tables": out})


def cmd_cache_clear(args: argparse.Namespace) -> None:
    if not args.table and not args.all:
        die("cache clear: pass --table <db.table> or --all")
    removed: list[str] = []
    if args.all:
        if os.path.isdir(CACHE_DIR):
            for fn in os.listdir(CACHE_DIR):
                if fn.endswith(".json"):
                    os.remove(os.path.join(CACHE_DIR, fn))
                    removed.append(fn[:-5])
    else:
        table = _table_ok(args.table)
        path = _cache_path(table)
        if os.path.exists(path):
            os.remove(path)
            removed.append(table)
    emit({"ok": True, "profile": PROFILE_NAME, "removed": removed})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MySQL CLI for Claude Code skill")
    p.add_argument("--profile", default="", help="config profile to use (else MYSQL_PROFILE / default_profile)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test-conn", help="connect + SELECT 1").set_defaults(func=cmd_test_conn)

    sp = sub.add_parser("query", help="run one SQL statement")
    sp.add_argument("--sql", required=True)
    sp.set_defaults(func=cmd_query)

    cache = sub.add_parser("cache", help="schema + data-sample cache for reviewed tables")
    csub = cache.add_subparsers(dest="cache_cmd", required=True)

    cg = csub.add_parser("get", help="read a cached table entry (hit/stale/miss)")
    cg.add_argument("--table", required=True, help="db.table")
    cg.add_argument("--max-age-days", type=float, default=None)
    cg.set_defaults(func=cmd_cache_get)

    cp = csub.add_parser("put", help="cache a table's schema + sample (skips if fresh unless --force)")
    cp.add_argument("--table", required=True, help="db.table")
    cp.add_argument("--force", action="store_true")
    cp.add_argument("--limit", type=int, default=DEFAULT_SAMPLE_ROWS)
    cp.set_defaults(func=cmd_cache_put)

    csub.add_parser("list", help="list cached tables with freshness").set_defaults(func=cmd_cache_list)

    cc = csub.add_parser("clear", help="evict cache entries")
    cc.add_argument("--table", default="")
    cc.add_argument("--all", action="store_true")
    cc.set_defaults(func=cmd_cache_clear)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
