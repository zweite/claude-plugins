#!/usr/bin/env python3
"""Unit tests for the pure logic in mongodb.py (no live connection,
no pymongo required).

Run: python3 plugins/mongodb/scripts/test_mongodb.py
Live paths (test-conn / query / cache put) are validated separately
against a real MongoDB.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_module(env=None):
    """Import mongodb.py fresh with a controlled environment (settings
    resolve at import time)."""
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.startswith("MONGODB_") or k == "TAKU_DIR":
                del os.environ[k]
        if env:
            os.environ.update(env)
        spec = importlib.util.spec_from_file_location("mongodb_under_test", os.path.join(_HERE, "mongodb.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod  # support type annotations resolved via sys.modules
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_metadata_ops_are_safe(self):
        for op in ("ping", "listDatabases", "listCollections"):
            r = self.m.classify({"op": op})
            self.assertEqual(r["level"], "safe", op)

    def test_find_with_small_limit_safe(self):
        r = self.m.classify({"op": "find", "collection": "x.y", "limit": 50})
        self.assertEqual(r["level"], "safe")

    def test_find_without_limit_is_low(self):
        r = self.m.classify({"op": "find", "collection": "x.y"})
        self.assertEqual(r["level"], "low")
        self.assertIn("no_limit", r["factors"])

    def test_find_with_huge_limit_is_low(self):
        r = self.m.classify({"op": "find", "collection": "x.y", "limit": 100000})
        self.assertEqual(r["level"], "low")
        self.assertIn("large_limit", r["factors"])

    def test_aggregate_respects_pipeline_limit(self):
        spec = {"op": "aggregate", "collection": "x.y", "pipeline": [{"$match": {}}, {"$limit": 10}]}
        self.assertEqual(self.m.classify(spec)["level"], "safe")

    def test_aggregate_without_limit_is_low(self):
        spec = {"op": "aggregate", "collection": "x.y", "pipeline": [{"$match": {}}]}
        self.assertEqual(self.m.classify(spec)["level"], "low")

    def test_updateOne_no_filter_is_high(self):
        r = self.m.classify({"op": "updateOne", "collection": "x.y", "update": {"$set": {"a": 1}}})
        self.assertEqual(r["level"], "high")

    def test_updateOne_with_filter_is_medium(self):
        r = self.m.classify({"op": "updateOne", "collection": "x.y",
                             "filter": {"_id": 1}, "update": {"$set": {"a": 1}}})
        self.assertEqual(r["level"], "medium")

    def test_deleteMany_empty_filter_is_high(self):
        r = self.m.classify({"op": "deleteMany", "collection": "x.y", "filter": {}})
        self.assertEqual(r["level"], "high")

    def test_runCommand_drop_is_high(self):
        r = self.m.classify({"op": "runCommand", "command": {"drop": "orders"}})
        self.assertEqual(r["level"], "high")
        self.assertEqual(r["operations"], ["drop"])

    def test_runCommand_serverStatus_is_low(self):
        r = self.m.classify({"op": "runCommand", "command": {"serverStatus": 1}})
        self.assertEqual(r["level"], "low")

    def test_missing_op_is_high(self):
        r = self.m.classify({})
        self.assertEqual(r["level"], "high")
        self.assertIn("missing_op", r["factors"])

    def test_spec_not_object_is_high(self):
        r = self.m.classify("DROP TABLE x")  # malformed
        self.assertEqual(r["level"], "high")


class SchemaInferenceTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_mixed_field_types_collected(self):
        docs = [
            {"_id": 1, "name": "a",  "tags": ["x"], "n": 3},
            {"_id": 2, "name": "b",  "tags": ["y"], "n": 3.5},
            {"_id": 3, "name": None, "tags": [], "extra": True},
        ]
        schema = self.m.infer_schema(docs)
        by_path = {s["path"]: s["types"] for s in schema}
        self.assertIn("int", by_path["_id"])
        self.assertEqual(by_path["name"], ["null", "string"])
        self.assertEqual(by_path["tags"], ["array"])
        self.assertEqual(by_path["n"], ["double", "int"])
        self.assertEqual(by_path["extra"], ["bool"])

    def test_bool_not_int(self):
        # bool is a subclass of int in Python; we must not collapse them.
        self.assertEqual(self.m._bson_type(True), "bool")
        self.assertEqual(self.m._bson_type(1), "int")

    def test_empty_input_returns_empty(self):
        self.assertEqual(self.m.infer_schema([]), [])
        self.assertEqual(self.m.infer_schema([None, "scalar"]), [])


class RedactTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_uri_password_masked(self):
        s = "cannot connect: mongodb://alice:secret@host:27017/db"
        self.assertNotIn("secret", self.m.redact(s))
        self.assertIn("[REDACTED]", self.m.redact(s))

    def test_srv_uri_masked(self):
        s = 'failed: mongodb+srv://alice:s3cret@cluster.mongodb.net/'
        self.assertNotIn("s3cret", self.m.redact(s))

    def test_json_password_masked(self):
        self.assertNotIn("hunter2",
                         self.m.redact('{"password": "hunter2", "host": "x"}'))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_flat_config(self):
        cfg = {"uri": "mongodb://x"}
        prof, name, uses = self.m._resolve_profile(cfg, "")
        self.assertEqual(name, "default")
        self.assertFalse(uses)
        self.assertEqual(prof["uri"], "mongodb://x")

    def test_profile_merged_with_base(self):
        cfg = {"cache_ttl_days": 7, "default_profile": "prod",
               "profiles": {"prod": {"uri": "p"}, "stg": {"uri": "s"}}}
        prof, name, uses = self.m._resolve_profile(cfg, "stg")
        self.assertEqual(name, "stg")
        self.assertTrue(uses)
        self.assertEqual(prof["uri"], "s")
        self.assertEqual(prof["cache_ttl_days"], 7)

    def test_single_profile_picked_automatically(self):
        cfg = {"profiles": {"only": {"uri": "o"}}}
        _, name, _ = self.m._resolve_profile(cfg, "")
        self.assertEqual(name, "only")

    def test_cache_namespaced_per_profile(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "mongodb.json")
            import json as _json
            _json.dump({"profiles": {"prod": {"uri": "u"}, "stg": {"uri": "u"}},
                        "default_profile": "prod"}, open(cfg_path, "w"))
            m1 = _load_module({"TAKU_DIR": d, "MONGODB_PROFILE": "prod"})
            m2 = _load_module({"TAKU_DIR": d, "MONGODB_PROFILE": "stg"})
            self.assertNotEqual(m1.CACHE_DIR, m2.CACHE_DIR)
            self.assertTrue(m1.CACHE_DIR.endswith("/prod"))
            self.assertTrue(m2.CACHE_DIR.endswith("/stg"))


class CollectionValidationTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_valid(self):
        self.assertEqual(self.m._collection_ok("appdb.orders"), ("appdb", "orders"))
        self.assertEqual(self.m._collection_ok("a-b.c_d"), ("a-b", "c_d"))

    def test_no_dot_rejected(self):
        with self.assertRaises(SystemExit):
            self.m._collection_ok("orders")

    def test_injection_rejected(self):
        # trailing whitespace is stripped (handles shell-piped input), so only
        # characters that survive .strip() are checked here.
        for bad in ["db.x; drop", "db x.coll", "$db.coll", "db.$where", "db.coll\nrow"]:
            with self.assertRaises(SystemExit, msg=repr(bad)):
                self.m._collection_ok(bad)


class CacheFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_hit(self):
        import datetime as dt
        e = {"cached_at": dt.datetime.now().isoformat()}
        status, age = self.m._freshness(e, 30)
        self.assertEqual(status, "hit")
        self.assertIsNotNone(age)

    def test_stale(self):
        import datetime as dt
        old = (dt.datetime.now() - dt.timedelta(days=40)).isoformat()
        self.assertEqual(self.m._freshness({"cached_at": old}, 30)[0], "stale")

    def test_unparseable_is_stale(self):
        self.assertEqual(self.m._freshness({"cached_at": "garbage"}, 30)[0], "stale")


class PipelineLimitTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()

    def test_finds_explicit_limit(self):
        self.assertEqual(self.m._pipeline_limit([{"$match": {}}, {"$limit": 50}]), 50)

    def test_none_when_absent(self):
        self.assertIsNone(self.m._pipeline_limit([{"$match": {}}]))

    def test_non_int_limit_returns_none(self):
        self.assertIsNone(self.m._pipeline_limit([{"$limit": "bad"}]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
