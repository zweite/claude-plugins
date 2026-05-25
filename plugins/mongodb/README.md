# mongodb

在 Claude Code 里直接用自然语言查 MongoDB，自带风险管控 + 集合元数据缓存。

走官方 `pymongo` 驱动，支持自托管 / 副本集 / Atlas SRV / SCRAM-SHA-256 / TLS —— 这是仓库里**第一个有 Python 依赖**的插件（pure-stdlib 写一个 BSON+OP_MSG+SCRAM 客户端要 1500+ 行，性价比太低）。

---

## 安装

```
/plugin marketplace add zweite/claude-plugins
/plugin install mongodb@zweite-tools
pip install -r "$(claude plugin path mongodb)/scripts/requirements.txt"
```

第一次跑 `test-conn` 如果报 `pymongo not installed`，执行上面那行 pip 装一下就行。`requirements.txt` 里是 `pymongo>=4.0` 和 `dnspython>=2.0`（Atlas SRV 必需）。

## 配置

凭据走环境变量或 `~/.taku/mongodb.json`（旧路径 `~/.mongodb/config.json` 仍兼容；`TAKU_DIR` 可改根目录。注意 `chmod 600`）。支持单环境（扁平）或多环境（`profiles`）—— 和 starrocks / mysql 完全一致：

```json
{
  "default_profile": "prod",
  "profiles": {
    "prod":    { "uri": "mongodb+srv://you:pw@cluster.mongodb.net/", "database": "app" },
    "staging": { "uri": "mongodb://you:pw@stage:27017/",             "database": "app" }
  },
  "cache_ttl_days": 30
}
```

- 选 profile：`--profile staging` 或 `MONGODB_PROFILE`；都没指定就用 `default_profile`，再没有且只有一个就用那个。
- `profiles` 外层的 key（如 `cache_ttl_days`）是**所有 profile 共享的默认值**，profile 内可覆盖。
- 没有 `profiles` 的扁平 config 视为隐式 `default` profile（其缓存不加 profile 子目录）。
- 缓存按 profile 隔离，prod / staging 同名集合互不覆盖。

可选项（**环境变量优先于 config.json，都没有才用默认值**）：

| 环境变量 | profile/config key | 默认 | 说明 |
| --- | --- | --- | --- |
| `MONGODB_URI` | `uri` | 必填 | 完整 URI，`mongodb://` 或 `mongodb+srv://`；用户名密码也在里面 |
| `MONGODB_DATABASE` | `database` | 空 | 默认 db（也可写在 URI 末尾 `/<db>`） |
| `MONGODB_TIMEOUT_SECONDS` | `timeout_seconds` | `30` | 连接 / socket 超时 |
| `MONGODB_CACHE_DIR` | `cache_dir` | `~/.mongodb/cache` | schema + sample 缓存目录 |
| `MONGODB_CACHE_TTL_DAYS` | `cache_ttl_days` | `30` | 缓存新鲜度窗口（天） |
| `MONGODB_AUTO_APPROVE_LEVEL` | —（仅环境变量） | `safe` | 风险闸门等级；由 Claude 读取，不走 config.json |

