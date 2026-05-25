#!/usr/bin/env python3
"""End-to-end smoke test for alidocs push/sync.

Creates a scratch folder under the user's personal space (spaceId
nb9XJlvApJp5PGyA, parent QOG9lyrgJPB7AbY1TBDmmGAKWzN67Mw4), pushes a synthetic
local tree, exercises each on-conflict mode, then recycles the scratch folder.

Run:
  python3 plugins/alidocs/scripts/test_upload_e2e.py

The script reuses the existing ~/.taku/alidocs.json cookie.
"""
from __future__ import annotations
import atexit
import json
import os
import subprocess
import sys
import tempfile
import time
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dl_alidocs as dl  # noqa: E402

SPACE = "nb9XJlvApJp5PGyA"
PARENT = "QOG9lyrgJPB7AbY1TBDmmGAKWzN67Mw4"
CLI = [sys.executable, str(HERE / "alidocs.py")]

cfg = json.load(open(os.path.expanduser("~/.taku/alidocs.json")))
cookie = dl.load_cookie(os.path.expanduser(cfg["cookie_file"]))
session = dl.make_session(cookie)

scratch_name = f"_zz_e2e_{int(time.time())}"
print(f"[setup]  creating scratch folder {scratch_name}")
folder = dl.create_folder(session, PARENT, scratch_name, SPACE,
                          conflict=dl.CONFLICT_AUTO_RENAME)
print(f"[setup]  scratch uuid={folder.uuid}")
SCRATCH_UUID = folder.uuid
SCRATCH_LINK = f"https://alidocs.dingtalk.com/i/nodes/{SCRATCH_UUID}?spaceId={SPACE}"

def atexit_recycle():
    try:
        dl.recycle_dentry(session, SCRATCH_UUID, SPACE)
        print(f"[teardown] recycled {SCRATCH_UUID}")
    except Exception as e:
        print(f"[teardown] EXC {e!r}", file=sys.stderr)
atexit.register(atexit_recycle)

# Build synthetic local tree
tmp = Path(tempfile.mkdtemp(prefix="alidocs_e2e_"))
def write(p: Path, content: bytes | str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str): p.write_text(content, encoding="utf-8")
    else: p.write_bytes(content)

write(tmp / "hello.txt", "hello world\n")
write(tmp / "readme.md", "# README\n\nSome **markdown**.\n")
write(tmp / "blob.bin", bytes(range(256)) * 4)         # 1 KB binary
write(tmp / "nested/a.txt", "alpha\n")
write(tmp / "nested/sub/b.txt", "beta\n")
write(tmp / "nested/sub/c.bin", b"\xff" * 32)
write(tmp / ".hidden", "should-be-excluded\n")
write(tmp / "__pycache__/junk.pyc", b"pyc-data")
print(f"[setup]  local tree at {tmp}")

def run(*args: str, expect_ok: bool = True, **kw) -> subprocess.CompletedProcess:
    proc = subprocess.run(CLI + list(args), capture_output=True, text=True, **kw)
    if expect_ok and proc.returncode != 0:
        print("STDOUT:", proc.stdout, file=sys.stderr)
        print("STDERR:", proc.stderr, file=sys.stderr)
        raise RuntimeError(f"CLI failed: {' '.join(args)}")
    return proc

def assert_(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)

# Phase 1: dry-run
print("\n=== phase 1: dry-run push ===")
p = run("push", "--src", str(tmp), "--dest", SCRATCH_LINK, "--dry-run")
print(p.stdout)
assert_("upload" in p.stdout or "+ file" in p.stdout, "dry-run should mention upload")

# Phase 2: actual push (default skip-if-same)
print("\n=== phase 2: real push ===")
p = run("push", "--src", str(tmp), "--dest", SCRATCH_LINK, "--yes")
print(p.stdout)
last = json.loads(p.stdout.strip().splitlines()[-1])
assert_(last["ok"], f"push failed: {last}")
stats = last["stats"]
assert_(stats["upload"] >= 5, f"expected >=5 uploads, got {stats}")  # 5 files (excludes .hidden + __pycache__)
assert_(stats["mkdir"] >= 2, f"expected >=2 mkdirs, got {stats}")
assert_(stats["errors"] == 0, f"errors: {stats}")

# Verify remote tree mirrors local
print("\n=== phase 3: verify remote mirrors local ===")
def remote_files() -> dict:
    return dl._list_remote_tree if False else None  # noqa
