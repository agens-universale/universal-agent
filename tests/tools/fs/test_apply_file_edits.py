import json

from tools.fs.apply_file_edits import ApplyFileEdits
from utils.file_text import sha256_hex_of_text


def test_apply_batch_success(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("A B C\nA B C\n", encoding="utf-8")
    res = json.loads(
        ApplyFileEdits()(
            path=str(p),
            edits=[
                {"search": "A", "replace": "X", "occurrence": "first", "expected_matches": 2},
                {"search": "C", "replace": "Z", "occurrence": "all", "expected_matches": 2},
            ],
        )
    )
    assert res["ok"] is True and res["applied"] is True
    assert res["edits_would_apply"] == 2
    assert res["total_would_replace"] == 3
    assert res["edits_applied"] == 2
    assert res["total_replaced"] == 3
    assert p.read_text(encoding="utf-8") == "X B Z\nA B Z\n"


def test_apply_batch_atomic_fail_keeps_file(tmp_path):
    p = tmp_path / "f.txt"
    original = "one two\none two\n"
    p.write_text(original, encoding="utf-8")
    res = json.loads(
        ApplyFileEdits()(
            path=str(p),
            edits=[
                {"search": "one", "replace": "ONE", "occurrence": "all", "expected_matches": 2},
                {"search": "missing", "replace": "x"},
            ],
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["failed_edit_index"] == 1
    assert res["edits_would_apply"] == 1
    assert res["total_would_replace"] == 2
    assert res["edits_applied"] == 0
    assert res["total_replaced"] == 0
    assert p.read_text(encoding="utf-8") == original


def test_apply_batch_dry_run(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("foo bar\n", encoding="utf-8")
    res = json.loads(
        ApplyFileEdits()(
            path=str(p),
            edits=[{"search": "foo", "replace": "FOO"}],
            dry_run=True,
            return_diff=True,
        )
    )
    assert res["ok"] is True and res["applied"] is False
    assert res["edits_would_apply"] == 1
    assert res["total_would_replace"] == 1
    assert res["edits_applied"] == 0
    assert res["total_replaced"] == 0
    assert isinstance(res["diff"], str)
    assert "-foo bar\n" in res["diff"] and "+FOO bar\n" in res["diff"]
    assert p.read_text(encoding="utf-8") == "foo bar\n"


def test_apply_batch_stale_file_guard(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("live content", encoding="utf-8")
    res = json.loads(
        ApplyFileEdits()(
            path=str(p),
            edits=[{"search": "live", "replace": "dead"}],
            expected_file_sha256=sha256_hex_of_text("old content"),
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["edits_applied"] == 0
    assert res["error"]["type"] == "stale_file"
    assert p.read_text(encoding="utf-8") == "live content"


def test_apply_batch_backup_once(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x y z\n", encoding="utf-8")
    res = json.loads(
        ApplyFileEdits()(
            path=str(p),
            edits=[
                {"search": "x", "replace": "X"},
                {"search": "z", "replace": "Z"},
            ],
            backup=True,
        )
    )
    assert res["ok"] is True
    bak = tmp_path / "f.txt.bak"
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == "x y z\n"
