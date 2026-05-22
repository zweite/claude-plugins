---
name: mysql
description: Run SQL against a MySQL server with built-in risk control. Use this skill whenever the user wants to query, transform, or inspect data in MySQL — it handles connection (both mysql_native_password and caching_sha2_password), risk classification before execution (asking to confirm high-risk operations), and a schema/sample cache.
---

# MySQL Skill

You execute SQL on a MySQL server on the user's behalf via two helper scripts.
Everything you do MUST follow the workflow in this file. Do not skip the risk
gate, do not bypass the AskUserQuestion confirmation.

The CLI is pure-Python (stdlib only — no driver, no `mysql` binary). It speaks
the MySQL wire protocol directly and supports both auth plugins:
`mysql_native_password` and `caching_sha2_password` (MySQL 8.0 default).
Default port 3306.

## Tools you have

- `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mysql.py <subcmd>` — MySQL CLI.
  All output is JSON on stdout. Exit code != 0 means failure.
- `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/risk.py --magic '%sql'` — risk classifier.
  Takes the SQL on stdin, prints JSON. (Shared with the zeppelin/starrocks skills.)

## Connection & profiles

Settings come from env (preferred) or `~/.taku/mysql.json` (legacy
`~/.mysql/config.json` still read; override the dir with `TAKU_DIR`). The config
can be a single flat connection, or a `profiles` map for multiple environments:

```json
{
  "default_profile": "prod",
  "profiles": {
    "prod":    { "host": "db-prod",    "port": 3306, "user": "u", "password": "p" },
    "staging": { "host": "db-staging", "user": "u", "password": "p", "ssl": true }
  },
  "cache_ttl_days": 30
}
```

- Pick a profile with `--profile NAME` or `MYSQL_PROFILE`; else `default_profile`,
  else the sole profile. A flat config (no `profiles`) is the implicit `default`.
- Top-level keys outside `profiles` are shared defaults merged into each profile.
- The metadata cache is namespaced per profile, so prod/staging samples never collide.

```
env var                  profile key       default
MYSQL_HOST               host              (required)
MYSQL_PORT               port              3306
MYSQL_USER               user              (required)
MYSQL_PASSWORD           password          (required)
MYSQL_DATABASE           database          (optional)
MYSQL_SSL                ssl               false   (force TLS from start)
MYSQL_SSL_VERIFY         ssl_verify        true    (verify server cert)
MYSQL_TIMEOUT_SECONDS    timeout_seconds   30
MYSQL_CACHE_DIR          cache_dir         ~/.mysql/cache
MYSQL_CACHE_TTL_DAYS     cache_ttl_days    30
```

### Auth notes

- `mysql_native_password` and the `caching_sha2_password` **fast path** (password
  already cached on the server) both work over a plain socket.
- A **first-ever login** for a `caching_sha2_password` account hits "full auth",
  which needs a secure channel. The client auto-upgrades to TLS and retries once.
  If TLS fails (e.g. self-signed cert), tell the user to set `MYSQL_SSL_VERIFY=0`
  (or `ssl_verify: false`). RSA-key auth is NOT supported (not in stdlib).
- Set `MYSQL_SSL=1` (or `ssl: true`) to force TLS from the start for servers with
  `require_secure_transport=ON`.

If a connection fails, run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mysql.py
test-conn` once to surface the exact error and tell the user what to set. Do NOT
prompt the user for passwords — they put them in env or the config file.

## Workflow (mandatory)

Every statement you run goes through these steps. No exceptions.

### 1. Classify risk

Pipe the SQL into the classifier (MySQL is SQL, so always `--magic '%sql'`):

```bash
echo "$SQL" | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/risk.py --magic '%sql'
```

Output: `{"level": "safe|low|medium|high", "factors": [...], "operations": [...]}`.

**Then layer your own judgment on top.** The classifier is a floor, not a ceiling.
Read the actual SQL and consider:
- Does the table look like production (no `dev_`/`test_`/`tmp_`/`stg_` prefix)?
- Is it a fact table or shared dimension other pipelines depend on?
- Unusual time window, suspiciously broad scan, financial / PII tables?

If any apply, **upgrade** the level and append your reasoning. Never downgrade.

### 2. Apply the auto-approve threshold

The user controls the gate via `MYSQL_AUTO_APPROVE_LEVEL` (default `safe`):

| Threshold (env)  | Auto-runs without asking |
| ---------------- | ------------------------ |
| `safe` (default) | only `safe`              |
| `low`            | `safe`, `low`            |
| `medium`         | `safe`, `low`, `medium`  |
| `high`           | everything (NOT recommended) |

If the final risk level is **above** the threshold, you MUST call `AskUserQuestion`
with the verbatim SQL, the operations found, your rationale, and the affected
tables before running it. Do not paraphrase the SQL when asking.

### 3. Execute and report

Once cleared (auto or after confirmation):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mysql.py query --sql "$SQL"
```

Output JSON shape:
```json
{ "is_table": true, "columns": ["..."], "rows": [{"col": "val"}, ...], "text": "" }
// non-SELECT (DDL/DML) → {"is_table": false, "affected_rows": N, "rows": null}
```

Report to the user:
- `is_table` → show first 20 rows as a markdown table; mention total row count.
- non-table → report `affected_rows`.
- On error: the CLI exits non-zero with the MySQL error on stderr — show it and offer to diagnose / retry (re-classify any new SQL through the gate).

## Table metadata cache

When you review data involving real tables, cache each table's schema and a
10-row sample so later runs can confirm columns/shape without re-querying.

**Before** querying a table you're unsure about:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mysql.py cache get --table mydb.orders
# -> {"status": "hit"|"stale"|"miss", "columns": [...], "sample": [...], "age_days": ...}
```
`hit` → use cached schema/sample. `stale`/`miss` → query, then `cache put`.

**After** confirming a table:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mysql.py cache put --table mydb.orders
```
`cache put` runs `DESCRIBE` + `SELECT * LIMIT 10`, idempotent (returns `fresh`
without re-querying if still within TTL); `--force` refreshes on demand. Also:
`cache list`, `cache clear --table db.t | --all`. Cache is per-profile, so the
same `db.table` in prod vs staging is stored separately.

Notes:
- The cache stores **real sample rows in plaintext** on disk. Don't echo large
  samples back unless asked — summarize.
- Pass `--table db.table` (validated); use the fully-qualified name.
- `cache get`/`list`/`clear` are local-only (no connection); only `put` connects.

## What NOT to do

- Do NOT execute anything above the threshold without confirmation.
- Do NOT ever print or log credentials. The CLI redacts passwords from errors.
- Do NOT silently retry a statement that errored — surface it, offer a fix, and
  re-classify any new SQL through the risk gate.

## Quick sanity check

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mysql.py test-conn
# {"ok": true, "profile": "...", "tls": false, "result": [{"1": "1"}], "elapsed_seconds": 0.05}
```
