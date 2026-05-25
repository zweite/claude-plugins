# alidocs

在 Claude Code 里给一个钉钉文档 / alidocs 链接，下载到本地；或者反过来，把本地目录推到 alidocs 文件夹下。

**下载：**

- 给**单篇文档链接** → 下载该文档
- 给**文件夹链接** → 递归下载文件夹下所有文档（保留目录结构）
- 给**知识库 overview 链接**（`/i/spaces/<id>/overview`）→ 整库下载，顶层文件夹用知识库的显示名
- 不指定目录时，落到配置的默认目录

**上传 / 同步**（`push` / `sync` 子命令）：

- `--src` 本地路径 + `--dest` 一个 alidocs 文件夹链接，把目录树原样推上去
- 默认 `--on-conflict skip-if-same`（同名且同 size 跳过；size 不一致则原 dentry 原地 over-write，保留 uuid + 版本）
- `--on-conflict overwrite` 强制全部覆盖；`rename` 上传成 `foo(1).txt` 副本
- `--prune` 把本地不存在的远端 dentry 软删（进回收站，可恢复）
- `--dry-run` 先看 plan 再决定；正式跑加 `--yes` 跳过确认
- `--create-dest '<folder-link>::path/to/leaf'` 缺路径就一层层建出来

底层封装 `dl_alidocs.py`：二进制文件走 API 直接下载；在线文档（`.adoc`→`.docx`、`.axls`→`.xlsx`）用 Playwright 驱动编辑器导出；其余在线类型存为 `.url` 快捷方式 + `.meta.json`。上传走 `/box/api/v2/file/uploadinfo` → OSS STS PUT → `/box/api/v2/file/commit`（或更新版本 `commitForUpdateVersion`），文件夹走 `dentry/createfolder`。

---

## 安装

```
/plugin marketplace add zweite/claude-plugins
/plugin install alidocs@zweite-tools
```

### 依赖

```bash
pip install requests            # 必须
pip install oss2                # 上传需要（push/sync）；不装就只能下载
# 想导出在线文档（推荐）：
pip install playwright && playwright install chromium
```

没装 Playwright 也能下载 —— 在线文档会存成 `.url` 快捷方式（脚本自动探测，没有就降级）。`oss2` 是 push/sync 的必备（懒加载）；下载不需要它。

## 配置

凭据走环境变量或 `~/.taku/alidocs.json`（旧路径 `~/.alidocs/config.json` 仍兼容；用 `TAKU_DIR` 可改根目录）。`cookie` 是从已登录的 alidocs 浏览器会话里复制的完整 `Cookie:` 请求头。

```json
{
  "cookie": "cna=...; doc_atoken=...; XSRF-TOKEN=...; ...",
  "default_out_dir": "~/Downloads/alidocs",
  "use_playwright": true
}
```

| key | 说明 |
| --- | --- |
| `cookie` / `cookie_file` | 必填。完整 Cookie 头（或存了 cookie 的文件路径）。`a-token` 自动从 `doc_atoken` 提取 |
| `default_out_dir` | 不传 `--out` 时的落盘目录，默认 `~/Downloads/alidocs` |
| `use_playwright` | 强制开/关在线文档导出；不写则自动探测 |
| `space_id` | 可选；不填则从链接页面自动发现 |

> 多账号：用 `profiles` 映射 + `default_profile`，`--profile NAME` 或 `ALIDOCS_PROFILE` 选择。
> ⚠️ cookie 含会话令牌，建议 `chmod 600 ~/.taku/alidocs.json`。

---

## 使用

装好后直接说：

> 用 alidocs 把这个文档下下来 https://alidocs.dingtalk.com/i/nodes/xxxx

或给个文件夹链接，整个目录都会下下来。

## CLI 直接用

```bash
ROOT=path/to/plugins/alidocs

# 验证 cookie + 解析节点（不下载）
python3 "$ROOT/scripts/alidocs.py" check --url 'https://alidocs.dingtalk.com/i/nodes/<uuid>'

# 下载到默认目录
python3 "$ROOT/scripts/alidocs.py" fetch --url 'https://alidocs.dingtalk.com/i/nodes/<uuid>'

# 下载到指定目录、强制不导出（只存 .url）
python3 "$ROOT/scripts/alidocs.py" fetch --url '<link>' --out ~/docs/proj --no-playwright

# 把本地目录推到 alidocs（dry-run 先看 plan）
python3 "$ROOT/scripts/alidocs.py" push --src ./my-project \
    --dest 'https://alidocs.dingtalk.com/i/nodes/<folderUuid>' --dry-run

# 正式跑（默认 skip-if-same）
python3 "$ROOT/scripts/alidocs.py" push --src ./my-project \
    --dest 'https://alidocs.dingtalk.com/i/nodes/<folderUuid>' --yes

# 强制覆盖 + 软删本地不存在的文件（单向同步）
python3 "$ROOT/scripts/alidocs.py" sync --src ./my-project \
    --dest '<link>' --on-conflict overwrite --prune --yes

# 自动建路径：在某文件夹下建 sub/dir/leaf
python3 "$ROOT/scripts/alidocs.py" push --src ./pkg \
    --create-dest '<folder-link>::imports/2026/q2' --yes
```

支持的链接形式：

- `https://alidocs.dingtalk.com/i/nodes/<dentryUuid>` — 单文档或文件夹（doc/folder 自动识别）
- `?dentryUuid=` / `?nodeId=` / `?spaceId=` 等查询参数形式
- `https://alidocs.dingtalk.com/i/spaces/<spaceId>/overview` — 整个知识库

### push / sync 常用旗标

| flag | 默认 | 说明 |
| --- | --- | --- |
| `--src PATH` | required | 本地文件或目录 |
| `--dest URL` | required | alidocs 文件夹链接（个人空间没有 root，必须先有一个 folder） |
| `--create-dest 'LINK::a/b/c'` | — | 在 LINK 下建路径，缺哪一层就建哪一层；leaf 当 dest |
| `--on-conflict` | `skip-if-same` | `skip-if-same` / `overwrite` / `rename` / `error` |
| `--prune` | off | 远端有、本地没有的 dentry → 软删（进回收站） |
| `--include 'glob'` / `--exclude 'glob'` | 默认排除 `.* __pycache__ node_modules *.pyc .DS_Store` | 可重复 |
| `--max-size N` | 无 | 跳过超过 N 字节的文件 |
| `--dry-run` / `-n` | off | 只打印 plan，不写远端 |
| `--yes` / `-y` | off | 跳过确认提示 |

### 已知限制

- `.md` / `.docx` / `.txt` 现在统一作为**二进制附件**上传（可下载，但不是在线 .adoc）。把它们转成在线编辑文档需要走 `/box/api/v2/import/document` + `/r/Adaptor/...` 上传通道，这条通道在浏览器内是钉钉客户端 JSAPI 桥接的，不能用 cookie 直接 HTTP 调到。需要在线编辑就在 alidocs Web 端上传后手动「转为在线文档」。
- 上传的 OSS STS token 有 ~15 分钟有效期；超大文件 (>100MB) 没有 multipart 支持，遇到再加。
- 单次 `commit` 默认走 `auto_rename`/`over_write`；`return_existing_dentry` 只用在 `createfolder` 上（已验证幂等）。
- 个人空间没有 listable virtual root；只能指定一个具体文件夹链接作为 dest。
