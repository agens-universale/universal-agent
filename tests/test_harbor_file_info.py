"""Differential oracle: ``FileInfo`` driven by the default (host) backend vs an
explicit ``FakeBackend`` override.

For each scenario we run the same content through the SAME ``FileInfo`` class:
  - once with no backend override (real temp file on the host fs — resolves
    to ``LocalFsBackend`` via the default ``_backend`` property fallback),
  - once with ``_backend = FakeBackend(...)`` on a virtual container path,
    same content in-memory.

We strip only genuinely environment-specific fields (``path``, ``modified_time``).
Every other field — including ``permissions`` and ``sha256`` — must match exactly.
"""
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from tests.harbor.fakes import FakeBackend, bind_os_backend
from tools.backends.local import LocalFsBackend
from tools.fs.file_info import FileInfo

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ENV_FIELDS = {"path", "modified_time"}


def _base_tool():
    """Instantiate FileInfo with host LocalFsBackend (real path)."""
    t = FileInfo.__new__(FileInfo)
    return bind_os_backend(t, LocalFsBackend())


def _harbor_tool(files: dict):
    """Instantiate FileInfo backed by FakeBackend (sandbox path)."""
    t = FileInfo.__new__(FileInfo)
    return bind_os_backend(t, FakeBackend(files))


def _strip(env: dict) -> dict:
    return {k: v for k, v in env.items() if k not in ENV_FIELDS}


# ---------------------------------------------------------------------------
# scenario helpers
# ---------------------------------------------------------------------------

def _compare(content: str, **call_kwargs):
    """Write a real temp file (base) and a fake backend on a virtual path
    (harbor) with the same content; compare."""
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write(content.encode("utf-8"))
        tmp = f.name
    try:
        os.chmod(tmp, 0o644)
        base_env = json.loads(_base_tool()(path=tmp, **call_kwargs))
        harbor_env = json.loads(_harbor_tool({"/work/f.txt": content})(path="/work/f.txt", **call_kwargs))
        return _strip(base_env), _strip(harbor_env)
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_text_file_no_hash():
    """Basic text file: all non-env fields must match (includes permissions)."""
    content = "hello\nworld\n"
    base, harbor = _compare(content)
    assert base == harbor, f"MISMATCH:\nbase={base}\nharbor={harbor}"


def test_text_file_with_crlf_and_hash():
    """Text file with \\r\\n: exposes hash divergence if shim uses raw-bytes hash."""
    content = "hello\r\nworld\r\n"
    base, harbor = _compare(content, include_hash=True)
    assert base == harbor, f"MISMATCH:\nbase={base}\nharbor={harbor}"


def test_text_file_include_hash():
    """Plain text + include_hash=True: sha256 and hash_mode must match exactly."""
    content = "the quick brown fox\n"
    base, harbor = _compare(content, include_hash=True)
    assert base == harbor, f"MISMATCH:\nbase={base}\nharbor={harbor}"


def test_text_file_include_line_count():
    """Line count must match."""
    content = "line1\nline2\nline3\n"
    base, harbor = _compare(content, include_hash=True, include_line_count=True)
    assert base == harbor, f"MISMATCH:\nbase={base}\nharbor={harbor}"


def test_binary_file_with_hash():
    """Binary content (nul byte): hash_mode='bytes', permissions must match."""
    content_bytes = b"BIN\x00DATA\xff"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as f:
        f.write(content_bytes)
        tmp = f.name
    try:
        os.chmod(tmp, 0o644)
        # FakeBackend stores str; for binary we encode/decode round-trip via latin-1
        content_str = content_bytes.decode("latin-1")
        base_env = json.loads(_base_tool()(path=tmp, include_hash=True))
        # For binary, the shim uses backend.sha256 = sha256(content.encode("utf-8"))
        # The base uses self._backend.sha256_bytes (raw bytes, LocalFsBackend).
        # We only compare non-hash binary fields here since FakeBackend can't faithfully
        # reproduce raw-bytes hash for non-UTF-8 content (latin-1 round-trip differs).
        harbor_env = json.loads(
            _harbor_tool({"/work/blob.bin": content_str})(path="/work/blob.bin", include_hash=True)
        )
        base_strip = _strip(base_env)
        harbor_strip = _strip(harbor_env)
        # permissions and classification must match; sha256 may differ for binary due to
        # encoding mismatch in FakeBackend (latin-1 content, utf-8 encode)
        for field in ("permissions", "is_binary", "exists", "is_file", "type", "hash_mode"):
            assert base_strip[field] == harbor_strip[field], (
                f"Field {field!r} mismatch: base={base_strip[field]!r}, harbor={harbor_strip[field]!r}"
            )
    finally:
        os.unlink(tmp)


def test_missing_path_returns_exists_false():
    """Missing path: ok=True, exists=False, no error."""
    path = "/nonexistent/path/file_for_harbor_test_xyz.txt"
    harbor_env = json.loads(_harbor_tool({})(path=path))
    assert harbor_env["ok"] is True
    assert harbor_env["exists"] is False
    assert harbor_env["error"] is None


def test_permissions_are_symbolic_not_octal():
    """Regression: permissions must be 9-char symbolic string, not octal '644'."""
    content = "check permissions\n"
    base, harbor = _compare(content)
    # base must be symbolic
    assert isinstance(base["permissions"], str)
    assert len(base["permissions"]) == 9, f"base permissions={base['permissions']!r}"
    # harbor must match base exactly (not '644')
    assert harbor["permissions"] == base["permissions"], (
        f"permissions mismatch: base={base['permissions']!r}, harbor={harbor['permissions']!r}"
    )


def test_hash_mode_is_text_for_text_file():
    """hash_mode must be 'text' for a text file (not 'bytes')."""
    content = "text content\n"
    base, harbor = _compare(content, include_hash=True)
    assert base["hash_mode"] == "text"
    assert harbor["hash_mode"] == "text"
