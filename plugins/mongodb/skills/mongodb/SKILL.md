---
name: mongodb
description: Query and inspect MongoDB collections with built-in risk control. Use this skill whenever the user wants to find, aggregate, count, or otherwise inspect MongoDB data — it handles connection (URI, supports Atlas SRV), classifies risk before execution (asking to confirm writes / admin ops), preserves BSON types in output, and caches per-collection schema + samples.
---

# MongoDB Skill

You execute MongoDB ops on the user's behalf via one helper script.
Everything you do MUST follow the workflow in this file. Do not skip the
risk gate, do not bypass the AskUserQuestion confirmation.

Unlike the other data plugins, this one needs `pymongo` (driver). If
`test-conn` says it's missing, tell the user to run:

```bash
pip install -r "$CLAUDE_PLUGIN_ROOT/scripts/requirements.txt"
```

## Tools you have

- `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mongodb.py <subcmd>` — MongoDB CLI.
  All output is JSON on stdout. Exit code != 0 means failure.
- The risk classifier is **built into the CLI** (`classify` subcommand) —
  MongoDB ops are JSON specs, not SQL, so the shared `risk.py` doesn't apply.

## Connection & profiles

Settings come from env (preferred) or `~/.taku/mongodb.json` (legacy
`~/.mongodb/config.json` still read; override the dir with `TAKU_DIR`).
The config can be a single flat connection, or a `profiles` map for
multiple environments — same shape as the starrocks/mysql plugins:

```json
{
  "default_profile": "prod",
  "profiles": {
    "prod":    { "uri": "mongodb+srv://u:p@cluster.mongodb.net/", "database": "app" },
    "staging": { "uri": "mongodb://u:p@stage:27017/",             "database": "app" }
  },
  "cache_ttl_days": 30
}
```

- Pick a profile with `--profile NAME` or `MONGODB_PROFILE`; else `default_profile`,
  else the sole profile. A flat config (no `profiles`) is the implicit `default`.
- Top-level keys outside `profiles` are shared defaults merged into each profile.
- The metadata cache is namespaced per profile, so prod/staging samples never collide.

```
env var                       profile key       default
MONGODB_URI                   uri               (required; mongodb://… or mongodb+srv://…)
MONGODB_DATABASE              database          (optional default db)
MONGODB_TIMEOUT_SECONDS       timeout_seconds   30
MONGODB_CACHE_DIR             cache_dir         ~/.mongodb/cache
MONGODB_CACHE_TTL_DAYS        cache_ttl_days    30
```

Do NOT prompt the user for passwords — they put them in the URI in env or
the config file. The CLI redacts the URI password from error messages.

## Spec shape (what to pass to `query` / `classify`)

`--spec` is a JSON object. Pick one `op`; pass extra keys per the op:

```json
// reads
{"op":"find",            "db":"x","collection":"y","filter":{...},"projection":{...},"sort":{"a":-1},"limit":20,"skip":0}
{"op":"count",           "db":"x","collection":"y","filter":{...}}
{"op":"distinct",        "db":"x","collection":"y","field":"name","filter":{...}}
{"op":"aggregate",       "db":"x","collection":"y","pipeline":[...], "limit":100}
{"op":"listCollections", "db":"x"}
{"op":"listDatabases"}
{"op":"ping",            "db":"x"}

// writes (always above the auto-approve threshold — confirm first)
{"op":"insertOne",   "db":"x","collection":"y","document":{...}}
{"op":"insertMany",  "db":"x","collection":"y","documents":[...]}
{"op":"updateOne",   "db":"x","collection":"y","filter":{...},"update":{"$set":{...}},"upsert":false}
{"op":"updateMany",  "db":"x","collection":"y","filter":{...},"update":{"$set":{...}}}
{"op":"deleteOne",   "db":"x","collection":"y","filter":{...}}
{"op":"deleteMany",  "db":"x","collection":"y","filter":{...}}

// escape hatch — runs db.command(...)
{"op":"runCommand",  "db":"x","command":{"<cmdName>":...}}
```

Notes:
- `db` is optional if set via `MONGODB_DATABASE`, the config, or the URI.
- For reads, you SHOULD almost always set `limit`. `find` defaults to 100,
  `aggregate` appends an implicit `$limit: 100` when the pipeline lacks one.
