import json
from types import SimpleNamespace

import pytest

from tests.harbor_support.fakes import FakeBackend, bind_os_backend, strip_path_fields
from backends.local import LocalFsBackend
from tools.fs.read_file import ReadFile

CONTENT = "line1\nline2\nline3\nline4\n"


def _harbor(files):
    t = ReadFile.__new__(ReadFile)
    t._config_dict = None
    return bind_os_backend(t, FakeBackend(files))


def _base():
    t = ReadFile.__new__(ReadFile)
    t._config_dict = None  # __new__ skips __init__; base uses this for _max_file_bytes/_resolve_char_limit
    return bind_os_backend(t, LocalFsBackend())


def _run_both(tmp_path, content, **kwargs):
    f = tmp_path / "f.txt"
    files = {}
    if content is not None:
        f.write_text(content, encoding="utf-8")
        files["/work/f.txt"] = content
    base_env = json.loads(_base()(path=str(f), **kwargs))
    h = _harbor(files)
    harbor_env = json.loads(h(path="/work/f.txt", **kwargs))
    return base_env, harbor_env


_CASES = [
    ("whole_file", CONTENT, {}),
    ("line_offset_limit", CONTENT, {"offset": 2, "limit": 2}),
    ("char_window", CONTENT, {"char_offset": 0, "char_limit": 6}),
    ("not_found", None, {}),
    ("bad_limit", CONTENT, {"char_limit": -1}),
]


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_identical_to_base(tmp_path, case):
    _id, content, kw = case
    b_env, h_env = _run_both(tmp_path, content, **kw)
    # sha256/total_lines/etc must match; path and error.message differ by location
    # (not_found embeds path in error.message), so strip both path and error.
    assert strip_path_fields(h_env, "path", "error") == strip_path_fields(b_env, "path", "error"), _id
    # Structural error shape must match (type/field keys), even if message text differs.
    h_err = h_env.get("error")
    b_err = b_env.get("error")
    assert (h_err is None) == (b_err is None), f"{_id}: error presence mismatch"
    if h_err is not None and b_err is not None:
        assert h_err.get("type") == b_err.get("type"), f"{_id}: error.type mismatch"
        assert h_err.get("field") == b_err.get("field"), f"{_id}: error.field mismatch"
