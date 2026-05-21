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
| [`alidocs`](./plugins/alidocs) | Download a DingTalk / alidocs doc or whole folder from its link. |

The data-query plugins (zeppelin, starrocks) share the same shape — risk
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
    └── alidocs/
        ├── .claude-plugin/plugin.json
        ├── skills/alidocs/SKILL.md
        ├── scripts/                  # alidocs.py + dl_alidocs.py
        └── README.md
```

## Validate

```bash
claude plugin validate .
```
