import json

from gc_core.tools.fs.file_info import FileInfo
from gc_core.utils.file_text import (
    BINARY_SAMPLE_BYTES,
    sha256_hex_of_bytes,
    sha256_hex_of_file_bytes,
)
from gc_testkit.helpers.tool_ctx import with_local_ctx


def test_file_info_missing_path(tmp_path):
    missing = tmp_path / "nope.txt"
    res = json.loads(with_local_ctx(FileInfo)(path=str(missing)))
    assert res["ok"] is True
    assert res["exists"] is False
    assert res["sha256"] is None


def test_file_info_fast_defaults(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")
    res = json.loads(with_local_ctx(FileInfo)(path=str(p)))
    assert res["ok"] is True
    assert res["exists"] is True
    assert res["type"] == "file"
    assert res["size_bytes"] == len("hello\nworld\n")
    assert res["is_binary"] is False
    assert res["encoding_guess"] == "utf-8"
    assert res["binary_detection_reason"] is None
    assert res["sha256"] is None
    assert res["line_count"] is None


def test_file_info_with_hash_and_line_count(tmp_path):
    text = "alpha\nbeta\n"
    p = tmp_path / "b.txt"
    p.write_text(text, encoding="utf-8")
    res = json.loads(
        with_local_ctx(FileInfo)(
            path=str(p),
            include_hash=True,
            include_line_count=True,
        )
    )
    assert res["sha256"] == sha256_hex_of_bytes(text.encode("utf-8"))
    assert res["hash_mode"] == "bytes"
    assert res["line_count"] == 2


def test_file_info_crlf_hash_is_raw_bytes(tmp_path):
    data = b"line1\r\nline2\r\n"
    p = tmp_path / "crlf.txt"
    p.write_bytes(data)
    res = json.loads(with_local_ctx(FileInfo)(path=str(p), include_hash=True))
    assert res["sha256"] == sha256_hex_of_bytes(data)
    assert res["hash_mode"] == "bytes"
    assert res["sha256"] != sha256_hex_of_bytes(data.replace(b"\r\n", b"\n"))


def test_file_info_large_utf8_markdown_not_binary(tmp_path):
    prefix = ("а" * 4095 + "x").encode("utf-8")
    data = prefix + "а".encode() + b"\n" * 340
    path = tmp_path / "report.md"
    path.write_bytes(data)
    assert len(data) > BINARY_SAMPLE_BYTES

    res = json.loads(
        with_local_ctx(FileInfo)(
            path=str(path),
            include_hash=True,
            include_line_count=True,
        )
    )
    assert res["is_binary"] is False
    assert res["encoding_guess"] == "utf-8"
    assert res["binary_detection_reason"] is None
    assert res["line_count"] == 340
    assert res["hash_mode"] == "bytes"
    assert res["sha256"] == sha256_hex_of_bytes(data)


def test_file_info_binary_hash_by_bytes(tmp_path):
    data = b"hello\x00world"
    path = tmp_path / "blob.bin"
    path.write_bytes(data)

    res = json.loads(
        with_local_ctx(FileInfo)(path=str(path), include_hash=True)
    )
    assert res["is_binary"] is True
    assert res["binary_detection_reason"] == "nul_byte"
    assert res["hash_mode"] == "bytes"
    assert res["sha256"] == sha256_hex_of_file_bytes(str(path))
    assert res["hash_skipped_reason"] is None


def test_file_info_cp1251_not_binary(tmp_path):
    text = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года.\n"
    ) * 3
    data = text.encode("cp1251")
    path = tmp_path / "legacy.txt"
    path.write_bytes(data)
    res = json.loads(
        with_local_ctx(FileInfo)(
            path=str(path), include_hash=True, include_line_count=True
        )
    )
    assert res["ok"] is True
    assert res["is_binary"] is False
    assert res["binary_detection_reason"] is None
    assert res["sha256"] == sha256_hex_of_bytes(data)
    assert res["encoding_guess"] == "cp1251"
    assert res["encoding_confidence"] >= 0.5
    assert res["line_count"] == 3


def test_file_info_broken_symlink_reports_symlink(tmp_path):
    broken = tmp_path / "broken_link"
    broken.symlink_to(str(tmp_path / "does_not_exist"))
    res = json.loads(
        with_local_ctx(FileInfo)(path=str(broken), follow_symlinks=False)
    )
    assert res["ok"] is True
    assert res["exists"] is True
    assert res["is_symlink"] is True
    assert res["type"] == "symlink"


def test_file_info_broken_symlink_follow_fails_stat(tmp_path):
    broken = tmp_path / "broken_link"
    broken.symlink_to(str(tmp_path / "does_not_exist"))
    res = json.loads(
        with_local_ctx(FileInfo)(path=str(broken), follow_symlinks=True)
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "stat_failed"


def test_file_info_symlink_to_file_follow(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("payload", encoding="utf-8")
    link = tmp_path / "via_link"
    link.symlink_to(real)
    res = json.loads(
        with_local_ctx(FileInfo)(
            path=str(link), follow_symlinks=True, include_hash=True
        )
    )
    assert res["ok"] is True
    assert res["type"] == "file"
    assert res["size_bytes"] == len("payload")
