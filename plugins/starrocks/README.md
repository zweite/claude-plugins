# starrocks

在 Claude Code 里直接用自然语言查 StarRocks，自带风险管控 + 表元数据缓存。

纯 Python（只用标准库）实现 MySQL 协议握手与查询 —— **不需要 `pip install`，也不需要本机装 `mysql` 客户端**，直接连 FE 查询端口（默认 9030）。

---

## 安装

```
/plugin marketplace add zweite/claude-plugins
/plugin install starrocks@zweite-tools
```

## 配置

凭据走环境变量或 `~/.starrocks/config.json`（注意 `chmod 600`）。支持单环境（扁平）或多环境（`profiles`）：

```json
{
  "default_profile": "prod",
  "profiles": {
    "prod":    { "host": "fe-prod",    "port": 9030, "user": "you", "password": "…" },
    "staging": { "host": "fe-staging", "user": "you", "password": "…" }
  },
  "cache_ttl_days": 30
}
```

- 选 profile：`--profile staging` 或 `STARROCKS_PROFILE`；都没指定就用 `default_profile`，再没有且只有一个 profile 就用那个。
- `profiles` 外层的 key（如 `cache_ttl_days`）是**所有 profile 共享的默认值**，profile 内可覆盖。
- 没有 `profiles` 的扁平 config 视为隐式 `default` profile（其缓存不加 profile 子目录）。
- 缓存按 profile 隔离，prod / staging 同名表互不覆盖。

可选项（**环境变量优先于 config.json，都没有才用默认值**）：

| 环境变量 | profile/config key | 默认 | 说明 |
| --- | --- | --- | --- |
| `STARROCKS_HOST` | `host` | 必填 | FE 地址 |
| `STARROCKS_PORT` | `port` | `9030` | FE MySQL 协议端口 |
| `STARROCKS_USER` | `user` | 必填 | 账号 |
| `STARROCKS_PASSWORD` | `password` | 必填 | 密码 |
| `STARROCKS_DATABASE` | `database` | 空 | 默认库（可选） |
| `STARROCKS_TIMEOUT_SECONDS` | `timeout_seconds` | `30` | socket 超时 |
| `STARROCKS_CACHE_DIR` | `cache_dir` | `~/.starrocks/cache` | 表结构 + sample 缓存目录 |
| `STARROCKS_CACHE_TTL_DAYS` | `cache_ttl_days` | `30` | 缓存新鲜度窗口（天） |
| `STARROCKS_AUTO_APPROVE_LEVEL` | —（仅环境变量） | `safe` | 风险闸门等级；由 Claude 读取，不走 config.json |

> ⚠️ 只支持 `mysql_native_password` 账号。如果账号用的是 `caching_sha2_password`，请改用 native 口令插件，或新建一个 native 账号。

验证：

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/starrocks.py" test-conn
# {"ok": true, "profile": "prod", "result": [{"1": "1"}], "elapsed_seconds": 0.05}
```

---

## 使用

装好后直接说：

> 用 starrocks 看下 prod 的 `mydb.orders` 今天有多少单

Claude 会走风险打分、命中高风险弹确认、跑完把结果摆出来，并按需缓存表结构。

---

## 风险等级

与 zeppelin 插件共用同一个打分器（`risk.py`）：`safe`（纯读）/ `low` / `medium` / `high`（DDL、无 WHERE 的删改、覆盖写等）。高于 `STARROCKS_AUTO_APPROVE_LEVEL` 的会弹确认。Claude 还会叠加语义判断（是不是生产表、是不是 PII / 财务表等）并**只升不降**。

---

## CLI 直接用（不经过 Claude）

`$ROOT` 指向插件目录。

```bash
# 连通性
python3 "$ROOT/scripts/starrocks.py" test-conn

# 跑 SQL（指定 profile）
python3 "$ROOT/scripts/starrocks.py" --profile staging query --sql 'SELECT 1'

# 表元数据缓存（结构 + 10 条 sample，按 profile 隔离，TTL 默认 30 天）
python3 "$ROOT/scripts/starrocks.py" cache put   --table mydb.orders          # 写入（已新鲜则跳过）
python3 "$ROOT/scripts/starrocks.py" cache put   --table mydb.orders --force  # 强制更新
python3 "$ROOT/scripts/starrocks.py" cache get   --table mydb.orders          # 读取：hit / stale / miss
python3 "$ROOT/scripts/starrocks.py" cache list                              # 列出 + 新鲜度
python3 "$ROOT/scripts/starrocks.py" cache clear --table mydb.orders         # 清除单个 / --all 清全部

# 单独打分（不联网）
echo 'DROP TABLE foo' | python3 "$ROOT/scripts/risk.py" --magic '%sql'
```

---

## 设计说明

- **纯标准库 MySQL 协议**：实现了 `mysql_native_password` 握手（含 AuthSwitchRequest）+ 文本协议 COM_QUERY 解析，无第三方依赖。
- **直接执行**：StarRocks 没有 notebook 概念，SQL 直接跑，无 note 生命周期。
- **凭据脱敏**：错误信息里的 `password` 字段会被打码。
