# alidocs

在 Claude Code 里给一个钉钉文档 / alidocs 链接，把对应文档（或整个文件夹、整个知识库）下载到本地。

- 给**单篇文档链接** → 下载该文档
- 给**文件夹链接** → 递归下载文件夹下所有文档（保留目录结构）
- 给**知识库 overview 链接**（`/i/spaces/<id>/overview`）→ 整库下载，顶层文件夹用知识库的显示名
- 不指定目录时，落到配置的默认目录

底层封装 `dl_alidocs.py`：二进制文件走 API 直接下载；在线文档（`.adoc`→`.docx`、`.axls`→`.xlsx`）用 Playwright 驱动编辑器导出；其余在线类型存为 `.url` 快捷方式 + `.meta.json`。

---

## 安装

```
/plugin marketplace add zweite/claude-plugins
/plugin install alidocs@zweite-tools
```

### 依赖

```bash
pip install requests
# 想导出在线文档（推荐）：
pip install playwright && playwright install chromium
```

没装 Playwright 也能跑 —— 在线文档会存成 `.url` 快捷方式（脚本自动探测，没有就降级）。

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
```

支持的链接形式：

- `https://alidocs.dingtalk.com/i/nodes/<dentryUuid>` — 单文档或文件夹（doc/folder 自动识别）
- `?dentryUuid=` / `?nodeId=` / `?spaceId=` 等查询参数形式
- `https://alidocs.dingtalk.com/i/spaces/<spaceId>/overview` — 整个知识库
