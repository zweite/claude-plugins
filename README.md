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

## Layout

```
.
├── .claude-plugin/marketplace.json   # marketplace manifest
└── plugins/
    └── zeppelin/
        ├── .claude-plugin/plugin.json
        ├── skills/zeppelin/SKILL.md
        ├── scripts/
        └── README.md
```

## Validate

```bash
claude plugin validate .
```
