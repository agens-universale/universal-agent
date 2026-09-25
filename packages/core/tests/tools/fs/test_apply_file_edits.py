import json

from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.utils.file_text import sha256_hex_of_bytes
from gc_testkit.helpers.tool_ctx import with_local_ctx


def test_apply_batch_success(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("A B C\nA B C\n", encoding="utf-8")
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[
                {
                    "search": "A",
                    "replace": "X",
                    "occurrence": "first",
                    "expected_matches": 2,
                },
                {
                    "search": "C",
                    "replace": "Z",
                    "occurrence": "all",
                    "expected_matches": 2,
                },
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
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[
                {
                    "search": "one",
                    "replace": "ONE",
                    "occurrence": "all",
                    "expected_matches": 2,
                },
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
    p.write_text("foo bar\n", encoding="utf-8", newline="")
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
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
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "live", "replace": "dead"}],
            expected_file_sha256=sha256_hex_of_bytes(b"old content"),
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["edits_applied"] == 0
    assert res["error"]["type"] == "stale_file"
    assert p.read_text(encoding="utf-8") == "live content"


def test_apply_crlf_file_info_hash_allows_edit_without_false_stale(tmp_path):
    from gc_core.tools.fs.file_info import FileInfo

    data = b"version one\r\n"
    p = tmp_path / "crlf.txt"
    p.write_bytes(data)
    info = json.loads(with_local_ctx(FileInfo)(path=str(p), include_hash=True))
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "one", "replace": "two"}],
            expected_file_sha256=info["sha256"],
        )
    )
    assert res["ok"] is True and res["applied"] is True
    assert p.read_bytes() == b"version two\r\n"
    assert res["sha256"] == sha256_hex_of_bytes(b"version two\r\n")


def test_apply_batch_rejects_nul_binary(tmp_path):
    p = tmp_path / "blob.bin"
    original = b"live\x00content"
    p.write_bytes(original)
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "live", "replace": "dead"}],
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["error"]["type"] == "is_binary"
    assert p.read_bytes() == original


def test_apply_batch_encoding_required_does_not_write(tmp_path):
    text = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    original = text.encode("cp1251")
    p = tmp_path / "legacy.txt"
    p.write_bytes(original)
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "коллеги", "replace": "друзья"}],
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["error"]["type"] == "encoding_required"
    assert res["error"]["encoding_guess"] == "cp1251"
    assert p.read_bytes() == original


def test_apply_batch_backup_once(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x y z\n", encoding="utf-8")
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
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


def test_apply_not_found_includes_suggestions(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("alpha beta gamma\nother line\n", encoding="utf-8")
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "alpha betta gamma", "replace": "x"}],
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["error"] == {"type": "not_found"}
    assert "suggestions" not in res["error"]
    assert len(res["edit_results"]) == 1
    er = res["edit_results"][0]
    assert er["matched"] is False
    assert er["match_count"] == 0
    assert len(er["suggestions"]) >= 1
    assert len(er["suggestions"]) <= 3
    assert er["suggestions"][0]["line"] == 1
    assert er["suggestions"][0]["score"] > 0.2
    assert er["suggestions"][0]["reason"] == "single-line difflib similarity"
    assert "Top matches: line 1" in res["message"]
    assert p.read_text(encoding="utf-8") == "alpha beta gamma\nother line\n"


def test_apply_not_found_multiline_suggestions(tmp_path):
    p = tmp_path / "ml.txt"
    p.write_text("foo bar\nbaz qux\nzap zot\n", encoding="utf-8")
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "foo baar\nbaz qxx", "replace": "x"}],
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["error"]["type"] == "not_found"
    er = res["edit_results"][0]
    assert len(er["suggestions"]) >= 1
    assert er["suggestions"][0]["line"] == 1
    assert er["suggestions"][0]["end_line"] == 2
    assert er["suggestions"][0]["reason"] == "multiline difflib similarity"
    assert er["suggestions"][0]["score"] > 0.2
    assert "Top matches:" in res["message"]


def test_apply_not_found_picks_nearest_among_distractors(tmp_path):
    p = tmp_path / "code.py"
    p.write_text(
        "import os\ndef compute_total(items):\n    return sum(items)\nprint('done')\n",
        encoding="utf-8",
    )
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "def compute_totl(items):", "replace": "x"}],
        )
    )
    assert res["ok"] is False
    er = res["edit_results"][0]
    assert er["suggestions"][0]["line"] == 2
    assert "compute_total" in er["suggestions"][0]["text"]


def test_apply_success_path_has_no_suggestions_field_needed(tmp_path):
    p = tmp_path / "ok.txt"
    p.write_text("alpha beta gamma\n", encoding="utf-8")
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[
                {"search": "alpha beta gamma", "replace": "ALPHA BETA GAMMA"}
            ],
        )
    )
    assert res["ok"] is True and res["applied"] is True
    assert "suggestions" not in res["edit_results"][0]
    assert p.read_text(encoding="utf-8") == "ALPHA BETA GAMMA\n"
