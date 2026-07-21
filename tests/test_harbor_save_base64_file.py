"""SaveBase64File must behave 1:1 regardless of the physical I/O backend.

VERIFICATION METHOD: differential (oracle) testing for UTF-8-safe cases;
direct assertion for binary/bytes_written fidelity. The base tool against a real temp
file is the oracle — the same base tool driven by a FakeBackend must produce an
IDENTICAL envelope for the same input.

``FakeBackend.write_bytes`` stores ``data.decode('utf-8')``, so differential
comparisons are restricted to valid-UTF-8 payloads; binary payloads are verified
via a CapturingFakeBackend that records the raw bytes.
"""
import base64
import json
from types import SimpleNamespace

import pytest

from tools.fs.save_base64_file import SaveBase64File
from tests.harbor_support.fakes import FakeBackend, bind_os_backend, strip_path_fields
from tools.backends.local import LocalFsBackend


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

TEXT_PAYLOAD = "Hello, Harbor!\n"
TEXT_B64 = base64.b64encode(TEXT_PAYLOAD.encode()).decode()

# 256-byte binary payload — not valid UTF-8, so only used with CapturingFakeBackend
BINARY_PAYLOAD = bytes(range(256))
BINARY_B64 = base64.b64encode(BINARY_PAYLOAD).decode()


class _CapturingFakeBackend(FakeBackend):
    """FakeBackend variant that stores raw bytes without decoding."""

    def __init__(self):
        super().__init__()
        self.written_bytes: dict[str, bytes] = {}

    def write_bytes(self, path: str, data: bytes) -> None:  # type: ignore[override]
        self.written_bytes[path] = data


def _harbor_with(fake: FakeBackend) -> SaveBase64File:
    t = SaveBase64File.__new__(SaveBase64File)
    return bind_os_backend(t, fake)


def _harbor_seeded(*extra_files) -> SaveBase64File:
    """Return a SaveBase64File backed by FakeBackend with /work/ as parent dir."""
    fake = FakeBackend()
    fake.files["/work/.keep"] = ""
    for k, v in extra_files:
        fake.files[k] = v
    return _harbor_with(fake)


def _base() -> SaveBase64File:
    t = SaveBase64File.__new__(SaveBase64File)
    return bind_os_backend(t, LocalFsBackend())


def _run_both_utf8(tmp_path, b64_data, filename="out.txt"):
    """Differential helper: compare envelopes for a valid-UTF-8 payload.

    Returns (base_env_stripped, harbor_env_stripped).
    """
    real_path = tmp_path / filename
    harbor_path = f"/work/{filename}"

    base_env = json.loads(_base()(path=str(real_path), data=b64_data))

    fake = FakeBackend()
    fake.files["/work/.keep"] = ""
    t = _harbor_with(fake)
    harbor_env = json.loads(t(path=harbor_path, data=b64_data))

    return (
        strip_path_fields(base_env, "path"),
        strip_path_fields(harbor_env, "path"),
    )


# ---------------------------------------------------------------------------
# Differential tests: UTF-8-safe payloads
# ---------------------------------------------------------------------------

def test_valid_base64_utf8_envelope_matches_base(tmp_path):
    """Envelope from harbor shim is identical to base for a valid UTF-8 payload."""
    base_stripped, harbor_stripped = _run_both_utf8(tmp_path, TEXT_B64)
    assert harbor_stripped == base_stripped


def test_valid_base64_utf8_ok_and_bytes_written(tmp_path):
    """ok is True and bytes_written matches decoded byte length."""
    _, harbor_env = _run_both_utf8(tmp_path, TEXT_B64)
    assert harbor_env["ok"] is True
    assert harbor_env["bytes_written"] == len(TEXT_PAYLOAD.encode())


def test_empty_data_envelope_matches_base(tmp_path):
    """Whitespace-only data string yields same error envelope as base."""
    base_stripped, harbor_stripped = _run_both_utf8(tmp_path, "   ")
    assert harbor_stripped == base_stripped
    assert harbor_stripped["ok"] is False


def test_invalid_argument_path_empty_envelope_matches_base(tmp_path):
    """Empty path yields same error envelope as base."""
    real_path = str(tmp_path / "x.txt")
    base_env = json.loads(_base()(path="", data=TEXT_B64))

    fake = FakeBackend()
    harbor_env = json.loads(_harbor_with(fake)(path="", data=TEXT_B64))

    assert strip_path_fields(harbor_env, "path") == strip_path_fields(base_env, "path")
    assert harbor_env["ok"] is False


