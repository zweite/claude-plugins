# zweite-tools — Claude Code plugin marketplace

A self-hosted [Claude Code](https://code.claude.com) plugin marketplace.

## Install

```
/plugin marketplace add zweite/claude-plugins
/plugin install <plugin>@zweite-tools
```

## Plugins

| Plugin | Description |
| --- | --- |
| [`zeppelin`](./plugins/zeppelin) | Run SQL / Spark / PySpark / shell on Apache Zeppelin with built-in risk control. |
| [`starrocks`](./plugins/starrocks) | Run SQL on StarRocks over the MySQL protocol (pure-Python, no driver) with built-in risk control. |
| [`mysql`](./plugins/mysql) | Run SQL on MySQL over the MySQL protocol (pure-Python; native + caching_sha2 auth) with built-in risk control. |
| [`mongodb`](./plugins/mongodb) | Query MongoDB via JSON specs (pymongo; supports Atlas SRV) with a mongo-aware risk classifier and a per-collection schema/sample cache. |
| [`alidocs`](./plugins/alidocs) | Download a DingTalk / alidocs doc, folder, or whole knowledge base from its link — and push / sync a local directory back up. |

The data-query plugins (zeppelin, starrocks, mysql, mongodb) share the same shape — risk
classification before execution, a schema/sample metadata cache, and
credentials with named **profiles** for multiple environments.

**Config lives under one directory:** `~/.taku/<tool>.json` (override with
`TAKU_DIR`). Legacy per-tool paths (`~/.<tool>/config.json`) are still read as a
fallback, so existing setups keep working.

## Layout

```
.
├── .claude-plugin/marketplace.json   # marketplace manifest
└── plugins/
    ├── zeppelin/
    │   ├── .claude-plugin/plugin.json
    │   ├── skills/zeppelin/SKILL.md
    │   ├── scripts/                  # zeppelin.py + risk.py
    │   └── README.md
    ├── starrocks/
    │   ├── .claude-plugin/plugin.json
    │   ├── skills/starrocks/SKILL.md
    │   ├── scripts/                  # starrocks.py + risk.py
    │   └── README.md
    ├── mysql/
    │   ├── .claude-plugin/plugin.json
    │   ├── skills/mysql/SKILL.md
    │   ├── scripts/                  # mysql.py + risk.py + test_mysql.py
    │   └── README.md
    ├── mongodb/
    │   ├── .claude-plugin/plugin.json
    │   ├── skills/mongodb/SKILL.md
    │   ├── scripts/                  # mongodb.py + test_mongodb.py + requirements.txt
    │   └── README.md
    └── alidocs/
        ├── .claude-plugin/plugin.json
        ├── skills/alidocs/SKILL.md
        ├── scripts/                  # alidocs.py + dl_alidocs.py + test_upload_e2e.py
        └── README.md
```

## Validate

```bash
claude plugin validate .
```
