# zeppelin — Claude Code Plugin

让 Claude Code 在你授权下安全地在 Apache Zeppelin 上跑 SQL / Spark / PySpark / Shell。
**自带风险评估**：危险操作（DDL、无 WHERE 的 DML、`.write` / `saveAsTable`、`rm -rf` 等）会先弹确认。

不依赖任何外部服务，也不依赖任何 pip 包，Python 3.9+ stdlib 即可。

---

## 安装

```
/plugin marketplace add zweite/claude-plugins
/plugin install zeppelin@zweite-tools
```

私有仓库也行，确保本机有对应的 git 凭据（ssh-agent / token）。
本地调试可以直接指向目录：`/plugin marketplace add /path/to/claude-plugins`。

---

## 配置

至少需要 Zeppelin 的地址 + 账号：

```bash
export ZEPPELIN_BASE_URL=http://zeppelin.example.com:30090
export ZEPPELIN_USERNAME=you
export ZEPPELIN_PASSWORD='…'
```

或者写文件 `~/.zeppelin/config.json`（注意 chmod 600）。除了凭据，下表里带 config.json key 的可选项也能写进来：

```json
{
  "base_url": "http://zeppelin.example.com:30090",
  "username": "you",
  "password": "…",
  "note_dir": "fin-eng/adhoc",
  "keep_notes": true,
  "timeout_seconds": 600,
  "poll_interval_seconds": 1.5,
  "cache_dir": "~/.zeppelin/cache",
  "cache_ttl_days": 30
}
```

### 多环境（profiles）

有多套 Zeppelin（prod / staging 等）时，把扁平 key 换成 `profiles` 映射：

```json
{
  "default_profile": "prod",
  "profiles": {
    "prod": { "base_url": "http://prod:8080", "username": "you", "password": "…", "note_dir": "prod/adhoc" },
    "stg":  { "base_url": "http://stg:8080",  "username": "you", "password": "…" }
  },
  "cache_ttl_days": 30
}
```

- 选 profile：`--profile stg`（放在子命令前，如 `zeppelin.py --profile stg exec …`）或 `ZEPPELIN_PROFILE`；都没指定就用 `default_profile`，再没有且只有一个就用那个。
- `profiles` 外层的 key 是所有 profile 共享的默认值，profile 内可覆盖。
- 缓存按 profile 隔离（`~/.zeppelin/cache/<profile>/`），prod / stg 同名表互不覆盖。
- 没有 `profiles` 的扁平 config 视为隐式 `default`，缓存不加子目录（向后兼容，旧缓存不丢）。

可选项（**环境变量优先于 config.json，都没有才用默认值**）：

| 环境变量 | config.json key | 默认 | 说明 |
| --- | --- | --- | --- |
| `ZEPPELIN_NOTE_DIR` | `note_dir` | `__skill/zeppelin` | 新建 note 放在 Zeppelin workspace 的哪个目录下，例如 `fin-eng/adhoc` |
| `ZEPPELIN_KEEP_NOTES` | `keep_notes` | `false` | `true`/`1` = 跑完保留 note；默认跑完即删 |
| `ZEPPELIN_TIMEOUT_SECONDS` | `timeout_seconds` | `300` | 单次轮询超时 |
| `ZEPPELIN_POLL_INTERVAL_SECONDS` | `poll_interval_seconds` | `1.5` | 轮询间隔 |
| `ZEPPELIN_CACHE_DIR` | `cache_dir` | `~/.zeppelin/cache` | 表结构 + 数据 sample 缓存目录 |
| `ZEPPELIN_CACHE_TTL_DAYS` | `cache_ttl_days` | `30` | 缓存新鲜度窗口（天），过期算 `stale` |
| `ZEPPELIN_AUTO_APPROVE_LEVEL` | —（仅环境变量） | `safe` | `safe` 只放纯读；`low` / `medium` / `high` 逐级放宽。高于这个等级的会弹确认。由 Claude 读取，不走 config.json |

