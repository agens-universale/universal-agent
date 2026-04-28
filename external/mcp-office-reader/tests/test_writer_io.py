from __future__ import annotations

import base64

import pytest

from mcp_office_reader.errors import ErrorCategory, OfficeReaderError
from mcp_office_reader.writer_io import (
    encode_base64_checked,
    write_bytes_checked,
)


def test_write_bytes_checked_happy(tmp_path):
    target = tmp_path / "out.bin"
    n = write_bytes_checked(
        str(target), b"hello",
        output_allowed_dirs=[str(tmp_path)],
        allow_any=False,
        overwrite=False,
    )
    assert n == 5
    assert target.read_bytes() == b"hello"


def test_write_bytes_checked_refuses_existing(tmp_path):
    target = tmp_path / "out.bin"
    target.write_bytes(b"x")
    with pytest.raises(OfficeReaderError) as ei:
        write_bytes_checked(
            str(target), b"y",
            output_allowed_dirs=[str(tmp_path)],
            allow_any=False,
            overwrite=False,
        )
    assert ei.value.category == ErrorCategory.FILE_EXISTS


def test_write_bytes_checked_overwrite(tmp_path):
    target = tmp_path / "out.bin"
    target.write_bytes(b"x")
    write_bytes_checked(
        str(target), b"new",
        output_allowed_dirs=[str(tmp_path)],
        allow_any=False,
        overwrite=True,
    )
    assert target.read_bytes() == b"new"


def test_write_bytes_checked_outside_whitelist(tmp_path):
    with pytest.raises(OfficeReaderError) as ei:
        write_bytes_checked(
            str(tmp_path.parent / "other" / "out.bin"), b"x",
            output_allowed_dirs=[str(tmp_path)],
            allow_any=False,
            overwrite=False,
        )
    assert ei.value.category == ErrorCategory.PATH_NOT_ALLOWED


def test_encode_base64_happy():
    out = encode_base64_checked(b"hello", max_base64_bytes=1024)
    assert out["raw_bytes"] == 5
    assert base64.b64decode(out["content_base64"]) == b"hello"


def test_encode_base64_too_large():
    with pytest.raises(OfficeReaderError) as ei:
        encode_base64_checked(b"x" * 10, max_base64_bytes=4)
    assert ei.value.category == ErrorCategory.OUTPUT_TOO_LARGE
