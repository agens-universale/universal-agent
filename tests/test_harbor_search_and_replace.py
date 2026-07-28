import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.harbor_support.fakes import FakeBackend, bind_os_backend, strip_path_fields
from backends.local import LocalFsBackend
from tools.fs.search_and_replace import SearchAndReplace

CONTENT = "alpha\nbeta\nalpha\ngamma\n"
SHA = hashlib.sha256(CONTENT.encode()).hexdigest()


def _harbor(files):
    t = SearchAndReplace.__new__(SearchAndReplace)
    return bind_os_backend(t, FakeBackend(files))


def _base():
    t = SearchAndReplace.__new__(SearchAndReplace)
    return bind_os_backend(t, LocalFsBackend())


def _run_both(tmp_path, content, **kwargs):
    f = tmp_path / "f.txt"
    files = {}
    if content is not None:
        f.write_text(content, encoding="utf-8")
        files["/work/f.txt"] = content
    base_env = json.loads(_base()(path=str(f), **kwargs))
    base_content = f.read_text(encoding="utf-8") if f.exists() else None
    h = _harbor(files)
    harbor_env = json.loads(h(path="/work/f.txt", **kwargs))
    return base_env, base_content, harbor_env, h._backend.files.get("/work/f.txt")


_CASES = [
    ("replace_first", CONTENT, {"search": "alpha", "replace": "Z"}),
    ("replace_all", CONTENT, {"search": "alpha", "replace": "Z", "occurrence": "all", "expected_matches": 2}),
    ("occurrence_last", CONTENT, {"search": "alpha", "replace": "Z", "occurrence": "last"}),
    ("no_match", CONTENT, {"search": "NOPE", "replace": "X"}),
    ("dry_run", CONTENT, {"search": "beta", "replace": "X", "dry_run": True}),
    ("expected_mismatch", CONTENT, {"search": "alpha", "replace": "Z", "occurrence": "all", "expected_matches": 1}),
    ("sha_ok", CONTENT, {"search": "beta", "replace": "X", "expected_file_sha256": SHA}),
    ("sha_stale", CONTENT, {"search": "beta", "replace": "X", "expected_file_sha256": "0" * 64}),
    ("not_found", None, {"search": "a", "replace": "b"}),
    ("empty_search", CONTENT, {"search": "", "replace": "X"}),
    ("backup", CONTENT, {"search": "beta", "replace": "X", "backup": True}),
    ("occurrence_int_2", CONTENT, {"search": "alpha", "replace": "Z", "occurrence": 2}),
    ("occurrence_out_of_range", CONTENT, {"search": "alpha", "replace": "Z", "occurrence": 9}),
]


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_identical_to_base(tmp_path, case):
    _id, content, kw = case
    b_env, b_content, h_env, h_content = _run_both(tmp_path, content, **kw)
    assert strip_path_fields(h_env, "path", "backup_path", "diff") == strip_path_fields(b_env, "path", "backup_path", "diff"), _id
    assert h_content == b_content, _id