验证：

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/mongodb.py" test-conn
# {"ok": true, "profile": "prod", "version": "7.0.x", "elapsed_seconds": 0.04}
```

---

## 使用

装好后直接说：

> 用 mongodb 看下 prod 的 `app.orders` 今天有多少单

Claude 会：把意图翻译成 JSON spec → 走 `classify` 评估风险 → 命中高风险弹确认 → 跑 `query` → 把结果摆出来，并按需缓存集合 schema。

---

## 风险等级

MongoDB op 是 JSON 不是 SQL，所以**内置 classifier**（`mongodb.py classify --spec '<JSON>'`），不复用 zeppelin / mysql / starrocks 共享的 `risk.py`。规则：

- `safe`：`ping` / `listCollections` / `listDatabases`；`find` / `count` / `distinct` / `aggregate` 且 `limit ≤ 1000`
- `low`：上面这些 op 但**没显式 limit** 或 `limit > 1000`；非管理类 `runCommand`
- `medium`：`insertOne` / `insertMany`；带 filter 的 `updateOne` / `deleteOne`
- `high`：**无 filter** 的 `updateMany` / `deleteMany`（含 `updateOne`/`deleteOne` 无 filter 时）；`runCommand` 走 `drop` / `dropDatabase` / `renameCollection` / `shutdown` / `createUser` / `dropUser` / `createIndex` / `dropIndex` / `compact` / `shardCollection`

高于 `MONGODB_AUTO_APPROVE_LEVEL` 的会弹确认。Claude 还会叠加语义判断（生产 / PII / 财务集合等）并**只升不降**。

---

## CLI 直接用（不经过 Claude）

`$ROOT` 指向插件目录。Spec 是 JSON：

```bash
# 连通性
python3 "$ROOT/scripts/mongodb.py" test-conn

# 单独打分（不联网）
python3 "$ROOT/scripts/mongodb.py" classify --spec '{"op":"deleteMany","db":"app","collection":"orders","filter":{}}'

# 查询
python3 "$ROOT/scripts/mongodb.py" --profile staging query --spec '{"op":"find","collection":"orders","filter":{"status":"paid"},"limit":10}'
python3 "$ROOT/scripts/mongodb.py" query --spec '{"op":"aggregate","collection":"orders","pipeline":[{"$match":{"status":"paid"}},{"$group":{"_id":"$customer","total":{"$sum":"$amount"}}}],"limit":50}'
python3 "$ROOT/scripts/mongodb.py" query --spec '{"op":"count","collection":"orders","filter":{}}'

# 集合元数据缓存（estimatedDocumentCount + listIndexes + 10 doc sample + 字段类型推断）
python3 "$ROOT/scripts/mongodb.py" cache put   --collection app.orders          # 写入（已新鲜则跳过）
python3 "$ROOT/scripts/mongodb.py" cache put   --collection app.orders --force  # 强制更新
python3 "$ROOT/scripts/mongodb.py" cache get   --collection app.orders          # 读取：hit / stale / miss
python3 "$ROOT/scripts/mongodb.py" cache list                                  # 列出 + 新鲜度
python3 "$ROOT/scripts/mongodb.py" cache clear --collection app.orders         # 清除单个 / --all 清全部
```

输出里 BSON 类型用 [Extended JSON](https://www.mongodb.com/docs/manual/reference/mongodb-extended-json/) 表示：`ObjectId` → `{"$oid": "..."}`，`Date` → `{"$date": "..."}`，`Decimal128` → `{"$numberDecimal": "..."}`。

---

## 测试

纯逻辑单测（classifier、schema 推断、profile 解析、缓存新鲜度、collection 名校验等，不联网，不需要 pymongo）：

```bash
python3 "$ROOT/scripts/test_mongodb.py"
```

联网 e2e（认证 + 查询 + 缓存）用 `test-conn` / `query` 对真实 MongoDB（含本地 `docker run mongo:7`）验证。

---

## 设计说明

- **依赖 pymongo**：避开了 BSON / OP_MSG / SCRAM 三件套的手写工程量，覆盖所有 MongoDB 部署形态（含 Atlas）。
- **JSON spec 协议**：MongoDB op 没有 SQL 那种线性文本表达，统一成 JSON 是最稳的中间表示。
- **内置 classifier**：MongoDB op 模型和 SQL 完全不同，单独写规则比硬塞进 `risk.py` 干净。
- **BSON-safe 输出**：用 `bson.json_util`（Extended JSON）保留 ObjectId / Date / Decimal128 等类型，不会被 JSON 序列化丢精度。
- **凭据脱敏**：错误信息里的 `mongodb://user:pass@...` 和 JSON 里的 `"password"` / `"uri"` 字段会被打码。
