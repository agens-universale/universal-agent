"""Edit tools match anchors across CRLF/LF and write replacements in the file's style."""
import json

import pytest

from tools.fs.apply_file_edits import ApplyFileEdits
from tools.fs.edit_line_endings import resolve_edit_line_endings
from tools.fs.search_and_replace import SearchAndReplace

from tests.helpers.tool_ctx import with_local_ctx

CRLF_TEXT = "def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n"
CRLF = CRLF_TEXT.encode()
LF = CRLF.replace(b"\r\n", b"\n")
LF_ANCHOR = "def a():\n    return 1\n"
LF_REPLACE = "def a():\n    return 10\n"
CRLF_ANCHOR = LF_ANCHOR.replace("\n", "\r\n")
CRLF_REPLACE = LF_REPLACE.replace("\n", "\r\n")


# --- policy: resolve_edit_line_endings ----------------------------------------

def test_literal_match_is_kept_as_is():
    res = resolve_edit_line_endings(CRLF_TEXT, CRLF_ANCHOR, CRLF_REPLACE)
    assert res.search == CRLF_ANCHOR
    assert res.replace == CRLF_REPLACE
    assert res.normalized is False
    assert res.file_line_ending == "\r\n"


def test_lf_anchor_is_converted_to_file_crlf_when_literal_misses():
    res = resolve_edit_line_endings(CRLF_TEXT, LF_ANCHOR, LF_REPLACE)
    assert res.search == CRLF_ANCHOR
    assert res.replace == CRLF_REPLACE
    assert res.normalized is True


def test_replace_follows_the_matched_anchor_style():
    res = resolve_edit_line_endings(CRLF_TEXT, CRLF_ANCHOR, LF_REPLACE)
    assert res.search == CRLF_ANCHOR
    assert res.replace == CRLF_REPLACE
    assert res.normalized is True


def test_single_line_anchor_replace_follows_file_style():
    res = resolve_edit_line_endings(CRLF_TEXT, "    return 1", "    return 1\n    pass")
    assert res.search == "    return 1"
    assert res.replace == "    return 1\r\n    pass"


def test_file_without_newlines_is_untouched():
    res = resolve_edit_line_endings("hello world", "world", "a\nb")
    assert res.replace == "a\nb"
    assert res.file_line_ending is None
    assert res.normalized is False


def test_miss_in_both_styles_keeps_original_search():
    res = resolve_edit_line_endings(CRLF_TEXT, "nope\n", "x")
    assert res.search == "nope\n"
    assert res.normalized is False


def test_mixed_file_literal_lf_island_keeps_lf_replace():
    mixed = "a\r\nb\r\nx\ny\nc\r\n"
    res = resolve_edit_line_endings(mixed, "x\ny\n", "X\nY\n")
    assert res.search == "x\ny\n"
    assert res.replace == "X\nY\n"
    assert res.normalized is False


# --- both tools ---------------------------------------------------------------

def _apply_edits(path, search, replace, **kwargs):
    return json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(path), edits=[{"search": search, "replace": replace}], **kwargs
        )
    )


def _search_replace(path, search, replace, **kwargs):
    return json.loads(
        with_local_ctx(SearchAndReplace)(path=str(path), search=search, replace=replace, **kwargs)
    )


TOOLS = [
    pytest.param(_apply_edits, id="apply_file_edits"),
    pytest.param(_search_replace, id="search_and_replace"),
]


@pytest.mark.parametrize("run", TOOLS)
def test_lf_anchor_edits_crlf_file_and_keeps_crlf(tmp_path, run):
    p = tmp_path / "f.py"
    p.write_bytes(CRLF)
    res = run(p, LF_ANCHOR, LF_REPLACE)
    assert res["ok"] is True and res["applied"] is True
    assert p.read_bytes() == CRLF.replace(b"return 1", b"return 10")


@pytest.mark.parametrize("run", TOOLS)
def test_crlf_anchor_edits_lf_file_and_keeps_lf(tmp_path, run):
    p = tmp_path / "f.py"
    p.write_bytes(LF)
    res = run(p, CRLF_ANCHOR, CRLF_REPLACE)
    assert res["ok"] is True and res["applied"] is True
    assert p.read_bytes() == LF.replace(b"return 1", b"return 10")


@pytest.mark.parametrize("run", TOOLS)
def test_lf_replace_into_crlf_file_does_not_mix_line_endings(tmp_path, run):
    p = tmp_path / "f.py"
    p.write_bytes(CRLF)
    res = run(p, CRLF_ANCHOR, LF_REPLACE)
    assert res["ok"] is True
    assert p.read_bytes() == CRLF.replace(b"return 1", b"return 10")


@pytest.mark.parametrize("run", TOOLS)
def test_single_line_anchor_still_matches(tmp_path, run):
    p = tmp_path / "f.py"
    p.write_bytes(CRLF)
    res = run(p, "    return 1", "    return 10")
    assert res["ok"] is True
    assert p.read_bytes() == CRLF.replace(b"return 1", b"return 10")


def test_apply_file_edits_reports_normalization_per_edit(tmp_path):
    p = tmp_path / "f.py"
    p.write_bytes(CRLF)
    res = _apply_edits(p, LF_ANCHOR, LF_REPLACE)
    assert res["edit_results"][0]["line_endings_normalized"] is True


def test_apply_file_edits_omits_flag_when_nothing_normalized(tmp_path):
    p = tmp_path / "f.py"
    p.write_bytes(CRLF)
    res = _apply_edits(p, CRLF_ANCHOR, CRLF_REPLACE)
    assert "line_endings_normalized" not in res["edit_results"][0]


def test_search_and_replace_reports_normalization(tmp_path):
    p = tmp_path / "f.py"
    p.write_bytes(CRLF)
    assert _search_replace(p, LF_ANCHOR, LF_REPLACE)["line_endings_normalized"] is True
    p.write_bytes(CRLF)
    assert _search_replace(p, CRLF_ANCHOR, CRLF_REPLACE)["line_endings_normalized"] is False


def test_apply_file_edits_occurrence_all_counts_normalized_anchor(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"x\r\ny\r\nx\r\ny\r\n")
    res = json.loads(
        with_local_ctx(ApplyFileEdits)(
            path=str(p),
            edits=[{"search": "x\ny\n", "replace": "z\n", "occurrence": "all", "expected_matches": 2}],
        )
    )
    assert res["ok"] is True and res["total_replaced"] == 2
    assert p.read_bytes() == b"z\r\nz\r\n"


def test_search_and_replace_dry_run_diff_uses_file_style(tmp_path):
    p = tmp_path / "f.py"
    p.write_bytes(CRLF)
    res = _search_replace(p, "    return 1\n", "    return 10\n", dry_run=True, return_diff=True)
    assert res["ok"] is True and res["applied"] is False
    assert "-    return 1\r\n" in res["diff"] and "+    return 10\r\n" in res["diff"]
    assert p.read_bytes() == CRLF
