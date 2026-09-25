from gc_testkit.harbor_fakes import FakeBackend, strip_path_fields


async def test_fake_backend_roundtrip():
    b = FakeBackend({"/a.txt": "hello"})
    assert await b.exists("/a.txt") and await b.is_file("/a.txt")
    assert not await b.exists("/missing")
    assert await b.read("/a.txt") == "hello"
    assert await b.getsize("/a.txt") == 5
    await b.write("/a.txt", "bye")
    assert await b.read("/a.txt") == "bye"


def test_strip_path_fields():
    assert strip_path_fields({"path": "/x", "ok": True}, "path") == {
        "ok": True
    }