# use module helper directly
sys.path.insert(0, str(HERE))
import alidocs  # noqa: E402
remote = alidocs._list_remote_tree(session, SCRATCH_UUID, SPACE)
print(f"remote keys: {sorted(remote.keys())}")
for k in ["hello.txt", "readme.md", "blob.bin", "nested", "nested/a.txt",
          "nested/sub", "nested/sub/b.txt", "nested/sub/c.bin"]:
    assert_(k in remote, f"missing remote: {k}")
assert_(".hidden" not in remote, "hidden file should be excluded by default")
assert_("__pycache__" not in remote, "__pycache__ should be excluded by default")

# Phase 4: idempotency — second push should skip everything
print("\n=== phase 4: re-push (idempotency) ===")
p = run("push", "--src", str(tmp), "--dest", SCRATCH_LINK, "--yes")
print(p.stdout)
last = json.loads(p.stdout.strip().splitlines()[-1])
assert_(last["ok"], f"second push failed: {last}")
s = last["stats"]
# All files should "skip", folders should "skip" (folder exists)
assert_(s.get("upload", 0) == 0, f"expected zero uploads on idempotent re-push, got {s}")
assert_(s.get("overwrite", 0) == 0, f"unexpected overwrites: {s}")
assert_(s.get("skip", 0) >= 5, f"expected skips on re-push, got {s}")

# Phase 5: conflict modes — mutate hello.txt then test each
print("\n=== phase 5: conflict modes ===")
write(tmp / "hello.txt", "hello world version 2 - longer content here\n")

# skip-if-same DETECTS the size change → overwrite path
p = run("push", "--src", str(tmp), "--dest", SCRATCH_LINK, "--yes")
last = json.loads(p.stdout.strip().splitlines()[-1])
print(f"[skip-if-same after size-change] {last['stats']}")
assert_(last["stats"].get("overwrite", 0) >= 1, "size-changed file should trigger overwrite")

# overwrite mode — forces all files to overwrite (even unchanged)
write(tmp / "hello.txt", "hello world version 3 forced\n")
p = run("push", "--src", str(tmp), "--dest", SCRATCH_LINK,
        "--on-conflict", "overwrite", "--yes")
last = json.loads(p.stdout.strip().splitlines()[-1])
print(f"[overwrite mode] {last['stats']}")
assert_(last["stats"].get("overwrite", 0) >= 5, f"overwrite mode should hit all files; got {last['stats']}")

# rename mode — should create a "(1)" duplicate for hello.txt
write(tmp / "hello.txt", "hello v4\n")
p = run("push", "--src", str(tmp), "--dest", SCRATCH_LINK,
        "--on-conflict", "rename", "--yes")
last = json.loads(p.stdout.strip().splitlines()[-1])
print(f"[rename mode] {last['stats']}")
remote = alidocs._list_remote_tree(session, SCRATCH_UUID, SPACE)
rename_keys = [k for k in remote if "hello" in k]
print(f"  hello* dentries: {rename_keys}")
assert_(any("(1)" in k or "(" in k for k in rename_keys), f"rename should produce a (N) variant; got {rename_keys}")

# Phase 6: prune
print("\n=== phase 6: prune ===")
# Remove blob.bin locally; push with --prune should recycle remote
(tmp / "blob.bin").unlink()
p = run("push", "--src", str(tmp), "--dest", SCRATCH_LINK,
        "--prune", "--yes")
last = json.loads(p.stdout.strip().splitlines()[-1])
print(f"[prune] {last['stats']}")
assert_(last["stats"].get("prune", 0) >= 1, f"prune should recycle at least blob.bin: {last}")
remote = alidocs._list_remote_tree(session, SCRATCH_UUID, SPACE)
assert_("blob.bin" not in remote, "blob.bin should be recycled")

# Phase 7: create-dest
print("\n=== phase 7: --create-dest auto-path ===")
sub_path = f"{SCRATCH_LINK}/auto/nested/leaf"
p = run("push", "--src", str(tmp / "nested" / "sub"), "--create-dest", sub_path, "--yes")
last = json.loads(p.stdout.strip().splitlines()[-1])
print(f"[create-dest] {last['stats']}")
assert_(last["ok"], f"create-dest push failed: {last}")
remote = alidocs._list_remote_tree(session, SCRATCH_UUID, SPACE)
assert_("auto/nested/leaf" in remote, f"create-dest should walk-or-create folders; remote={sorted(remote)}")
assert_("auto/nested/leaf/b.txt" in remote, f"files should land at the leaf folder; remote={sorted(remote)}")

print("\n=== ALL E2E TESTS PASSED ===")
shutil.rmtree(tmp, ignore_errors=True)
