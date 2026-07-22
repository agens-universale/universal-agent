"""HarborApplyFileEdits must behave 1:1 with the base ApplyFileEdits.

VERIFICATION METHOD: differential (oracle) testing. The base tool is the oracle — the
same base tool driven by an in-memory FakeBackend must produce an IDENTICAL envelope
and IDENTICAL resulting file content for the same input. `_run_both` feeds one case
through the base (real temp file) and the FakeBackend-driven instance and the
parametrized matrix below exercises every `__call__` branch (validation errors, file
checks, sha stale, every edit outcome, dry_run, backup, diff, occurrence/scope
variants). Any divergence on any case fails the suite.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.harbor_support.fakes import FakeBackend, bind_os_backend, strip_path_fields
from tools.backends.local import LocalFsBackend
from tools.fs.apply_file_edits import ApplyFileEdits

CONTENT = "alpha\nbeta\nalpha\ngamma\n"
CONTENT_SHA = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()


def _edit(search, replace, **over):
    e = {
        "search": search, "replace": replace,
        "expected_matches": None, "occurrence": None,
    }
    e.update(over)
    return e


def _harbor(files):
    t = ApplyFileEdits.__new__(ApplyFileEdits)
    return bind_os_backend(t, FakeBackend(files))


def _base():
    t = ApplyFileEdits.__new__(ApplyFileEdits)
    return bind_os_backend(t, LocalFsBackend(), config_dict={})  # write_guard disabled


def _strip_path_fields(env):
    # path/backup_path are location-specific; diff embeds the path in its header.
    # diff_truncated stays in the comparison so diff truncation parity is still checked.
    return strip_path_fields(env, "path", "backup_path", "diff")


def _run_both(tmp_path, content, edits, **kwargs):
    """Run the same case through base (real fs) and harbor (fake backend).

    Returns (base_env, base_content, harbor_env, harbor_content). content=None means
    the file is absent (for not_found). Paths differ by location and are stripped.
    """
    f = tmp_path / "f.txt"
    files = {}
    if content is not None:
        f.write_text(content, encoding="utf-8")
        files["/work/f.txt"] = content

    base_env = json.loads(_base()(path=str(f), edits=edits, **kwargs))
    base_content = f.read_text(encoding="utf-8") if f.exists() else None

    h = _harbor(files)
    harbor_env = json.loads(h(path="/work/f.txt", edits=edits, **kwargs))
    harbor_content = h._backend.files.get("/work/f.txt")
    return base_env, base_content, harbor_env, harbor_content


# (id, content, edits, kwargs) — one row per __call__ branch.
_CASES = [
    ("single_apply", CONTENT, [_edit("beta", "BETA")], {}),
    ("multi_apply", CONTENT, [_edit("beta", "B"), _edit("gamma", "G")], {}),
    ("occurrence_first", CONTENT, [_edit("alpha", "Z", occurrence="first")], {}),
    ("occurrence_last", CONTENT, [_edit("alpha", "Z", occurrence="last")], {}),
    ("occurrence_all", CONTENT, [_edit("alpha", "Z", occurrence="all")], {}),
    ("occurrence_int_2", CONTENT, [_edit("alpha", "Z", occurrence=2)], {}),
    ("occurrence_out_of_range", CONTENT, [_edit("alpha", "Z", occurrence=9)], {}),
    ("expected_matches_ok", CONTENT, [_edit("alpha", "Z", occurrence="all", expected_matches=2)], {}),
    ("expected_matches_mismatch", CONTENT, [_edit("alpha", "Z", expected_matches=1)], {}),
    ("no_match", CONTENT, [_edit("NOPE", "X")], {}),
    ("atomic_second_fails", CONTENT, [_edit("alpha", "A"), _edit("NOPE", "X")], {}),
    ("dry_run", CONTENT, [_edit("beta", "X")], {"dry_run": True}),
    ("backup", CONTENT, [_edit("beta", "X")], {"backup": True}),
    ("return_diff", CONTENT, [_edit("beta", "X")], {"return_diff": True}),
    ("no_change", CONTENT, [_edit("beta", "beta")], {}),
    ("sha_ok", CONTENT, [_edit("beta", "X")], {"expected_file_sha256": CONTENT_SHA}),
    ("sha_stale", CONTENT, [_edit("beta", "X")], {"expected_file_sha256": "0" * 64}),
    ("not_found", None, [_edit("a", "b")], {}),
    # validation branches (file untouched)
    ("edits_empty", CONTENT, [], {}),
    ("atomic_false", CONTENT, [_edit("beta", "X")], {"atomic": False}),
    ("bad_max_diff_chars", CONTENT, [_edit("beta", "X")], {"max_diff_chars": 0}),
    ("bad_sha_format", CONTENT, [_edit("beta", "X")], {"expected_file_sha256": "xyz"}),
    ("edit_empty_search", CONTENT, [_edit("", "X")], {}),
    ("edit_missing_keys", CONTENT, [{"search": "beta"}], {}),
]


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_harbor_call_is_identical_to_base(tmp_path, case):
    _id, content, edits, kwargs = case
    base_env, base_content, harbor_env, harbor_content = _run_both(tmp_path, content, edits, **kwargs)
    assert _strip_path_fields(harbor_env) == _strip_path_fields(base_env), f"envelope diverged: {_id}"
    assert harbor_content == base_content, f"content diverged: {_id}"


def test_harbor_matches_base_envelope_and_content(tmp_path):
    edits = [_edit("beta", "BETA"), _edit("gamma", "GAMMA")]

    f = tmp_path / "x.txt"
    f.write_text(CONTENT, encoding="utf-8")
    base_env = json.loads(_base()(path=str(f), edits=edits))
    base_content = f.read_text(encoding="utf-8")

    h = _harbor({"/work/x.txt": CONTENT})
    harbor_env = json.loads(h(path="/work/x.txt", edits=edits))
    harbor_content = h._backend.files["/work/x.txt"]

    assert harbor_content == base_content == "alpha\nBETA\nalpha\nGAMMA\n"
    # Identical envelope apart from the (location-specific) path fields.
    assert _strip_path_fields(harbor_env) == _strip_path_fields(base_env)
    assert harbor_env["applied"] is True
    assert harbor_env["edits_applied"] == 2
    assert harbor_env["total_replaced"] == 2


def test_dry_run_does_not_write():
    h = _harbor({"/f": CONTENT})
    env = json.loads(h(path="/f", edits=[_edit("beta", "X")], dry_run=True))
    assert env["ok"] is True and env["applied"] is False
    assert h._backend.files["/f"] == CONTENT  # unchanged


def test_no_match_is_atomic_failure():
    # edit #0 would apply, edit #1 has no match → whole batch fails, nothing written.
    edits = [_edit("alpha", "A"), _edit("NOPE", "X")]
    h = _harbor({"/f": CONTENT})
    env = json.loads(h(path="/f", edits=edits))
    assert env["ok"] is False and env["applied"] is False
    assert env["failed_edit_index"] == 1
    assert env["error"]["type"] == "not_found"
    assert h._backend.files["/f"] == CONTENT  # atomic: untouched


def test_occurrence_last_replaces_correct_match_and_counts():
    h = _harbor({"/f": CONTENT})
    env = json.loads(h(path="/f", edits=[_edit("alpha", "Z", occurrence="last")]))
    assert env["applied"] is True
    assert env["total_replaced"] == 1  # not the file's total match count (=2)
    assert h._backend.files["/f"] == "alpha\nbeta\nZ\ngamma\n"


def test_expected_matches_mismatch_blocks():
    h = _harbor({"/f": CONTENT})
    env = json.loads(h(path="/f", edits=[_edit("alpha", "Z", expected_matches=1)]))
    assert env["ok"] is False and env["applied"] is False
    assert env["error"]["type"] == "match_count_mismatch"
    assert h._backend.files["/f"] == CONTENT


def test_not_found_envelope():
    h = _harbor({})
    env = json.loads(h(path="/missing", edits=[_edit("a", "b")]))
    assert env["ok"] is False and env["error"]["type"] == "not_found"
