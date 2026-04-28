import os
import sys
from pathlib import Path

import pytest

from mcp_office_reader.errors import ErrorCategory, OfficeReaderError
from mcp_office_reader.security import validate_path


def test_path_inside_allowed_dir(tmp_path: Path):
    f = tmp_path / "report.docx"
    f.write_bytes(b"x")
    result = validate_path(str(f), allowed_dirs=[str(tmp_path)], allow_any=False)
    assert result == f.resolve()


def test_path_outside_allowed_denied(tmp_path: Path):
    outside = tmp_path / "outside.doc"
    outside.write_bytes(b"x")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    with pytest.raises(OfficeReaderError) as exc:
        validate_path(str(outside), allowed_dirs=[str(allowed)], allow_any=False)
    assert exc.value.category == ErrorCategory.PATH_NOT_ALLOWED


def test_traversal_via_dotdot_denied(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    bad = str(allowed / ".." / "secrets.doc")
    with pytest.raises(OfficeReaderError):
        validate_path(bad, allowed_dirs=[str(allowed)], allow_any=False)


def test_allow_any_bypasses_check(tmp_path: Path):
    somewhere = tmp_path / "anywhere.doc"
    somewhere.write_bytes(b"x")
    result = validate_path(str(somewhere), allowed_dirs=[], allow_any=True)
    assert result == somewhere.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink needs admin on Windows")
def test_symlink_escaping_allowed_dir_denied(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret.doc"
    secret.write_bytes(b"x")
    link = allowed / "shortcut.doc"
    os.symlink(secret, link)
    with pytest.raises(OfficeReaderError):
        validate_path(str(link), allowed_dirs=[str(allowed)], allow_any=False)
