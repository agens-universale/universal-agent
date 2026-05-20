import time

from science_mcp.cache import DiskCache, hash_key


def test_set_get_roundtrip(tmp_path):
    c = DiskCache(tmp_path, enabled=True)
    c.set("k", {"foo": "bar"}, ttl_sec=10)
    assert c.get("k") == {"foo": "bar"}


def test_miss_returns_none(tmp_path):
    assert DiskCache(tmp_path, enabled=True).get("nope") is None


def test_ttl_expiry(tmp_path):
    c = DiskCache(tmp_path, enabled=True)
    c.set("k", "v", ttl_sec=0.05)
    time.sleep(0.1)
    assert c.get("k") is None


def test_disabled_is_noop(tmp_path):
    c = DiskCache(tmp_path, enabled=False)
    c.set("k", "v", ttl_sec=10)
    assert c.get("k") is None


def test_hash_key_stable():
    a = hash_key("openalex", "/works", {"q": "crispr", "limit": 1})
    b = hash_key("openalex", "/works", {"limit": 1, "q": "crispr"})
    assert a == b


def test_hash_key_distinct_per_provider():
    a = hash_key("openalex", "/works", {"q": "x"})
    b = hash_key("crossref", "/works", {"q": "x"})
    assert a != b
