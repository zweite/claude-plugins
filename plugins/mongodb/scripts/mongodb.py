#!/usr/bin/env python3
"""
mongodb.py — MongoDB CLI for the Claude Code `mongodb` skill.

Depends on pymongo (and dnspython for mongodb+srv:// / Atlas URIs):
    pip install -r "$CLAUDE_PLUGIN_ROOT/scripts/requirements.txt"

Commands:
  test-conn                                — connect + {ping: 1}
  query    --spec '<JSON>'                 — run one op (see SKILL.md for shape)
  classify --spec '<JSON>'                 — rule-based risk level (no connection)
  cache    get   --collection db.coll [--max-age-days N]
  cache    put   --collection db.coll [--force] [--limit N]
  cache    list
  cache    clear --collection db.coll | --all

All output is JSON on stdout (BSON values via Extended JSON — ObjectId as
{"$oid":"…"}, Date as {"$date":"…"}, etc). Errors go to stderr; exit code
!= 0 on failure.

Settings come from env (preferred) or ~/.taku/mongodb.json (legacy
~/.mongodb/config.json still read; override the dir with TAKU_DIR). The
config may hold a single flat connection OR a `profiles` map for multiple
environments — same shape as the starrocks/mysql plugins:

  {
    "default_profile": "prod",
    "profiles": {
      "prod":    {"uri": "mongodb+srv://u:p@cluster.mongodb.net/", "database": "app"},
      "staging": {"uri": "mongodb://u:p@stage:27017/",             "database": "app"}
    },
    "cache_ttl_days": 30
  }

Pick a profile with --profile NAME or MONGODB_PROFILE; otherwise
default_profile, else the sole profile. The metadata cache is namespaced
per profile so prod/staging samples never collide.

  env var                       profile/config key       default
  MONGODB_URI                   uri                      (required)
  MONGODB_DATABASE              database                 (optional; default db)
  MONGODB_PROFILE               —                        (profile selector)
  MONGODB_TIMEOUT_SECONDS       timeout_seconds          30
  MONGODB_CACHE_DIR             cache_dir                ~/.mongodb/cache
  MONGODB_CACHE_TTL_DAYS        cache_ttl_days           30
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
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


CONFIG_PATH = _config_path("mongodb", "~/.mongodb/config.json")
DEFAULT_SAMPLE_DOCS = 10
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

# Mask `user:pw@` in mongodb:// URIs anywhere they appear in error text.
_URI_PWD_RE = re.compile(r'(mongodb(?:\+srv)?://[^:/?#@\s]*:)([^@\s]+)(@)')
_JSON_PWD_RE = re.compile(r'("(?:password|uri)"\s*:\s*")[^"]*(")')


def redact(s: str) -> str:
    s = _URI_PWD_RE.sub(r"\1[REDACTED]\3", s)
    return _JSON_PWD_RE.sub(r"\1[REDACTED]\2", s)


def die(msg: str, code: int = 1) -> None:
    print(redact(msg), file=sys.stderr)
    sys.exit(code)


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
    return os.environ.get("MONGODB_PROFILE", "").strip()


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


DEFAULT_TIMEOUT = _cfg_float("MONGODB_TIMEOUT_SECONDS", "timeout_seconds", 30.0)
DEFAULT_CACHE_TTL_DAYS = _cfg_float("MONGODB_CACHE_TTL_DAYS", "cache_ttl_days", 30.0)
_BASE_CACHE_DIR = os.path.expanduser(_cfg_str("MONGODB_CACHE_DIR", "cache_dir", "~/.mongodb/cache"))
CACHE_DIR = os.path.join(_BASE_CACHE_DIR, PROFILE_NAME) if USES_PROFILES else _BASE_CACHE_DIR


def _get_uri_db() -> tuple[str, str]:
    uri = os.environ.get("MONGODB_URI", "").strip() or str(_PROFILE.get("uri", "") or "").strip()
    db = os.environ.get("MONGODB_DATABASE", "").strip() or str(_PROFILE.get("database", "") or "").strip()
    if not uri:
        die(f"missing MongoDB URI. Set MONGODB_URI or 'uri' in {CONFIG_PATH} "
            f"(profile {PROFILE_NAME!r}).")
    return uri, db


# ── risk classification (pure stdlib, no pymongo) ────────────────────────

_META_OPS = {"ping", "listDatabases", "listCollections"}
_READ_OPS = {"find", "count", "distinct", "aggregate"}
_LIGHT_WRITE = {"insertOne", "updateOne", "deleteOne"}
_BULK_WRITE = {"insertMany", "updateMany", "deleteMany"}
_ALL_OPS = _META_OPS | _READ_OPS | _LIGHT_WRITE | _BULK_WRITE | {"runCommand"}
_ADMIN_HIGH = {
    "drop", "dropDatabase", "renameCollection", "shutdown",
    "createUser", "dropUser", "updateUser",
    "createIndex", "dropIndex", "dropIndexes",
    "compact", "shardCollection",
}


def _pipeline_limit(pipeline: list[Any]) -> int | None:
    """Return the first explicit $limit in an aggregation pipeline, else None."""
    if not isinstance(pipeline, list):
        return None
    for stage in pipeline:
        if isinstance(stage, dict) and "$limit" in stage:
            v = stage["$limit"]
            return v if isinstance(v, int) else None
    return None


def classify(spec: dict[str, Any]) -> dict[str, Any]:
    """Rule-based risk classifier for a MongoDB op spec. Mirrors the
    safe/low/medium/high vocabulary of risk.py used by the other plugins,
    but with mongo-aware factors. Conservative: false positives just mean
    the user is asked to confirm."""
    if not isinstance(spec, dict):
        return {"level": "high", "factors": ["spec_not_object"], "operations": []}
    op = str(spec.get("op") or "").strip()
    factors: list[str] = []
    operations: list[str] = [op] if op else []
    if not op:
        return {"level": "high", "factors": ["missing_op"], "operations": []}
    if op not in _ALL_OPS:
        return {"level": "low", "factors": ["unknown_op"], "operations": operations}

    if op in _META_OPS:
        return {"level": "safe", "factors": ["metadata_op"], "operations": operations}

    if op in _READ_OPS:
        if op == "aggregate":
            limit = _pipeline_limit(spec.get("pipeline") or [])
            if limit is None:
                limit = spec.get("limit")
        else:
            limit = spec.get("limit")
        if not isinstance(limit, int):
            factors.append("no_limit")
            return {"level": "low", "factors": factors, "operations": operations}
        if limit > 1000:
            factors.append("large_limit")
            return {"level": "low", "factors": factors, "operations": operations}
        return {"level": "safe", "factors": ["bounded_read"], "operations": operations}

    if op == "insertOne":
        return {"level": "medium", "factors": ["write_single"], "operations": operations}
    if op == "insertMany":
        return {"level": "medium", "factors": ["write_bulk_insert"], "operations": operations}

    if op in _LIGHT_WRITE:  # updateOne, deleteOne
        flt = spec.get("filter")
        if not isinstance(flt, dict) or not flt:
            return {"level": "high", "factors": ["write_no_filter"], "operations": operations}
        return {"level": "medium", "factors": ["write_with_filter"], "operations": operations}

    if op in _BULK_WRITE:  # updateMany, deleteMany
        flt = spec.get("filter")
        if not isinstance(flt, dict) or not flt:
            return {"level": "high", "factors": ["bulk_write_no_filter"], "operations": operations}
        return {"level": "medium", "factors": ["bulk_write_with_filter"], "operations": operations}

    if op == "runCommand":
        cmd = spec.get("command")
        if not isinstance(cmd, dict) or not cmd:
            return {"level": "low", "factors": ["empty_runcommand"], "operations": operations}
        top = next(iter(cmd))
        operations = [top]
        if top in _ADMIN_HIGH:
            return {"level": "high", "factors": [f"admin:{top}"], "operations": operations}
        return {"level": "low", "factors": [f"runcommand:{top}"], "operations": operations}

    return {"level": "low", "factors": ["unhandled_op"], "operations": operations}


# ── pymongo lazy import (so classify / cache get/list/clear work without it) ─

def _import_pymongo():
    try:
        import pymongo  # type: ignore
        from bson import json_util  # type: ignore
        return pymongo, json_util
    except ImportError as e:
        die(f"pymongo not installed ({e}). Run: "
            f"pip install -r \"$CLAUDE_PLUGIN_ROOT/scripts/requirements.txt\"")


def get_client():
    pymongo, _ = _import_pymongo()
    uri, _ = _get_uri_db()
    timeout_ms = max(1, int(DEFAULT_TIMEOUT * 1000))
    try:
        client = pymongo.MongoClient(
            uri,
            serverSelectionTimeoutMS=timeout_ms,
            socketTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
        )
        client.admin.command("ping")  # eager check
    except Exception as e:
        die(f"cannot connect: {type(e).__name__}: {e}")
    return client


def _resolve_db(client, spec_db: str) -> str:
    name = (spec_db or "").strip()
    if name:
        return name
    _, cfg_db = _get_uri_db()
    if cfg_db:
        return cfg_db
    # last resort: try the URI's default db
    try:
        default = client.get_default_database()
        return default.name
    except Exception:
        die("no database specified: set 'db' in spec, MONGODB_DATABASE env, "
            "'database' in config, or include /<db> in the URI.")


# ── output (BSON-safe) ───────────────────────────────────────────────────

def emit_bson(obj: Any) -> None:
    _, json_util = _import_pymongo()
    sys.stdout.write(json_util.dumps(obj, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")


def emit_plain(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


# ── spec dispatch ────────────────────────────────────────────────────────

def execute_spec(client, spec: dict[str, Any], default_limit: int = 100) -> dict[str, Any]:
    op = str(spec.get("op") or "").strip()
    if not op:
        die("spec: missing 'op'")
    db_name = _resolve_db(client, str(spec.get("db") or "").strip())

    if op == "ping":
        return {"op": op, "result": client[db_name].command({"ping": 1})}
    if op == "listDatabases":
        return {"op": op, "rows": sorted(client.list_database_names())}
    if op == "listCollections":
        return {"op": op, "db": db_name, "rows": sorted(client[db_name].list_collection_names())}

    coll_name = str(spec.get("collection") or "").strip()
    if not coll_name:
        die(f"{op}: 'collection' required in spec")
    coll = client[db_name][coll_name]

    if op == "find":
        flt = spec.get("filter") or {}
        proj = spec.get("projection")
        cur = coll.find(flt, proj)
        if spec.get("sort"):
            cur = cur.sort([(k, int(v)) for k, v in spec["sort"].items()])
        if spec.get("skip"):
            cur = cur.skip(int(spec["skip"]))
        limit = int(spec.get("limit", default_limit))
        cur = cur.limit(limit)
        rows = list(cur)
        return {"op": op, "rows": rows, "row_count": len(rows)}

    if op == "count":
        return {"op": op, "count": coll.count_documents(spec.get("filter") or {})}

    if op == "distinct":
        field = str(spec.get("field") or "").strip()
        if not field:
            die("distinct: 'field' required")
        return {"op": op, "field": field,
                "values": coll.distinct(field, spec.get("filter") or {})}

    if op == "aggregate":
        pipeline = spec.get("pipeline") or []
        if not isinstance(pipeline, list):
            die("aggregate: 'pipeline' must be a JSON array")
        # implicit cap unless the pipeline already has $limit
        if _pipeline_limit(pipeline) is None and spec.get("limit") is not False:
            pipeline = list(pipeline) + [{"$limit": int(spec.get("limit", default_limit))}]
        rows = list(coll.aggregate(pipeline))
        return {"op": op, "rows": rows, "row_count": len(rows)}

    if op == "insertOne":
        res = coll.insert_one(spec.get("document") or {})
        return {"op": op, "inserted_id": res.inserted_id}
    if op == "insertMany":
        res = coll.insert_many(spec.get("documents") or [])
        return {"op": op, "inserted_ids": list(res.inserted_ids),
                "inserted_count": len(res.inserted_ids)}
    if op == "updateOne":
        res = coll.update_one(spec.get("filter") or {}, spec.get("update") or {},
                              upsert=bool(spec.get("upsert")))
        return {"op": op, "matched": res.matched_count, "modified": res.modified_count,
                "upserted_id": res.upserted_id}
    if op == "updateMany":
        res = coll.update_many(spec.get("filter") or {}, spec.get("update") or {},
                               upsert=bool(spec.get("upsert")))
        return {"op": op, "matched": res.matched_count, "modified": res.modified_count}
    if op == "deleteOne":
        res = coll.delete_one(spec.get("filter") or {})
        return {"op": op, "deleted": res.deleted_count}
    if op == "deleteMany":
        res = coll.delete_many(spec.get("filter") or {})
        return {"op": op, "deleted": res.deleted_count}

    if op == "runCommand":
        return {"op": op, "db": db_name,
                "result": client[db_name].command(spec.get("command") or {})}

    die(f"unknown op {op!r}")


# ── schema inference for cache ───────────────────────────────────────────

_PLAIN_TYPES = {
    str: "string", bool: "bool", int: "int", float: "double",
    list: "array", dict: "document", type(None): "null", bytes: "binary",
}


def _bson_type(v: Any) -> str:
    """Best-effort BSON type name: matches python primitives, falls back to
    the value's class name (catches ObjectId / Decimal128 / Datetime / Binary
    without importing bson)."""
    if isinstance(v, bool):  # bool first — bool is a subclass of int
        return "bool"
    return _PLAIN_TYPES.get(type(v)) or type(v).__name__


def infer_schema(docs: list[Any]) -> list[dict[str, Any]]:
    """Walk top-level fields of each doc; return [{path, types}] sorted by path."""
    fields: dict[str, set[str]] = {}
    for d in docs or []:
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            fields.setdefault(k, set()).add(_bson_type(v))
    return [{"path": k, "types": sorted(v)} for k, v in sorted(fields.items())]


# ── metadata cache (per-profile namespaced; see CACHE_DIR) ───────────────

def _collection_ok(coll: str) -> tuple[str, str]:
    """Validate 'db.collection' and return the (db, coll) pair."""
    s = (coll or "").strip()
    if "." not in s:
        die(f"--collection must be 'db.collection', got {coll!r}")
    db, _, c = s.partition(".")
    if not _NAME_RE.fullmatch(db) or not _NAME_RE.fullmatch(c):
        die(f"invalid collection name {coll!r}: letters, digits, _, . and - only")
    return db, c


def _cache_path(coll: str) -> str:
    safe = coll.replace(os.sep, "_")
    return os.path.join(CACHE_DIR, f"{safe}.json")


def _read_cache(coll: str) -> dict[str, Any] | None:
    path = _cache_path(coll)
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


def _fetch_collection_meta(client, db_name: str, coll_name: str, limit: int) -> dict[str, Any]:
    coll = client[db_name][coll_name]
    try:
        est = coll.estimated_document_count()
    except Exception:
        est = None
    try:
        indexes = list(coll.list_indexes())
    except Exception:
        indexes = []
    sample = list(coll.find().limit(limit))
    schema = infer_schema(sample)
    return {
        "collection": f"{db_name}.{coll_name}",
        "profile": PROFILE_NAME,
        "cached_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ttl_days": DEFAULT_CACHE_TTL_DAYS,
        "estimated_count": est,
        "indexes": indexes,
        "fields": schema,
        "sample": sample,
        "sample_row_count": len(sample),
    }


# ── commands ──────────────────────────────────────────────────────────────

def cmd_test_conn(_args: argparse.Namespace) -> None:
    import time
    started = time.monotonic()
    client = get_client()
    try:
        info = client.server_info()
    finally:
        client.close()
    emit_bson({
        "ok": True,
        "profile": PROFILE_NAME,
        "version": info.get("version"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    })


def cmd_query(args: argparse.Namespace) -> None:
    try:
        spec = json.loads(args.spec)
    except json.JSONDecodeError as e:
        die(f"--spec is not valid JSON: {e}")
    client = get_client()
    try:
        res = execute_spec(client, spec)
    finally:
        client.close()
    emit_bson(res)


def cmd_classify(args: argparse.Namespace) -> None:
    try:
        spec = json.loads(args.spec)
    except json.JSONDecodeError as e:
        die(f"--spec is not valid JSON: {e}")
    emit_plain(classify(spec))


def cmd_cache_get(args: argparse.Namespace) -> None:
    coll = args.collection.strip()
    _collection_ok(coll)
    max_age = args.max_age_days if args.max_age_days is not None else DEFAULT_CACHE_TTL_DAYS
    entry = _read_cache(coll)
    if entry is None:
        emit_plain({"collection": coll, "profile": PROFILE_NAME, "status": "miss"})
        return
    status, age = _freshness(entry, max_age)
    entry["status"] = status
    entry["age_days"] = age
    emit_plain(entry)  # cache file is already Extended JSON; pass-through


def cmd_cache_put(args: argparse.Namespace) -> None:
    coll = args.collection.strip()
    db_name, coll_name = _collection_ok(coll)
    if not args.force:
        entry = _read_cache(coll)
        if entry is not None:
            status, age = _freshness(entry, DEFAULT_CACHE_TTL_DAYS)
            if status == "hit":
                entry["status"] = "fresh"
                entry["age_days"] = age
                emit_plain(entry)
                return
    client = get_client()
    try:
        entry = _fetch_collection_meta(client, db_name, coll_name, args.limit)
    finally:
        client.close()
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(coll)
    _, json_util = _import_pymongo()  # serialize BSON values in indexes/sample
    with open(path, "w", encoding="utf-8") as f:
        f.write(json_util.dumps(entry, indent=2, ensure_ascii=False))
    entry["status"] = "written"
    entry["path"] = path
    emit_bson(entry)


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
                "collection": entry.get("collection", fn[:-5]),
                "cached_at": entry.get("cached_at"),
                "age_days": age,
                "status": status,
                "estimated_count": entry.get("estimated_count"),
                "fields": len(entry.get("fields") or []),
                "sample_rows": entry.get("sample_row_count", len(entry.get("sample") or [])),
            })
    emit_plain({"cache_dir": CACHE_DIR, "profile": PROFILE_NAME,
                "ttl_days": DEFAULT_CACHE_TTL_DAYS, "collections": out})


def cmd_cache_clear(args: argparse.Namespace) -> None:
    if not args.collection and not args.all:
        die("cache clear: pass --collection <db.coll> or --all")
    removed: list[str] = []
    if args.all:
        if os.path.isdir(CACHE_DIR):
            for fn in os.listdir(CACHE_DIR):
                if fn.endswith(".json"):
                    os.remove(os.path.join(CACHE_DIR, fn))
                    removed.append(fn[:-5])
    else:
        coll = args.collection.strip()
        _collection_ok(coll)
        path = _cache_path(coll)
        if os.path.exists(path):
            os.remove(path)
            removed.append(coll)
    emit_plain({"ok": True, "profile": PROFILE_NAME, "removed": removed})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MongoDB CLI for Claude Code skill")
    p.add_argument("--profile", default="", help="config profile to use (else MONGODB_PROFILE / default_profile)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test-conn", help="connect + ping").set_defaults(func=cmd_test_conn)

    sp = sub.add_parser("query", help="run one MongoDB op (JSON spec)")
    sp.add_argument("--spec", required=True, help="JSON spec — see SKILL.md")
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("classify", help="classify the risk of a spec (no connection)")
    sp.add_argument("--spec", required=True)
    sp.set_defaults(func=cmd_classify)

    cache = sub.add_parser("cache", help="schema + data-sample cache for reviewed collections")
    csub = cache.add_subparsers(dest="cache_cmd", required=True)

    cg = csub.add_parser("get", help="read a cached entry (hit/stale/miss)")
    cg.add_argument("--collection", required=True, help="db.collection")
    cg.add_argument("--max-age-days", type=float, default=None)
    cg.set_defaults(func=cmd_cache_get)

    cp = csub.add_parser("put", help="cache schema + N-doc sample (skips if fresh unless --force)")
    cp.add_argument("--collection", required=True, help="db.collection")
    cp.add_argument("--force", action="store_true")
    cp.add_argument("--limit", type=int, default=DEFAULT_SAMPLE_DOCS)
    cp.set_defaults(func=cmd_cache_put)

    csub.add_parser("list", help="list cached collections with freshness").set_defaults(func=cmd_cache_list)

    cc = csub.add_parser("clear", help="evict cache entries")
    cc.add_argument("--collection", default="")
    cc.add_argument("--all", action="store_true")
    cc.set_defaults(func=cmd_cache_clear)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
