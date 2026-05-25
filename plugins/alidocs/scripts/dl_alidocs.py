#!/usr/bin/env python3
"""
Mirror an alidocs.dingtalk.com knowledge-base subtree to local disk,
preserving folder structure.

Walks the dentry tree via HTTP, then for each leaf:
  * uploaded binary file (extension not in ONLINE_DOC_EXTS) →
      direct download via /box/api/v2/file/download
  * online doc (.adoc) →
      Playwright drives the editor's "下载到本地 → Word(.docx)" menu,
      intercepts the resulting OSS URL, downloads as .docx
  * other online types (.axls/.amind/.aboard/...) →
      not yet wired for export; left as a .url shortcut + .meta.json

Usage:
  python3 dl_alidocs.py --cookie-file cookie.txt --root <dentryUuid> --out ./out

cookie.txt:
  Single line. Either the full Cookie: request-header value, or the bare
  cookie string. The a-token is auto-extracted from the doc_atoken cookie.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://alidocs.dingtalk.com"

ONLINE_DOC_EXTS = {".adoc", ".axls", ".amind", ".aboard", ".amxd", ".asheet"}
EXPORTABLE_TO_DOCX = {".adoc"}  # extensions Playwright will export to .docx
EXPORTABLE_TO_XLSX = {".axls"}  # extensions Playwright will export to .xlsx

INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str) -> str:
    name = INVALID_FS_CHARS.sub("_", name).strip().rstrip(".")
    return name or "untitled"


def load_cookie(path: str) -> str:
    raw = Path(path).read_text(encoding="utf-8").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return raw


def cookie_value(cookie_header: str, name: str) -> str | None:
    m = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", cookie_header)
    return m.group(1) if m else None


def make_session(cookie_header: str, a_token: str | None = None) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=8, backoff_factor=2,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    xsrf = cookie_value(cookie_header, "XSRF-TOKEN") or ""
    a_token = a_token or cookie_value(cookie_header, "doc_atoken") or ""
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": BASE + "/",
        "Origin": BASE,
        "Cookie": cookie_header,
        "a-token": a_token,
        "X-XSRF-TOKEN": xsrf,
        "x-csrf-token": xsrf,
        "Content-Type": "application/json",
    })
    return s


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------

@dataclass
class Node:
    uuid: str
    name: str
    dentry_type: str
    has_children: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_folder(self) -> bool:
        return self.dentry_type == "folder"

    @property
    def ext(self) -> str:
        _, e = os.path.splitext(self.name)
        return e.lower()

    @property
    def is_online_doc(self) -> bool:
        return self.ext in ONLINE_DOC_EXTS


def _get_with_proxy_retry(session: requests.Session, url: str, **kwargs) -> requests.Response:
    for attempt in range(10):
        try:
            return session.get(url, **kwargs)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as e:
            wait = 10 * (attempt + 1)
            print(f"[proxy-retry] attempt {attempt+1}, waiting {wait}s: {e.__class__.__name__}", flush=True)
            time.sleep(wait)
    return session.get(url, **kwargs)


def _post_with_proxy_retry(session: requests.Session, url: str, **kwargs) -> requests.Response:
    for attempt in range(10):
        try:
            return session.post(url, **kwargs)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as e:
            wait = 10 * (attempt + 1)
            print(f"[proxy-retry] attempt {attempt+1}, waiting {wait}s: {e.__class__.__name__}", flush=True)
            time.sleep(wait)
    return session.post(url, **kwargs)


def fetch_info(session: requests.Session, uuid: str, space_id: str) -> Node:
    r = _get_with_proxy_retry(session, BASE + "/box/api/v2/dentry/info",
                              params={"dentryUuid": uuid, "spaceId": space_id}, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    return Node(uuid=data["dentryUuid"], name=data.get("name", uuid),
                dentry_type=data.get("dentryType", "file"),
                has_children=bool(data.get("hasChildren", False)), raw=data)


def list_children(session: requests.Session, uuid: str, space_id: str) -> list[Node]:
    r = _get_with_proxy_retry(session, BASE + "/box/api/v2/dentry/list",
                              params={"dentryUuid": uuid, "spaceId": space_id, "size": 200}, timeout=20)
    r.raise_for_status()
    items = (r.json()["data"].get("children") or [])
    return [Node(uuid=it["dentryUuid"], name=it.get("name", it["dentryUuid"]),
                 dentry_type=it.get("dentryType", "file"),
                 has_children=bool(it.get("hasChildren", False)), raw=it)
            for it in items]


def fetch_launch(session: requests.Session, uuid: str, space_id: str) -> dict:
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/dentry/launch",
                               json={"dentryUuid": uuid, "spaceId": space_id}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def parse_launch_url(launch_url: str) -> tuple[str, str]:
    """Returns (docKey, dentryKey) from the editor launch URL query string."""
    qs = parse_qs(urlparse(launch_url).query)
    return qs["docId"][0], qs["dentryKey"][0]


# ---------------------------------------------------------------------------
# Download primitives
# ---------------------------------------------------------------------------

def download_binary_file(session: requests.Session, node: Node, space_id: str, out_path: Path) -> None:
    r = session.get(BASE + "/box/api/v2/file/download",
                    params={"dentryUuid": node.uuid, "spaceId": space_id}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"file/download {r.status_code}: {r.text[:200]}")
    ct = r.headers.get("content-type", "")
    if ct.startswith("application/json"):
        data = r.json()
        d = data.get("data") or {}
        url = d.get("downloadUrl") or d.get("url")
        # Handle URL_PRE_SIGNATURE format: ossUrlPreSignatureInfo.preSignUrls[0]
        if not url:
            pre_sign = d.get("ossUrlPreSignatureInfo") or {}
            urls = pre_sign.get("preSignUrls") or []
            url = urls[0] if urls else None
        if not url:
            raise RuntimeError(f"no download URL: {r.text[:200]}")
        # OSS pre-signed URL — use bare request without auth headers
        with requests.get(url, stream=True, timeout=120,
                          headers={"User-Agent": "Mozilla/5.0"}) as rr:
            rr.raise_for_status()
            with out_path.open("wb") as f:
                for chunk in rr.iter_content(chunk_size=64 * 1024):
                    if chunk: f.write(chunk)
    else:
        out_path.write_bytes(r.content)


def write_url_shortcut(out_path: Path, url: str) -> None:
    out_path.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")


def write_meta(out_path: Path, node: Node) -> None:
    out_path.write_text(json.dumps(node.raw, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Upload primitives (mkdir / file create / overwrite / rename / recycle / import)
# ---------------------------------------------------------------------------
#
# Conflict-strategy enum (server is case-insensitive; we mirror the JS-source
# lowercase form):
#   auto_rename              — append "(N)" suffix to avoid collision
#   over_write               — replace contents (creates a new file version)
#   return_existing_dentry   — well-defined for createfolder; returns existing
#   return_existing_error    — avoid: 500-storms in practice
#
# All write endpoints return {status:200, isSuccess:true, data:<dentry>} on
# success; raise on anything else.

CONFLICT_AUTO_RENAME = "auto_rename"
CONFLICT_OVER_WRITE = "over_write"
CONFLICT_RETURN_EXISTING = "return_existing_dentry"


def _expect_ok(r: requests.Response, label: str) -> dict:
    if not r.ok:
        raise RuntimeError(f"{label} {r.status_code}: {r.text[:300]}")
    body = r.json()
    if not body.get("isSuccess"):
        raise RuntimeError(f"{label} not isSuccess: {r.text[:300]}")
    return body.get("data") or {}


def _node_from_data(d: dict) -> Node:
    return Node(uuid=d["dentryUuid"], name=d.get("name", d["dentryUuid"]),
                dentry_type=d.get("dentryType", "file"),
                has_children=bool(d.get("hasChildren", False)), raw=d)


def create_folder(session: requests.Session, parent_uuid: str, name: str,
                  space_id: str, conflict: str = CONFLICT_RETURN_EXISTING) -> Node:
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/dentry/createfolder",
        json={"parentDentryUuid": parent_uuid, "name": name, "spaceId": space_id,
              "conflictHandleStrategy": conflict}, timeout=20)
    return _node_from_data(_expect_ok(r, "createfolder"))


def _oss_put(sts: dict, payload: bytes) -> None:
    try:
        import oss2  # lazy: optional dep
    except ImportError as e:
        raise RuntimeError("oss2 is required for uploads. Install with: pip install oss2") from e
    auth = oss2.StsAuth(sts["accessKeyId"], sts["accessKeySecret"], sts["accessToken"])
    bucket = oss2.Bucket(auth, "https://" + sts["endPoint"], sts["bucket"])
    bucket.put_object(sts["objectKey"], payload)


def upload_file(session: requests.Session, local_path: Path, parent_uuid: str,
                space_id: str, conflict: str = CONFLICT_AUTO_RENAME,
                target_name: str | None = None) -> Node:
    """uploadinfo → OSS STS PUT → commit. Returns the resulting Node (new dentry)."""
    name = target_name or local_path.name
    payload = local_path.read_bytes()
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/file/uploadinfo",
        json={"uploadType": "STS_SIGNATURE",
              "supportUploadTypes": ["STS_SIGNATURE", "HTTP_TO_CENTER"],
              "parentDentryUuid": parent_uuid, "spaceId": space_id,
              "fileSize": len(payload), "name": name,
              "conflictHandleStrategy": conflict, "multipart": False}, timeout=30)
    info = _expect_ok(r, "uploadinfo")
    _oss_put(info["stsSignatureInfo"], payload)
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/file/commit",
        json={"parentDentryUuid": parent_uuid, "uploadKey": info["uploadKey"],
              "fileSize": len(payload), "name": name,
              "conflictHandleStrategy": conflict}, timeout=30)
    return _node_from_data(_expect_ok(r, "commit"))


def overwrite_file(session: requests.Session, target_uuid: str, local_path: Path,
                   target_name: str | None = None) -> Node:
    """uploadInfoForUpdateVersion → OSS STS PUT → commitForUpdateVersion.
    Preserves dentry uuid, bumps version. target_name defaults to local filename."""
    name = target_name or local_path.name
    payload = local_path.read_bytes()
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/file/uploadInfoForUpdateVersion",
        json={"uploadType": "STS_SIGNATURE",
              "supportUploadTypes": ["STS_SIGNATURE", "HTTP_TO_CENTER"],
              "targetDentryUuid": target_uuid, "fileSize": len(payload),
              "name": name, "conflictHandleStrategy": CONFLICT_OVER_WRITE,
              "multipart": False}, timeout=30)
    info = _expect_ok(r, "uploadInfoForUpdateVersion")
    _oss_put(info["stsSignatureInfo"], payload)
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/file/commitForUpdateVersion",
        json={"targetDentryUuid": target_uuid, "uploadKey": info["uploadKey"],
              "fileSize": len(payload), "name": name,
              "conflictHandleStrategy": CONFLICT_OVER_WRITE}, timeout=30)
    return _node_from_data(_expect_ok(r, "commitForUpdateVersion"))


def rename_dentry(session: requests.Session, uuid: str, space_id: str, new_name: str) -> Node:
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/dentry/rename",
        json={"dentryUuid": uuid, "spaceId": space_id, "name": new_name}, timeout=20)
    return _node_from_data(_expect_ok(r, "rename"))


def recycle_dentry(session: requests.Session, uuid: str, space_id: str) -> None:
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/dentry/recycle",
        json={"dentryUuid": uuid, "spaceId": space_id}, timeout=20)
    _expect_ok(r, "recycle")


# Best-effort online-doc conversion. The web client uses an RPC bridge
# (/r/Adaptor/...) for the upload-temp-resource step which is not reachable
# from a cookie-authenticated HTTP request on the public cloud. We expose
# `import_document` for parity, but it currently surfaces the same 400/52600006
# the JS produces without a valid `downloadUrl`; until that surface is opened
# we fall back to a binary upload. Callers should not block uploads on this.

def import_document(session: requests.Session, parent_uuid: str, name: str,
                    suffix: str, download_url: str, file_size: int,
                    document_type: str) -> str:
    """POST /box/api/v2/import/document. Returns taskId for batchget polling.
    suffix like '.md'; document_type one of WORD / EXCEL / MIND."""
    body = {"parentDentryUuid": parent_uuid, "name": name, "suffix": suffix,
            "downloadUrl": download_url, "fileSize": file_size,
            "documentType": document_type}
    r = _post_with_proxy_retry(session, BASE + "/box/api/v2/import/document",
                               json=body, timeout=30)
    data = _expect_ok(r, "import/document")
    return data["id"]


def poll_import_tasks(session: requests.Session, task_ids: list[str],
                      timeout_s: int = 120, interval_s: float = 2.0) -> list[dict]:
    deadline = time.time() + timeout_s
    last: list[dict] = []
    while time.time() < deadline:
        r = _post_with_proxy_retry(session, BASE + "/box/api/v2/import/task/batchget",
                                   json={"taskIds": task_ids}, timeout=20)
        last = _expect_ok(r, "import/task/batchget") or []
        if last and all((rec.get("taskStatus") or rec.get("status")) in
                        ("SUCCESS", "ERROR", "TIMEOUT", "FAILED") for rec in last):
            return last
        time.sleep(interval_s)
    return last


# Suffix → documentType for /box/api/v2/import/document (server-side conversion).
# Informational only — see import_document() note re: /r/Adaptor.
IMPORT_DOC_TYPE = {
    ".docx": "WORD", ".doc": "WORD", ".txt": "WORD",
    ".md": "WORD", ".markdown": "WORD",
    ".xlsx": "EXCEL", ".xls": "EXCEL", ".xmind": "MIND",
}


# ---------------------------------------------------------------------------
# Playwright-based export for .adoc → .docx
# ---------------------------------------------------------------------------

class Exporter:
    """One headless Chromium session; opens a fresh tab per export to avoid state pollution."""

    def __init__(self, cookie_header: str):
        # Lazy import so the script runs without playwright when no .adoc nodes exist.
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"),
        )
        self._ctx.add_cookies(self._cookies_from_header(cookie_header))

    def _new_page(self):
        return self._ctx.new_page()

    @staticmethod
    def _cookies_from_header(header: str) -> list[dict]:
        out = []
        for kv in header.split(";"):
            kv = kv.strip()
            if "=" not in kv: continue
            n, _, v = kv.partition("=")
            out.append({"name": n.strip(), "value": v.strip(),
                        "domain": ".dingtalk.com", "path": "/"})
        return out

    def export_docx(self, doc_key: str, dentry_key: str, space_id: str) -> str:
        """Drive the editor to export the doc as .docx; return the signed OSS URL."""
        url = (f"{BASE}/note/edit?docId={doc_key}&docKey={doc_key}"
               f"&dentryKey={dentry_key}&docType=doc&workspaceId={space_id}"
               f"&dt_editor_toolbar=true&platform=pc")
        page = self._new_page()
        try:
            page.goto(url, wait_until="load", timeout=60000)
            # Editor uses long-poll/websocket; networkidle never fires. Fixed wait.
            page.wait_for_timeout(15000)

            captured: list[str] = []

            def _on_resp(r: Any) -> None:
                if "alidocs-body" in r.url and "/export/tempres/docx/" in r.url:
                    captured.append(r.url)

            page.on("response", _on_resp)
            page.mouse.click(1291, 22)            # open more-menu
            page.wait_for_timeout(2000)
            dl_item = page.get_by_text("下载到本地", exact=True).first
            box = dl_item.bounding_box()
            dl_item.hover()
            page.wait_for_timeout(2000)
            # Check for permission error tooltip (no download rights)
            no_perm = page.get_by_text("可查看/下载", exact=False)
            if no_perm.count() > 0:
                raise RuntimeError("no download permission: 需要「可查看/下载」及以上权限")
            # Word(.docx) submenu opens to the left; click by offset from 下载到本地
            page.mouse.click(box["x"] - 95, box["y"] + 51)
            for _ in range(30):
                if captured:
                    break
                page.wait_for_timeout(1000)
            if not captured:
                raise RuntimeError("no export response captured after 30s")
            return captured[0]
        finally:
            page.close()

    def export_xlsx(self, launch_url: str) -> str:
        """Drive the sheet editor to export as .xlsx; return the signed OSS URL."""
        sep = "&" if "?" in launch_url else "?"
        url = launch_url + sep + "dt_editor_toolbar=true&platform=pc"
        page = self._new_page()
        try:
            page.goto(url, wait_until="load", timeout=60000)
            page.wait_for_timeout(15000)

            captured: list[str] = []

            def _on_resp(r: Any) -> None:
                if "alidocs-body" in r.url and "/export/tempres/" in r.url:
                    captured.append(r.url)

            page.on("response", _on_resp)
            page.mouse.click(1291, 22)
            page.wait_for_timeout(800)
            page.get_by_text("下载为", exact=True).first.hover()
            page.wait_for_timeout(1500)
            page.get_by_text("Excel (.xlsx，表格整体)", exact=True).first.click()
            for _ in range(60):
                if captured:
                    break
                page.wait_for_timeout(1000)
            if not captured:
                raise RuntimeError("no export response captured after 60s")
            return captured[0]
        finally:
            page.close()

    def close(self) -> None:
        try: self._browser.close()
        finally: self._pw.stop()


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------

def walk(session: requests.Session, exporter: Exporter | None,
         node: Node, space_id: str, dst: Path, stats: dict) -> None:
    if node.is_folder:
        sub = dst / safe_name(node.name)
        sub.mkdir(parents=True, exist_ok=True)
        print(f"[dir]    {sub}")
        stats["folders"] += 1
        for child in list_children(session, node.uuid, space_id):
            walk(session, exporter, child, space_id, sub, stats)
        return

    base_path = dst / safe_name(node.name)

    # Online doc that we know how to export
    if node.ext in EXPORTABLE_TO_DOCX and exporter is not None:
        out = base_path.with_suffix(".docx")
        if out.exists():
            print(f"[skip]   {out}")
            stats["skipped"] += 1
            return
        try:
            launch = fetch_launch(session, node.uuid, space_id)
            doc_key, dentry_key = parse_launch_url(launch["launchInfo"]["launchUrl"])
            print(f"[export] {out}  (docKey={doc_key})", flush=True)
            oss = exporter.export_docx(doc_key, dentry_key, space_id)
            # OSS pre-signed URL — fetch without our auth headers (Origin would
            # trigger CORS rejection, Cookie/a-token are irrelevant).
            with requests.get(oss, stream=True, timeout=120,
                              headers={"User-Agent": "Mozilla/5.0"}) as rr:
                rr.raise_for_status()
                with out.open("wb") as f:
                    for chunk in rr.iter_content(chunk_size=64 * 1024):
                        if chunk: f.write(chunk)
            print(f"[ok]     {out}")
            stats["exported"] += 1
        except Exception as e:
            print(f"[err]    {out}: {e}")
            stats["errors"] += 1
        return

    # Online spreadsheet → export to .xlsx
    if node.ext in EXPORTABLE_TO_XLSX and exporter is not None:
        out = base_path.with_suffix(".xlsx")
        if out.exists():
            print(f"[skip]   {out}")
            stats["skipped"] += 1
            return
        try:
            launch = fetch_launch(session, node.uuid, space_id)
            launch_url = launch["launchInfo"]["launchUrl"]
            print(f"[export-xlsx] {out}", flush=True)
            oss = exporter.export_xlsx(launch_url)
            with requests.get(oss, stream=True, timeout=120,
                              headers={"User-Agent": "Mozilla/5.0"}) as rr:
                rr.raise_for_status()
                with out.open("wb") as f:
                    for chunk in rr.iter_content(chunk_size=64 * 1024):
                        if chunk: f.write(chunk)
            print(f"[ok]     {out}")
            stats["exported"] += 1
        except Exception as e:
            print(f"[err]    {out}: {e}")
            stats["errors"] += 1
        return

    # Other online doc types — leave as URL shortcut + meta sidecar
    if node.is_online_doc:
        shortcut = base_path.with_suffix(node.ext + ".url")
        if shortcut.exists():
            print(f"[skip]   {shortcut}")
            stats["skipped"] += 1
            return
        launch_url = f"{BASE}/i/nodes/{node.uuid}"
        try:
            launch = fetch_launch(session, node.uuid, space_id)
            launch_url = launch["launchInfo"]["launchUrl"]
        except Exception:
            pass
        write_url_shortcut(shortcut, launch_url)
        write_meta(base_path.with_suffix(node.ext + ".meta.json"), node)
        print(f"[doc->url] {shortcut}")
        stats["online_docs"] += 1
        return

    # True uploaded binary file
    if base_path.exists():
        print(f"[skip]   {base_path}")
        stats["skipped"] += 1
        return
    try:
        download_binary_file(session, node, space_id, base_path)
        print(f"[file]   {base_path}")
        stats["files"] += 1
    except Exception as e:
        print(f"[err]    {base_path}: {e}")
        stats["errors"] += 1


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie-file", required=True)
    ap.add_argument("--a-token", default=None)
    ap.add_argument("--root", required=True)
    ap.add_argument("--space-id", default=None)
    ap.add_argument("--out", default="./out")
    ap.add_argument("--no-playwright", action="store_true",
                    help="don't use Playwright; mirror online docs as .url shortcuts only")
    args = ap.parse_args()

    cookie = load_cookie(args.cookie_file)
    session = make_session(cookie, args.a_token)

    space_id = args.space_id
    if not space_id:
        r = session.get(f"{BASE}/i/nodes/{args.root}", timeout=20)
        m = re.search(r'"spaceId":"([^"]+)"', r.text)
        if not m:
            print("ERROR: could not discover spaceId from page; pass --space-id explicitly.", file=sys.stderr)
            return 2
        space_id = m.group(1)
        print(f"[info]   spaceId={space_id}")

    root = fetch_info(session, args.root, space_id)
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"[root]   {root.name} ({root.dentry_type}) → {out_root}")

    exporter = None if args.no_playwright else Exporter(cookie)
    stats = {"folders": 0, "files": 0, "exported": 0, "online_docs": 0, "skipped": 0, "errors": 0}
    try:
        walk(session, exporter, root, space_id, out_root, stats)
    finally:
        if exporter is not None:
            exporter.close()
    print(f"\ndone: {stats}")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