- Output preserves BSON types via MongoDB Extended JSON: `ObjectId` →
  `{"$oid": "..."}`, `Date` → `{"$date": "..."}`, `Decimal128` → `{"$numberDecimal": "..."}`.

## Workflow (mandatory)

Every spec you run goes through these steps. No exceptions.

### 1. Classify risk

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mongodb.py classify --spec "$SPEC"
```

Output: `{"level":"safe|low|medium|high","factors":[...],"operations":[...]}`.

Built-in rules:
- `safe` — metadata ops (ping/listCollections/listDatabases); read ops (find/count/distinct/aggregate) with explicit `limit ≤ 1000`
- `low`  — same reads without `limit`, or `limit > 1000`; non-admin `runCommand`
- `medium` — `insertOne`/`insertMany`; `updateOne`/`deleteOne` with a non-empty filter
- `high` — `updateMany`/`deleteMany` (and `updateOne`/`deleteOne`) **without** a filter; `runCommand` for `drop`/`dropDatabase`/`renameCollection`/`shutdown`/`createUser`/`dropUser`/`createIndex`/`dropIndex`/`compact`/`shardCollection`

**Then layer your own judgment on top.** The classifier is a floor, not a
ceiling. Read the actual spec and consider:
- Does the collection look like production (no `dev_`/`test_`/`tmp_`/`stg_` prefix)?
- Is it core domain data other services depend on?
- PII / financial / auth collections?
- A filter that *technically* exists but matches all docs (e.g. `{"deleted":{"$ne":null}}`)?

If any apply, **upgrade** the level and append your reasoning. Never downgrade.

### 2. Apply the auto-approve threshold

The user controls the gate via `MONGODB_AUTO_APPROVE_LEVEL` (default `safe`):

| Threshold (env)  | Auto-runs without asking |
| ---------------- | ------------------------ |
| `safe` (default) | only `safe`              |
| `low`            | `safe`, `low`            |
| `medium`         | `safe`, `low`, `medium`  |
| `high`           | everything (NOT recommended) |

If the final risk level is **above** the threshold, you MUST call
`AskUserQuestion` with the verbatim spec, the operations found, your
rationale, and the affected collections before running it. Do not
paraphrase the spec when asking.

### 3. Execute and report

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mongodb.py query --spec "$SPEC"
```

Output JSON shape (read ops):
```json
{"op": "find", "rows": [{...}, ...], "row_count": N}
```

Report to the user:
- read with rows → show first 20 as a markdown table; mention `row_count`
- `count`/`distinct` → report the number/values
- writes → report `inserted_id`/`matched`/`modified`/`deleted`
- On error: the CLI exits non-zero with the pymongo error on stderr — show it and offer to diagnose / retry (re-classify any new spec through the gate).

## Collection metadata cache

When you review data involving real collections, cache each collection's
inferred schema, indexes, and a 10-doc sample so later runs can confirm
shape without re-querying.

**Before** querying a collection you're unsure about:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mongodb.py cache get --collection app.orders
# -> {"status": "hit"|"stale"|"miss", "fields": [...], "indexes": [...], "sample": [...], ...}
```
`hit` → use cached schema/sample. `stale`/`miss` → query, then `cache put`.

**After** confirming a collection:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mongodb.py cache put --collection app.orders
```
`cache put` runs `estimatedDocumentCount` + `listIndexes` + `find().limit(10)`,
infers a per-field type set from the sample, and is idempotent (returns
`fresh` without re-querying if still within TTL); `--force` refreshes on
demand. Also: `cache list`, `cache clear --collection db.coll | --all`.
Cache is per-profile, so the same `db.coll` in prod vs staging is stored
separately.

Notes:
- The cache stores **real sample docs in plaintext** on disk. Don't echo
  large samples back unless asked — summarize.
- Pass `--collection db.coll` (validated); use the fully-qualified name.
- `cache get`/`list`/`clear` are local-only (no connection); only `put` connects.

## What NOT to do

- Do NOT execute anything above the threshold without confirmation.
- Do NOT ever print or log credentials. The CLI redacts URIs from errors.
- Do NOT silently retry a spec that errored — surface it, offer a fix, and
  re-classify any new spec through the risk gate.
- Do NOT smuggle JavaScript / `$where` expressions in filters without
  flagging them — they execute server-side code; upgrade the risk level.

## Quick sanity check

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mongodb.py test-conn
# {"ok": true, "profile": "...", "version": "7.0.x", "elapsed_seconds": 0.04}
```
