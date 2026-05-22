#!/usr/bin/env python3
"""Unit tests for the pure logic in mysql.py (no live connection).

Run: python3 plugins/mysql/scripts/test_mysql.py
Live auth/query paths are validated separately via `mysql.py test-conn`
against a real server.
"""
import hashlib
import importlib.util
import os
import struct
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_module(env=None):
    """Import mysql.py fresh with a controlled environment (settings resolve
    at import time)."""
    saved = dict(os.environ)
    try:
        # strip MYSQL_*/TAKU_DIR so tests are hermetic, then apply overrides
        for k in list(os.environ):
            if k.startswith("MYSQL_") or k == "TAKU_DIR":
                del os.environ[k]
        if env:
            os.environ.update(env)
        spec = importlib.util.spec_from_file_location("mysql_under_test", os.path.join(_HERE, "mysql.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod  # dataclass resolves annotations via sys.modules
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


# Reference scramble implementations, written independently from the ones in
# mysql.py, so a typo/regression in either side shows up as a mismatch.
def ref_native(pw: bytes, salt: bytes) -> bytes:
    if not pw:
        return b""
    stage1 = hashlib.sha1(pw).digest()
    stage2 = hashlib.sha1(stage1).digest()
    res = hashlib.sha1(salt + stage2).digest()
    return bytes(x ^ y for x, y in zip(stage1, res))


def ref_sha2(pw: bytes, salt: bytes) -> bytes:
    if not pw:
        return b""
    a = hashlib.sha256(pw).digest()
    b = hashlib.sha256(hashlib.sha256(a).digest() + salt).digest()
    return bytes(x ^ y for x, y in zip(a, b))


class ScrambleTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()
        self.salt = bytes(range(20))

    def test_native_matches_reference(self):
        self.assertEqual(self.m._scramble_native(b"hunter2", self.salt),
                         ref_native(b"hunter2", self.salt))

    def test_native_length_and_empty(self):
        self.assertEqual(len(self.m._scramble_native(b"x", self.salt)), 20)
        self.assertEqual(self.m._scramble_native(b"", self.salt), b"")

    def test_sha2_matches_reference(self):
        self.assertEqual(self.m._scramble_sha2(b"hunter2", self.salt),
                         ref_sha2(b"hunter2", self.salt))

    def test_sha2_length_and_empty(self):
        self.assertEqual(len(self.m._scramble_sha2(b"x", self.salt)), 32)
        self.assertEqual(self.m._scramble_sha2(b"", self.salt), b"")

    def test_native_and_sha2_differ(self):
        self.assertNotEqual(self.m._scramble_native(b"p", self.salt),
                            self.m._scramble_sha2(b"p", self.salt))


class LencTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_lenc_int_one_byte(self):
        self.assertEqual(self.m._lenc_int(bytes([0x05]), 0), (5, 1))

    def test_lenc_int_two_byte(self):
        data = bytes([0xFC]) + struct.pack("<H", 300)
        self.assertEqual(self.m._lenc_int(data, 0), (300, 3))

    def test_lenc_int_eight_byte(self):
        data = bytes([0xFE]) + struct.pack("<Q", 1 << 40)
        self.assertEqual(self.m._lenc_int(data, 0), (1 << 40, 9))

    def test_lenc_str(self):
        data = bytes([3]) + b"abc" + b"tail"
        val, pos = self.m._lenc_str(data, 0)
        self.assertEqual(val, b"abc")
        self.assertEqual(pos, 4)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_flat_config_is_default(self):
        cfg = {"host": "h", "user": "u"}
        prof, name, uses = self.m._resolve_profile(cfg, "")
        self.assertEqual(name, "default")
        self.assertFalse(uses)
        self.assertEqual(prof["host"], "h")

    def test_profile_selected_and_merged_with_base(self):
        cfg = {"cache_ttl_days": 7, "default_profile": "prod",
               "profiles": {"prod": {"host": "p"}, "stg": {"host": "s"}}}
        prof, name, uses = self.m._resolve_profile(cfg, "stg")
        self.assertEqual(name, "stg")
        self.assertTrue(uses)
        self.assertEqual(prof["host"], "s")
        self.assertEqual(prof["cache_ttl_days"], 7)  # base default merged in

    def test_single_profile_no_default_picked_automatically(self):
        cfg = {"profiles": {"only": {"host": "o"}}}
        _, name, uses = self.m._resolve_profile(cfg, "")
        self.assertEqual(name, "only")
        self.assertTrue(uses)


class CacheFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_hit_when_recent(self):
        import datetime
        entry = {"cached_at": datetime.datetime.now().isoformat()}
        status, age = self.m._freshness(entry, 30)
        self.assertEqual(status, "hit")
        self.assertIsNotNone(age)

    def test_stale_when_old(self):
        import datetime
        old = (datetime.datetime.now() - datetime.timedelta(days=40)).isoformat()
        status, _ = self.m._freshness({"cached_at": old}, 30)
        self.assertEqual(status, "stale")

    def test_stale_when_unparsable(self):
        status, age = self.m._freshness({"cached_at": "nonsense"}, 30)
        self.assertEqual(status, "stale")
        self.assertIsNone(age)


class ConfigBoolTests(unittest.TestCase):
    def test_ssl_env_true(self):
        m = _load_module({"MYSQL_SSL": "1"})
        self.assertTrue(m.SSL_ENABLED)

    def test_ssl_default_false(self):
        m = _load_module()
        self.assertFalse(m.SSL_ENABLED)

    def test_ssl_verify_default_true(self):
        m = _load_module()
        self.assertTrue(m.SSL_VERIFY)

    def test_ssl_verify_env_off(self):
        m = _load_module({"MYSQL_SSL_VERIFY": "0"})
        self.assertFalse(m.SSL_VERIFY)

    def test_default_port_3306(self):
        with tempfile.TemporaryDirectory() as d:
            m = _load_module({"TAKU_DIR": d})
            saved = dict(os.environ)
            os.environ.update({"MYSQL_HOST": "h", "MYSQL_USER": "u"})  # load_creds reads env live
            try:
                creds = m.load_creds()
            finally:
                os.environ.clear()
                os.environ.update(saved)
            self.assertEqual(creds.port, 3306)


class TableValidationTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_valid_names(self):
        self.assertEqual(self.m._table_ok("db.orders"), "db.orders")
        self.assertEqual(self.m._table_ok("orders"), "orders")

    def test_invalid_names_exit(self):
        for bad in ["db; DROP TABLE x", "db orders", "db.orders;", "-x"]:
            with self.assertRaises(SystemExit):
                self.m._table_ok(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
