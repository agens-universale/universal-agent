"""WriteToFile must behave 1:1 regardless of the physical I/O backend.

VERIFICATION METHOD: differential (oracle) testing. The base tool against a real temp
file is the oracle — the same base tool driven by an in-memory FakeBackend must produce
an IDENTICAL envelope (path/backup_path fields stripped) and IDENTICAL resulting file
content for the same input. `_run_both` feeds one case through both backends.
"""

import json

import pytest
from gc_core.backends.local import LocalFsBackend
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.harbor_fakes import FakeBackend, bind_os_backend, strip_path_fields

# A sentinel file that makes /work/ appear as an existing directory in FakeBackend.
# FakeBackend.is_dir(p) checks: any file in files that starts with p.rstrip("/") + "/".
_DIR_SENTINEL = "/work/.keep"


def _harbor(files=None):
    """Build WriteToFile with a FakeBackend pre-seeded with files + dir sentinel."""
    seed = {_DIR_SENTINEL: ""}
    if files:
        seed.update(files)
    t = WriteToFile.__new__(WriteToFile)
    return bind_os_backend(t, FakeBackend(seed))


def _base():
    t = WriteToFile.__new__(WriteToFile)
    return bind_os_backend(t, LocalFsBackend())


def _run_both(tmp_path, initial_content, path_name, call_kwargs):
    """Run the same case through the base tool (real fs) and the harbor shim (FakeBackend).

    Returns (base_env, base_content, harbor_env, harbor_content).
    base_content / harbor_content are None when the file does not exist after the call.
    """
    real_file = tmp_path / path_name
    harbor_path = f"/work/{path_name}"

    files = {}
    if initial_content is not None:
        real_file.write_text(initial_content, encoding="utf-8")
        files[harbor_path] = initial_content

    base_env = json.loads(_base()(path=str(real_file), **call_kwargs))
    # Read with newline="" to suppress universal newline translation — FakeBackend
    # stores strings verbatim, so we need the raw bytes-as-text for a fair comparison.
    if real_file.exists():
        with open(str(real_file), encoding="utf-8", newline="") as _fh:
            base_content = _fh.read()
    else:
        base_content = None

    h = _harbor(files)
    harbor_env = json.loads(h(path=harbor_path, **call_kwargs))
    harbor_content = h._backend.files.get(harbor_path)

    return base_env, base_content, harbor_env, harbor_content


def _cmp(tmp_path, initial_content, call_kwargs, *, path_name="f.txt"):
    b_env, b_content, h_env, h_content = _run_both(tmp_path, initial_content, path_name, call_kwargs)
    assert strip_path_fields(h_env, "path", "backup_path") == strip_path_fields(b_env, "path", "backup_path")
    assert h_content == b_content
    return b_env, h_env


# ---------------------------------------------------------------------------
# Parametric differential suite
# ---------------------------------------------------------------------------

_CASES = [
    # (id, initial_content_or_None, kwargs)
    ("create_new", None, {"content": "hello\nworld\n"}),
    ("overwrite", "old\n", {"content": "new\n"}),
    ("create_only_new", None, {"content": "x\n", "mode": "create_only"}),
    ("create_only_existing_fails", "x\n", {"content": "y\n", "mode": "create_only"}),
    ("fail_if_missing_absent_fails", None, {"content": "x\n", "mode": "fail_if_missing"}),
    ("fail_if_missing_exists", "old\n", {"content": "new\n", "mode": "fail_if_missing"}),
    ("newline_lf", None, {"content": "a\r\nb\r\nc\n", "newline": "lf"}),
    ("newline_crlf", None, {"content": "a\nb\nc\n", "newline": "crlf"}),
    ("newline_preserve", None, {"content": "a\r\nb\n", "newline": "preserve"}),
    ("backup_existing", "original\n", {"content": "updated\n", "backup": True}),
    ("backup_no_prior_file", None, {"content": "fresh\n", "backup": True}),
]


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_identical_to_base(tmp_path, case):
    _id, initial, kw = case
    b_env, b_content, h_env, h_content = _run_both(tmp_path, initial, "f.txt", kw)
    assert strip_path_fields(h_env, "path", "backup_path") == strip_path_fields(b_env, "path", "backup_path"), (
        f"envelope diverged for case={_id}: harbor={h_env} base={b_env}"
    )
    assert h_content == b_content, f"content diverged for case={_id}"


# ---------------------------------------------------------------------------
# Targeted assertions
# ---------------------------------------------------------------------------


def test_create_new_writes_content(tmp_path):
    _, h_env = _cmp(tmp_path, None, {"content": "hello\n"})
    assert h_env["ok"] is True
    assert h_env["created"] is True
    assert h_env["bytes_written"] > 0


def test_overwrite_replaces_content(tmp_path):
    b_env, h_env = _cmp(tmp_path, "old\n", {"content": "new\n"})
    assert h_env["ok"] is True
    assert h_env["created"] is False


def test_create_only_existing_fails(tmp_path):
    b_env, h_env = _cmp(tmp_path, "x\n", {"content": "y\n", "mode": "create_only"})
    assert h_env["ok"] is False
    assert h_env["error"]["type"] == "already_exists"


def test_fail_if_missing_absent_fails(tmp_path):
    b_env, h_env = _cmp(tmp_path, None, {"content": "x\n", "mode": "fail_if_missing"})
    assert h_env["ok"] is False
    assert h_env["error"]["type"] == "not_found"


def test_newline_lf_normalizes(tmp_path):
    content_crlf = "a\r\nb\r\nc\n"
    b_env, b_content, h_env, h_content = _run_both(tmp_path, None, "f.txt", {"content": content_crlf, "newline": "lf"})
    assert "\r\n" not in (h_content or ""), "CRLF not normalized to LF"
    assert h_content == b_content


def test_newline_crlf_normalizes(tmp_path):
    content_lf = "a\nb\nc\n"
    b_env, b_content, h_env, h_content = _run_both(tmp_path, None, "f.txt", {"content": content_lf, "newline": "crlf"})
    assert "\r\n" in (h_content or ""), "LF not normalized to CRLF"
    assert h_content == b_content


def test_backup_creates_bak_file(tmp_path):
    files = {"/work/f.txt": "original\n"}
    h = _harbor(files)
    h(path="/work/f.txt", content="updated\n", backup=True)
    assert h._backend.files.get("/work/f.txt.bak") == "original\n"
    assert h._backend.files.get("/work/f.txt") == "updated\n"


def test_relative_path_rejected(tmp_path):
    """Relative paths must be rejected by both base and harbor without hitting the fs."""
    h = _harbor()
    h_env = json.loads(h(path="relative/path.txt", content="x\n"))
    b = _base()
    b_env = json.loads(b(path="relative/path.txt", content="x\n"))
    assert h_env["ok"] is False
    assert h_env["error"]["type"] == "invalid_argument"
    assert strip_path_fields(h_env, "path", "backup_path") == strip_path_fields(b_env, "path", "backup_path")