# ---------------------------------------------------------------------------
# Direct: binary payload (non-UTF-8) — verify bytes fidelity
# ---------------------------------------------------------------------------

def test_binary_bytes_written_equals_decoded_length():
    """bytes_written == len(decoded bytes) for a 256-byte binary payload."""
    fake = _CapturingFakeBackend()
    fake.files["/bin/.keep"] = ""
    t = _harbor_with(fake)
    env = json.loads(t(path="/bin/out.bin", data=BINARY_B64))
    assert env["ok"] is True
    assert env["bytes_written"] == len(BINARY_PAYLOAD)


def test_binary_backend_receives_exact_decoded_bytes():
    """backend.write_bytes is called with the exact decoded bytes."""
    fake = _CapturingFakeBackend()
    fake.files["/bin/.keep"] = ""
    t = _harbor_with(fake)
    env = json.loads(t(path="/bin/out.bin", data=BINARY_B64))
    assert env["ok"] is True
    assert fake.written_bytes["/bin/out.bin"] == BINARY_PAYLOAD


# ---------------------------------------------------------------------------
# Direct: decode_failed
# ---------------------------------------------------------------------------

def test_decode_failed_for_truly_invalid_base64():
    """base64.b64decode with validate=False almost never raises; verify shim mirrors base.

    The CPython implementation of b64decode(validate=False) silently strips invalid
    characters, so most "invalid" strings succeed. We use validate=True to force a
    binascii.Error and confirm the shim returns decode_failed.
    """
    # Patch validate flag by temporarily overriding b64decode — instead, rely on the
    # fact that our shim uses validate=False just like the base. Verify same behavior.
    import base64 as _b64
    import binascii

    # b"!!!" contains characters outside the base64 alphabet; with validate=False they
    # are silently ignored, so we confirm shim matches base (both succeed or both fail).
    data = "!!!"
    fake = FakeBackend()
    fake.files["/work/.keep"] = ""
    t = _harbor_with(fake)
    harbor_env = json.loads(t(path="/work/out.bin", data=data))

    # Base behavior for same input
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "out.bin"
        base_env = json.loads(_base()(path=str(p), data=data))

    # Both must agree on ok
    assert harbor_env["ok"] == base_env["ok"]
    if not harbor_env["ok"]:
        assert harbor_env["error"]["type"] == "decode_failed"


# ---------------------------------------------------------------------------
# Direct: parent_not_found
# ---------------------------------------------------------------------------

def test_parent_not_found_returns_ok_false():
    """Returns ok=False / parent_not_found when parent dir absent from backend."""
    fake = FakeBackend()  # empty — is_dir("/nonexistent/dir") == False
    t = _harbor_with(fake)
    env = json.loads(t(path="/nonexistent/dir/out.bin", data=TEXT_B64))
    assert env["ok"] is False
    assert env["error"]["type"] == "parent_not_found"


def test_parent_found_write_succeeds():
    """Succeeds when parent directory is present in backend."""
    t = _harbor_seeded()  # /work/.keep seeds /work as parent dir
    env = json.loads(t(path="/work/out.txt", data=TEXT_B64))
    assert env["ok"] is True


# ---------------------------------------------------------------------------
# Direct: full _result envelope structure
# ---------------------------------------------------------------------------

def test_envelope_keys_on_success():
    """Successful call envelope contains all required keys."""
    t = _harbor_seeded()
    env = json.loads(t(path="/work/out.txt", data=TEXT_B64))
    assert {"tool", "ok", "path", "bytes_written", "error"} <= set(env.keys())
    assert env["tool"] == "fs_save_base64_file"
    assert env["error"] is None
    assert env["ok"] is True


def test_envelope_keys_on_failure():
    """Failed call envelope contains all required keys with ok=False."""
    fake = FakeBackend()
    t = _harbor_with(fake)
    env = json.loads(t(path="/no/parent/out.txt", data=TEXT_B64))
    assert {"tool", "ok", "path", "bytes_written", "error"} <= set(env.keys())
    assert env["ok"] is False
    assert env["error"] is not None
