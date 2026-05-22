#!/usr/bin/env python3
"""
risk.py — classify a Zeppelin paragraph (SQL or Spark code) into a risk
level for the `zeppelin` skill. Pure stdlib, regex-based; intentionally
conservative — false positives are cheap (the user is just asked to
confirm), false negatives are not.

Usage:
  echo "DROP TABLE foo" | python3 risk.py --magic %spark.sql

Output (stdout, JSON):
  {
    "level":     "safe" | "low" | "medium" | "high",
    "factors":   ["ddl_drop", ...],     # machine tags
    "rationale": "...",                  # one human sentence per factor
    "magic":     "%spark.sql",
    "operations":["DROP", "SELECT"]      # ops that the linter detected
  }

Levels are ordered safe < low < medium < high. Callers compare against a
threshold (see SKILL.md). The skill ALSO asks Claude to reason on top of
this — the classifier is a floor, not a ceiling.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Iterable

LEVEL_ORDER = {"safe": 0, "low": 1, "medium": 2, "high": 3}

# ── SQL pattern catalog ─────────────────────────────────────────────────
# Each pattern is (factor_tag, level, regex, human_sentence).
# Match is case-insensitive, dot does not span newlines unless re.S used.
SQL_PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    # ── DDL: always high ────────────────────────────────────────────────
    ("ddl_drop",          "high",   re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW|INDEX|FUNCTION)\b", re.I),
        "DROP statement removes a persistent object."),
    ("ddl_truncate",      "high",   re.compile(r"\bTRUNCATE\s+TABLE\b", re.I),
        "TRUNCATE wipes every row in the target table."),
    ("ddl_alter",         "high",   re.compile(r"\bALTER\s+(TABLE|DATABASE|SCHEMA|VIEW|INDEX|FUNCTION)\b", re.I),
        "ALTER changes a persistent object's schema."),
    ("ddl_create",        "high",   re.compile(r"\bCREATE\s+(TABLE|DATABASE|SCHEMA|VIEW|INDEX|FUNCTION|MATERIALIZED\s+VIEW)\b", re.I),
        "CREATE persists a new object that won't be auto-cleaned."),
    ("ddl_replace",       "high",   re.compile(r"\b(CREATE\s+OR\s+REPLACE|REPLACE)\s+(TABLE|VIEW|FUNCTION)\b", re.I),
        "REPLACE rewrites an existing object's definition."),
    ("ddl_grant_revoke",  "high",   re.compile(r"\b(GRANT|REVOKE)\b", re.I),
        "Permission change."),

    # ── DML: writes ─────────────────────────────────────────────────────
    ("dml_insert_overwrite", "high", re.compile(r"\bINSERT\s+OVERWRITE\b", re.I),
        "INSERT OVERWRITE replaces partition / table contents."),
    ("dml_insert",        "medium", re.compile(r"\bINSERT\s+(INTO|TABLE)\b", re.I),
        "INSERT writes new rows."),
    ("dml_merge",         "medium", re.compile(r"\bMERGE\s+INTO\b", re.I),
        "MERGE upserts rows."),

    # UPDATE / DELETE — initially flagged here; refined by has_where below.
    ("dml_update",        "medium", re.compile(r"\bUPDATE\s+\w+", re.I),
        "UPDATE modifies existing rows."),
    ("dml_delete",        "medium", re.compile(r"\bDELETE\s+FROM\b", re.I),
        "DELETE removes rows."),

    # ── reads ───────────────────────────────────────────────────────────
    ("read_select",       "safe",   re.compile(r"\bSELECT\b", re.I),
        "SELECT query."),
    ("read_show",         "safe",   re.compile(r"\b(SHOW|DESC(RIBE)?|EXPLAIN|USE)\b", re.I),
        "Read-only metadata / planner statement."),
]

# Spark code red flags (regardless of magic) — these get scanned for
# %spark, %pyspark, %scala, %sh paragraphs.
SPARK_PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    ("spark_write",       "high",   re.compile(r"\.write(?:Stream)?\s*[.(]", re.I),
        "Spark .write* call persists data."),
    ("spark_save_as_table","high",  re.compile(r"\.(?:saveAsTable|insertInto)\s*\(", re.I),
        "Spark saveAsTable / insertInto writes to the catalog."),
    ("spark_save",        "high",   re.compile(r"\.save\s*\(", re.I),
        "Spark .save() writes data to a path."),
    ("spark_sql_ddl",     "high",   re.compile(r"spark\.sql\s*\(\s*[\"'].*?\b(DROP|TRUNCATE|ALTER|CREATE|INSERT\s+OVERWRITE|GRANT|REVOKE)\b", re.I | re.S),
        "Embedded spark.sql() runs DDL / overwrite."),
    ("shell_rm",          "high",   re.compile(r"\brm\s+-rf?\b", re.I),
        "Shell rm -rf is destructive."),
    ("shell_hdfs_rm",     "high",   re.compile(r"\b(hdfs|hadoop)\s+dfs\s+-rm", re.I),
        "HDFS dfs -rm deletes paths."),
    ("shell_curl_post",   "medium", re.compile(r"\bcurl\b[^\n|]*\s-X\s*(POST|PUT|DELETE|PATCH)\b", re.I),
        "Shell HTTP write call."),
]

# Comments and strings stripper for SQL — coarse but good enough for
# block / line / single-quote / double-quote / backtick.
_STRIP_PATTERNS = [
    re.compile(r"--[^\n]*"),             # SQL line comment
    re.compile(r"#[^\n]*"),              # python / shell line comment
    re.compile(r"/\*.*?\*/", re.S),      # block comment
    re.compile(r"'(?:[^'\\]|\\.)*'", re.S),
    re.compile(r'"(?:[^"\\]|\\.)*"', re.S),
    re.compile(r"`[^`]*`"),              # MySQL identifier quoting
]

WHERE_RE = re.compile(r"\bWHERE\b", re.I)


def strip_noise(code: str) -> str:
    for p in _STRIP_PATTERNS:
        code = p.sub(" ", code)
    return code


def magic_kind(magic: str) -> str:
    m = (magic or "").strip().lower()
    if m.startswith("%spark.sql") or m.startswith("%sql"):
        return "sql"
    if m.startswith("%pyspark") or m.startswith("%spark") or m.startswith("%scala"):
        return "code"
    if m.startswith("%sh"):
        return "shell"
    # No magic / unknown — treat permissively as SQL first, code second.
    return "sql"


def has_where(stripped: str, around: re.Match[str]) -> bool:
    """Crude: any WHERE within ~400 chars *after* the UPDATE/DELETE keyword.
    Avoids treating a downstream WHERE in a different statement as guarding
    an earlier write."""
    tail = stripped[around.end():around.end() + 400]
    return bool(WHERE_RE.search(tail))


def classify(code: str, magic: str) -> dict:
    kind = magic_kind(magic)
    stripped = strip_noise(code)
    factors: list[tuple[str, str, str]] = []   # (tag, level, sentence)
    operations: list[str] = []

    def add(tag: str, level: str, sentence: str) -> None:
        if not any(t == tag for t, _, _ in factors):
            factors.append((tag, level, sentence))

    # Always scan SQL patterns — even Spark code often embeds SQL.
    for tag, level, pat, sentence in SQL_PATTERNS:
        m = pat.search(stripped)
        if not m:
            continue
        if tag == "dml_update":
            operations.append("UPDATE")
            if not has_where(stripped, m):
                add("dml_update_no_where", "high", "UPDATE has no WHERE clause — touches every row.")
            else:
                add(tag, "low", "UPDATE with WHERE clause.")
            continue
        if tag == "dml_delete":
            operations.append("DELETE")
            if not has_where(stripped, m):
                add("dml_delete_no_where", "high", "DELETE has no WHERE clause — wipes every row.")
            else:
                add(tag, "low", "DELETE with WHERE clause.")
            continue
        if tag == "read_select":
            operations.append("SELECT")
        elif tag == "read_show":
            operations.append(m.group(0).upper().split()[0])
        elif tag.startswith("ddl_"):
            operations.append(m.group(0).upper().split()[0])
        elif tag.startswith("dml_"):
            operations.append("INSERT" if "insert" in tag else "MERGE")
        add(tag, level, sentence)

    if kind in ("code", "shell"):
        for tag, level, pat, sentence in SPARK_PATTERNS:
            if pat.search(stripped):
                add(tag, level, sentence)

    if not factors:
        # Nothing recognized — be cautious for code/shell, permissive for
        # what looks like a bare SQL fragment.
        if kind == "sql":
            return {
                "level": "low",
                "factors": ["unrecognized_sql"],
                "rationale": "No known operation matched; treating as low-risk SQL. Ask Claude to re-read it.",
                "magic": magic,
                "operations": [],
            }
        return {
            "level": "medium",
            "factors": ["unrecognized_code"],
            "rationale": "Code paragraph with no recognized red flags; Claude should reason about side effects before submit.",
            "magic": magic,
            "operations": [],
        }

    top = max((LEVEL_ORDER[lvl] for _, lvl, _ in factors), default=0)
    level = next(name for name, ord_ in LEVEL_ORDER.items() if ord_ == top)
    return {
        "level": level,
        "factors": [t for t, _, _ in factors],
        "rationale": " ".join(s for _, _, s in factors),
        "magic": magic,
        "operations": sorted(set(operations)),
    }


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Risk-classify a Zeppelin paragraph body")
    ap.add_argument("--magic", default="", help="paragraph magic (e.g. %%spark.sql, %%pyspark, %%sh)")
    ap.add_argument("--code-file", default="", help="read code from this file instead of stdin")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.code_file:
        with open(args.code_file, "r", encoding="utf-8") as f:
            code = f.read()
    else:
        code = sys.stdin.read()

    json.dump(classify(code, args.magic), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
