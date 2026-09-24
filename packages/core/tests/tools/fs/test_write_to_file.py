import json

from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.helpers.tool_ctx import with_local_ctx


def test_write_overwrite_and_backup(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("old", encoding="utf-8")
    res = json.loads(
        with_local_ctx(WriteToFile)(path=str(p), content="new", backup=True)
    )
    assert res["ok"] is True
    assert res["backup_path"] == f"{p}.bak"
    assert p.read_text(encoding="utf-8") == "new"
    assert (tmp_path / "x.txt.bak").read_text(encoding="utf-8") == "old"


def test_write_modes_and_newline(tmp_path):
    p = tmp_path / "m.txt"
    create = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(p), content="a\r\nb\r\n", mode="create_only", newline="lf"
        )
    )
    assert create["ok"] is True and p.read_text(encoding="utf-8") == "a\nb\n"
    create_again = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(p), content="x", mode="create_only"
        )
    )
    assert (
        create_again["ok"] is False
        and create_again["error"]["type"] == "already_exists"
    )
    missing = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(tmp_path / "missing.txt"),
            content="x",
            mode="fail_if_missing",
        )
    )
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"


def test_write_validation(tmp_path):
    tool = with_local_ctx(WriteToFile)
    rel = json.loads(tool(path="x.txt", content="a"))
    assert rel["ok"] is False and rel["error"]["field"] == "path"
    bad_mode = json.loads(
        tool(path=str(tmp_path / "x.txt"), content="a", mode="bad")
    )
    assert bad_mode["ok"] is False and bad_mode["error"]["field"] == "mode"


def test_write_cp1251_and_alias(tmp_path):
    import hashlib

    p = tmp_path / "ru.txt"
    res = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(p), content="Привет", encoding="windows-1251"
        )
    )
    assert res["ok"] is True
    assert res["encoding_used"] == "cp1251"
    assert p.read_bytes() == "Привет".encode("cp1251")
    assert res["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_write_unsupported_encoding(tmp_path):
    p = tmp_path / "a.txt"
    res = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(p), content="x", encoding="shift_jis"
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "unsupported_encoding"
    assert not p.exists()


def test_write_encode_failed_keeps_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"old")
    res = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(p), content="Привет", encoding="latin-1"
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "encode_failed"
    assert p.read_bytes() == b"old"
