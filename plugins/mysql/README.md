# mysql

在 Claude Code 里直接用自然语言查 MySQL，自带风险管控 + 表元数据缓存。

纯 Python（只用标准库）实现 MySQL 协议握手与查询 —— **不需要 `pip install`，也不需要本机装 `mysql` 客户端**，直接连数据库端口（默认 3306）。同时支持 `mysql_native_password` 和 MySQL 8.0 默认的 `caching_sha2_password`。

---

## 安装

```
/plugin marketplace add zweite/claude-plugins
/plugin install mysql@zweite-tools
```

## 配置

凭据走环境变量或 `~/.taku/mysql.json`（旧路径 `~/.mysql/config.json` 仍兼容；`TAKU_DIR` 可改根目录。注意 `chmod 600`）。支持单环境（扁平）或多环境（`profiles`）：

```json
{
  "default_profile": "prod",
  "profiles": {
    "prod":    { "host": "db-prod",    "port": 3306, "user": "you", "password": "…" },
    "staging": { "host": "db-staging", "user": "you", "password": "…", "ssl": true }
  },
  "cache_ttl_days": 30
}
```

- 选 profile：`--profile staging` 或 `MYSQL_PROFILE`；都没指定就用 `default_profile`，再没有且只有一个 profile 就用那个。
- `profiles` 外层的 key（如 `cache_ttl_days`）是**所有 profile 共享的默认值**，profile 内可覆盖。
- 没有 `profiles` 的扁平 config 视为隐式 `default` profile（其缓存不加 profile 子目录）。
- 缓存按 profile 隔离，prod / staging 同名表互不覆盖。

可选项（**环境变量优先于 config.json，都没有才用默认值**）：

| 环境变量 | profile/config key | 默认 | 说明 |
| --- | --- | --- | --- |
| `MYSQL_HOST` | `host` | 必填 | 数据库地址 |
| `MYSQL_PORT` | `port` | `3306` | 端口 |
| `MYSQL_USER` | `user` | 必填 | 账号 |
| `MYSQL_PASSWORD` | `password` | 必填 | 密码 |
| `MYSQL_DATABASE` | `database` | 空 | 默认库（可选） |
| `MYSQL_SSL` | `ssl` | `false` | 从一开始就走 TLS（`require_secure_transport=ON` 时用） |
| `MYSQL_SSL_VERIFY` | `ssl_verify` | `true` | 是否校验服务端证书（自签证书设为 `0`） |
| `MYSQL_TIMEOUT_SECONDS` | `timeout_seconds` | `30` | socket 超时 |
| `MYSQL_CACHE_DIR` | `cache_dir` | `~/.mysql/cache` | 表结构 + sample 缓存目录 |
| `MYSQL_CACHE_TTL_DAYS` | `cache_ttl_days` | `30` | 缓存新鲜度窗口（天） |
| `MYSQL_AUTO_APPROVE_LEVEL` | —（仅环境变量） | `safe` | 风险闸门等级；由 Claude 读取，不走 config.json |

### 认证说明

- `mysql_native_password` 和 `caching_sha2_password` 的**快速路径**（密码已在服务端缓存）都能走明文 socket。
- `caching_sha2_password` 账号**首次登录**会触发 full auth，需要安全通道。客户端会自动升级到 TLS 并重试一次。若 TLS 握手失败（如自签证书），设 `MYSQL_SSL_VERIFY=0`（或 `ssl_verify: false`）。**不支持** RSA 公钥认证（标准库没有 RSA）。
- 服务端开了 `require_secure_transport=ON` 时，设 `MYSQL_SSL=1`（或 `ssl: true`）从一开始就走 TLS。

验证：

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/mysql.py" test-conn
# {"ok": true, "profile": "prod", "tls": false, "result": [{"1": "1"}], "elapsed_seconds": 0.05}
```

---

## 使用

装好后直接说：

> 用 mysql 看下 prod 的 `mydb.orders` 今天有多少单

Claude 会走风险打分、命中高风险弹确认、跑完把结果摆出来，并按需缓存表结构。

---

## 风险等级

与 zeppelin / starrocks 插件共用同一个打分器（`risk.py`）：`safe`（纯读）/ `low` / `medium` / `high`（DDL、无 WHERE 的删改、覆盖写等）。高于 `MYSQL_AUTO_APPROVE_LEVEL` 的会弹确认。Claude 还会叠加语义判断（是不是生产表、是不是 PII / 财务表等）并**只升不降**。

---

## CLI 直接用（不经过 Claude）

`$ROOT` 指向插件目录。

```bash
# 连通性（输出里 tls 字段表示这次连接是否走了 TLS）
python3 "$ROOT/scripts/mysql.py" test-conn

# 跑 SQL（指定 profile）
python3 "$ROOT/scripts/mysql.py" --profile staging query --sql 'SELECT 1'

# 表元数据缓存（结构 + 10 条 sample，按 profile 隔离，TTL 默认 30 天）
python3 "$ROOT/scripts/mysql.py" cache put   --table mydb.orders          # 写入（已新鲜则跳过）
python3 "$ROOT/scripts/mysql.py" cache put   --table mydb.orders --force  # 强制更新
python3 "$ROOT/scripts/mysql.py" cache get   --table mydb.orders          # 读取：hit / stale / miss
python3 "$ROOT/scripts/mysql.py" cache list                              # 列出 + 新鲜度
python3 "$ROOT/scripts/mysql.py" cache clear --table mydb.orders         # 清除单个 / --all 清全部

# 单独打分（不联网）
echo 'DROP TABLE foo' | python3 "$ROOT/scripts/risk.py" --magic '%sql'
```

---

## 测试

纯逻辑单测（scramble 算法对照、profile 解析、缓存新鲜度、表名校验等，不联网）：

```bash
python3 "$ROOT/scripts/test_mysql.py"
```

联网的 e2e（认证 + 查询）请用 `test-conn` 对真实服务器验证。

---

## 设计说明

- **纯标准库 MySQL 协议**：实现了 `mysql_native_password` 与 `caching_sha2_password` 握手（含 AuthSwitchRequest、caching_sha2 的 fast/full auth）+ 文本协议 COM_QUERY 解析，无第三方依赖。
- **caching_sha2 full auth 走 TLS**：首次登录冷缓存时自动升级 TLS 发送明文口令（标准库 `ssl`），不实现 RSA 路径。
- **直接执行**：MySQL 没有 notebook 概念，SQL 直接跑。
- **凭据脱敏**：错误信息里的 `password` 字段会被打码。
