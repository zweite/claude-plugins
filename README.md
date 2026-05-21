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

Both plugins share the same shape — risk classification before execution, a
schema/sample metadata cache, and `config.json` credentials with named
**profiles** for multiple environments.

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
    └── starrocks/
        ├── .claude-plugin/plugin.json
        ├── skills/starrocks/SKILL.md
        ├── scripts/                  # starrocks.py + risk.py
        └── README.md
```

## Validate

```bash
claude plugin validate .
```