验证（`${CLAUDE_PLUGIN_ROOT}` 是 Claude Code 注入的插件根目录）：

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/zeppelin.py" test-conn
# {"ok": true, "principal": {...}, "elapsed_seconds": 0.42}
```

---

## 使用

装好之后在 Claude Code 里直接说：

> 用 zeppelin 看一下 `bi.dws_user_active_d` 今天的活跃用户数

Claude 会自己拼 `%spark.sql`、调风险打分、命中高风险时让你确认、跑完把结果摆出来。

---

## 风险等级

| 等级 | 例子 |
| --- | --- |
| `safe` | `SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN`, `USE` |
| `low` | `UPDATE … WHERE …`、`DELETE … WHERE …`、未识别但看起来是 SQL 的 |
| `medium` | `INSERT INTO`, `MERGE`, 未识别的代码段 |
| `high` | 所有 DDL（DROP/TRUNCATE/ALTER/CREATE/REPLACE/GRANT/REVOKE）、`INSERT OVERWRITE`、无 WHERE 的 UPDATE/DELETE、`.write` / `saveAsTable` / `.save(`、`rm -rf` / `hdfs dfs -rm` |

打分器是**保守**的（宁误报不漏报），Claude 还会在上面叠加一层语义判断：表名像不像生产、时间是不是凌晨、是不是 PII 表，等等，命中就会**升级**等级，不会降级。

---

## CLI 直接用（不经过 Claude）

`$ROOT` 指向插件目录（装好后在 `~/.claude/plugins/...`，或就用源码目录的 `plugins/zeppelin`）。

```bash
# 临时查询：提交 + 轮询 + 跑完删 note
python3 "$ROOT/scripts/zeppelin.py" exec --magic '%spark.sql' --code 'SELECT 1'

# 落到指定目录、保留 note
ZEPPELIN_NOTE_DIR=fin-eng/adhoc \
python3 "$ROOT/scripts/zeppelin.py" exec --magic '%spark.sql' --code 'SELECT 1' --keep-note

# 只提交不等
python3 "$ROOT/scripts/zeppelin.py" submit --magic '%pyspark' --code 'print("hi")'

# 轮询 / 清理
python3 "$ROOT/scripts/zeppelin.py" poll --note 2K... --para 20... --timeout 600
python3 "$ROOT/scripts/zeppelin.py" list-notes
python3 "$ROOT/scripts/zeppelin.py" delete-note --note 2K...

# 表元数据缓存（结构 + 10 条 sample，默认存 ~/.zeppelin/cache，TTL 30 天）
python3 "$ROOT/scripts/zeppelin.py" cache put --table uparpu_main.orders          # 写入（已新鲜则跳过）
python3 "$ROOT/scripts/zeppelin.py" cache put --table uparpu_main.orders --force  # 强制更新
python3 "$ROOT/scripts/zeppelin.py" cache get --table uparpu_main.orders          # 读取：hit / stale / miss
python3 "$ROOT/scripts/zeppelin.py" cache list                                    # 列出所有缓存 + 新鲜度
python3 "$ROOT/scripts/zeppelin.py" cache clear --table uparpu_main.orders        # 清除单个 / --all 清全部

# 单独打分（不联网）
echo 'DROP TABLE foo' | python3 "$ROOT/scripts/risk.py" --magic '%spark.sql'
```

---

## 已知限制

- CLI 不直接暴露「往已有 note 里追加 paragraph」的子命令——`exec` 每次开新 note。需要多 paragraph 共享 SparkContext 时，目前要走 `submit` + 自行拼 REST，或等加一个 `append-paragraph` 子命令。
- 风险打分基于正则；很复杂的动态 SQL（字符串拼接、嵌套）可能漏判，所以才需要 Claude 在上面再判一层。
- 凭据只走 env 或本地配置文件，不支持 secret manager / OIDC。
