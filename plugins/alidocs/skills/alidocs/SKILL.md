---
name: alidocs
description: Download a DingTalk / alidocs document — or a whole folder, or an entire knowledge base — from a shared link, to local disk. Use this skill whenever the user gives an alidocs.dingtalk.com link and wants the doc(s) saved locally.
---

# alidocs Skill

You download DingTalk / alidocs documents to local disk via a helper script.
Give it a link and it mirrors that subtree: a single doc downloads just that
doc, a folder downloads everything under it, and a knowledge-base overview link
(`/i/spaces/<spaceId>/overview`) downloads the whole space.

## Tool

- `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/alidocs.py <subcmd>`

## Dependencies

The script needs `requests` (`pip install requests`). To export online docs
(`.adoc`→`.docx`, `.axls`→`.xlsx`) it also needs Playwright:

```
pip install playwright && playwright install chromium
```

Without Playwright the download still works, but online docs are saved as `.url`
shortcuts (+ `.meta.json`) instead of exported files. The script auto-detects
Playwright; pass `--no-playwright` to force shortcut mode.

## Config

Credentials come from env or `~/.taku/alidocs.json` (legacy `~/.alidocs/config.json`
still read; override the dir with `TAKU_DIR`). The cookie is the full `Cookie:`
request header copied from a logged-in alidocs browser session.

```json
{
  "cookie": "cna=...; doc_atoken=...; XSRF-TOKEN=...; ...",
  "default_out_dir": "~/Downloads/alidocs",
  "use_playwright": true
}
```

- `cookie` (or `cookie_file`: path to a file holding the cookie) — required.
  The `a-token` is auto-extracted from the `doc_atoken` cookie.
- `default_out_dir` — where downloads go when `--out` isn't passed (default `~/Downloads/alidocs`).
- `use_playwright` — force on/off; omit to auto-detect.
- `space_id` — optional; auto-discovered from the link's page otherwise.
- Multiple accounts: use a `profiles` map + `default_profile`, selected with
  `--profile NAME` / `ALIDOCS_PROFILE`.

Do NOT prompt the user for the cookie inline — they put it in the config file
(`chmod 600`, it contains session tokens).

## Workflow

1. **Verify** the link + cookie resolve (cheap, no download):
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/alidocs.py check --url "<link>"
   # -> {"ok": true, "name": "...", "is_folder": true/false, "has_children": ...}
   ```
   If it errors with a 401/auth message, the cookie is stale — tell the user to
   refresh it in `~/.taku/alidocs.json`.

2. **Download**:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/alidocs.py fetch --url "<link>" [--out DIR]
   ```
   - No `--out` → goes to the configured `default_out_dir`.
   - Final stdout line is a JSON summary: `{"ok", "root", "out_dir", "playwright", "stats": {...}}`.

3. **Report** to the user: the output directory, and the `stats`
   (folders / files / exported / online_docs / skipped / errors). If
   `playwright` is false and there were `online_docs`, mention they were saved
   as `.url` shortcuts and how to enable full export.

## Notes

- The link can be a doc, a folder, or a whole knowledge base (`/i/spaces/<id>/overview`) — same command; no need to ask which.
- Re-running skips files already downloaded (idempotent).
- Existing-permission errors on a doc mean the account lacks 可查看/下载 rights — surface that, don't retry blindly.
- This skill writes files to the user's disk. Confirm the output directory with the user if it's not the default and looks unusual.
