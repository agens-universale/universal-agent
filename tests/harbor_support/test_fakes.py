from tests.harbor_support.fakes import FakeBackend, strip_path_fields


def test_fake_backend_roundtrip():
    b = FakeBackend({"/a.txt": "hello"})
    assert b.exists("/a.txt") and b.is_file("/a.txt")
    assert not b.exists("/missing")
    assert b.read("/a.txt") == "hello"
    assert b.getsize("/a.txt") == 5
    b.write("/a.txt", "bye")
    assert b.read("/a.txt") == "bye"


def test_strip_path_fields():
    assert strip_path_fields({"path": "/x", "ok": True}, "path") == {"ok": True}
