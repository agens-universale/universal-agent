import json

from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.utils.file_text import sha256_hex_of_bytes
from gc_testkit.helpers.tool_ctx import with_local_ctx


def test_replace_first_occurrence_only(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a b a", encoding="utf-8")
    res = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="a", replace="X"))
    assert res["ok"] is True and res["replaced"] == 1
    assert res["applied"] is True
    assert res["match_count"] == 2
    assert res["line_numbers"] == [1]
    assert res["match_ranges"] == [{"start_line": 1, "end_line": 1}]
    assert p.read_text(encoding="utf-8") == "X b a"


def test_replace_multiline_and_not_found(tmp_path):
    p = tmp_path / "m.txt"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")
    ok = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="line1\nline2", replace="top"))
    assert ok["ok"] is True
    assert ok["match_count"] == 1
    assert p.read_text(encoding="utf-8") == "top\nline3\n"
    miss = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="zzzzzzzzzz", replace="x"))
    assert miss["ok"] is False and miss["matched"] is False and miss["error"] is None
    assert miss["match_count"] == 0
    assert miss["suggestions"] == []


def test_replace_validation(tmp_path):
    tool = with_local_ctx(SearchAndReplace)
    target = tmp_path / "x.txt"
    target.write_text("abc", encoding="utf-8")
    empty = json.loads(tool(path=str(target), search="", replace="x"))
    assert empty["ok"] is False and empty["error"]["field"] == "search"
    nf = json.loads(tool(path=str(tmp_path / "none.txt"), search="a", replace="b"))
    assert nf["ok"] is False and nf["error"]["type"] == "not_found"


def test_occurrence_last_and_index(tmp_path):
    p = tmp_path / "o.txt"
    p.write_text("x y x y x", encoding="utf-8")
    last = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="x", replace="Z", occurrence="last"))
    assert last["ok"] is True and last["line_numbers"] == [1]
    assert p.read_text(encoding="utf-8") == "x y x y Z"

    p.write_text("x y x y x", encoding="utf-8")
    second = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="x", replace="Q", occurrence=2))
    assert second["ok"] is True and second["match_count"] == 3
    assert p.read_text(encoding="utf-8") == "x y Q y x"


def test_occurrence_all_requires_expected_matches(tmp_path):
    p = tmp_path / "all_guard.txt"
    p.write_text("a b a b a", encoding="utf-8")
    guarded = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="a", replace="z", occurrence="all")
    )
    assert guarded["ok"] is False
    assert guarded["error"]["type"] == "invalid_argument"


def test_occurrence_all_replaces_everything(tmp_path):
    p = tmp_path / "all_ok.txt"
    p.write_text("TODO one\nTODO two\nTODO three\n", encoding="utf-8")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="TODO",
            replace="DONE",
            occurrence="all",
            expected_matches=3,
        )
    )
    assert res["ok"] is True and res["applied"] is True
    assert res["replaced"] == 3 and res["match_count"] == 3
    assert res["line_numbers"] == [1, 2, 3]
    assert res["match_ranges"] == [
        {"start_line": 1, "end_line": 1},
        {"start_line": 2, "end_line": 2},
        {"start_line": 3, "end_line": 3},
    ]
    assert p.read_text(encoding="utf-8") == "DONE one\nDONE two\nDONE three\n"


def test_occurrence_all_dry_run(tmp_path):
    p = tmp_path / "all_dry.txt"
    p.write_text("x\nx\nx\n", encoding="utf-8")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="x",
            replace="y",
            occurrence="all",
            expected_matches=3,
            dry_run=True,
        )
    )
    assert res["ok"] is True and res["applied"] is False
    assert res["replaced"] == 0 and res["match_count"] == 3
    assert res["line_numbers"] == [1, 2, 3]
    assert res["match_ranges"] == [
        {"start_line": 1, "end_line": 1},
        {"start_line": 2, "end_line": 2},
        {"start_line": 3, "end_line": 3},
    ]
    assert p.read_text(encoding="utf-8") == "x\nx\nx\n"


def test_expected_matches_guard(tmp_path):
    p = tmp_path / "gm.txt"
    p.write_text("a a a", encoding="utf-8")
    guarded = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="a",
            replace="b",
            expected_matches=1,
        )
    )
    assert guarded["ok"] is False
    assert guarded["error"]["type"] == "match_count_mismatch"
    assert guarded["line_numbers"] == [1]
    assert guarded["match_ranges"] == [
        {"start_line": 1, "end_line": 1},
        {"start_line": 1, "end_line": 1},
        {"start_line": 1, "end_line": 1},
    ]
    assert p.read_text(encoding="utf-8") == "a a a"


def test_dry_run_and_return_diff(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("hello world", encoding="utf-8")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="world",
            replace="team",
            dry_run=True,
            return_diff=True,
        )
    )
    assert res["ok"] is True and res["matched"] is True
    assert res["applied"] is False
    assert res["replaced"] == 0
    assert isinstance(res["diff"], str) and "@@ " in res["diff"]
    assert "-hello world\n" in res["diff"] and "+hello team\n" in res["diff"]
    assert res["diff_truncated"] is False
    assert p.read_text(encoding="utf-8") == "hello world"


def test_diff_truncation(tmp_path):
    p = tmp_path / "diff_trunc.txt"
    p.write_text(("alpha beta gamma\n" * 50), encoding="utf-8")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="beta",
            replace="BETA",
            occurrence="all",
            expected_matches=50,
            dry_run=True,
            return_diff=True,
            max_diff_chars=80,
        )
    )
    assert res["ok"] is True and res["applied"] is False
    assert isinstance(res["diff"], str)
    assert len(res["diff"]) == 80
    assert res["diff_truncated"] is True


def test_backup_created(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("first", encoding="utf-8")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="first",
            replace="second",
            backup=True,
        )
    )
    assert res["ok"] is True
    bak = tmp_path / "b.txt.bak"
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == "first"


def test_not_found_suggestions_by_difflib_similarity(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text("KEY: value\nretry    count\nalpha beta gamma\nunrelated\n", encoding="utf-8")

    by_case = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="key: value", replace="x"))
    assert by_case["ok"] is False and by_case["matched"] is False
    assert len(by_case["suggestions"]) >= 1
    assert by_case["suggestions"][0]["line"] == 1
    assert by_case["suggestions"][0]["reason"] == "single-line difflib similarity"
    assert isinstance(by_case["suggestions"][0]["score"], float)

    by_spaces = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="retry count", replace="x"))
    assert by_spaces["ok"] is False and by_spaces["matched"] is False
    assert len(by_spaces["suggestions"]) >= 1
    assert by_spaces["suggestions"][0]["line"] == 2
    assert by_spaces["suggestions"][0]["reason"] == "single-line difflib similarity"

    by_casefold = json.loads(with_local_ctx(SearchAndReplace)(path=str(p), search="ALPHA BETA", replace="x"))
    assert by_casefold["ok"] is False and by_casefold["matched"] is False
    assert len(by_casefold["suggestions"]) >= 1
    assert by_casefold["suggestions"][0]["line"] == 3


def test_not_found_suggestions_for_multiline_search(tmp_path):
    p = tmp_path / "ml.txt"
    p.write_text("foo bar\nbaz qux\nzap zot\n", encoding="utf-8")
    miss = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="foo baar\nbaz qxx", replace="x")
    )
    assert miss["ok"] is False and miss["matched"] is False
    assert len(miss["suggestions"]) >= 1
    assert miss["suggestions"][0]["line"] == 1
    assert miss["suggestions"][0]["end_line"] == 2
    assert miss["suggestions"][0]["reason"] == "multiline difflib similarity"
    assert isinstance(miss["suggestions"][0]["score"], float)
    assert miss["suggestions"][0]["score"] > 0.2


def test_not_found_suggestions_for_single_token_search(tmp_path):
    p = tmp_path / "st.txt"
    p.write_text("alpha beta gamma\n", encoding="utf-8")
    miss = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="ALPHA", replace="x")
    )
    assert miss["ok"] is False and miss["matched"] is False
    assert len(miss["suggestions"]) >= 1
    assert miss["suggestions"][0]["line"] == 1
    assert miss["suggestions"][0]["reason"] == "single-line difflib similarity"


def test_not_found_suggestions_limit_to_three(tmp_path):
    p = tmp_path / "lim.txt"
    lines = [f"alpha beta line {i}" for i in range(20)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    miss = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="Alpha   beta", replace="x")
    )
    assert miss["ok"] is False and miss["matched"] is False
    assert len(miss["suggestions"]) == 3
    assert miss["suggestions_total"] == 20
    scores = [s["score"] for s in miss["suggestions"]]
    assert scores == sorted(scores, reverse=True)


def test_not_found_suggestions_typo_in_token(tmp_path):
    p = tmp_path / "typo.txt"
    p.write_text("alpha beta gamma\nother stuff\n", encoding="utf-8")
    miss = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="alpha betta gamma", replace="x")
    )
    assert miss["ok"] is False and miss["matched"] is False
    assert len(miss["suggestions"]) >= 1
    assert miss["suggestions"][0]["line"] == 1
    assert miss["suggestions"][0]["score"] > 0.2


def test_not_found_suggestions_picks_nearest_function_line(tmp_path):
    p = tmp_path / "code.py"
    p.write_text(
        "import os\n"
        "def compute_total(items):\n"
        "    return sum(items)\n"
        "print('done')\n",
        encoding="utf-8",
    )
    miss = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="def compute_totl(items):",
            replace="def compute_total(items):",
        )
    )
    assert miss["ok"] is False and miss["matched"] is False
    assert miss["suggestions_total"] >= 1
    assert miss["suggestions"][0]["line"] == 2
    assert "compute_total" in miss["suggestions"][0]["text"]
    assert miss["suggestions"][0]["score"] > 0.2


def test_not_found_suggestions_mid_score_multiline_kept(tmp_path):
    p = tmp_path / "block.txt"
    p.write_text("hello world\nexample text\nnoise noise\n", encoding="utf-8")
    miss = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="hello wrld\nexampl text",
            replace="x",
        )
    )
    assert miss["ok"] is False and miss["matched"] is False
    assert len(miss["suggestions"]) >= 1
    assert miss["suggestions"][0]["line"] == 1
    assert miss["suggestions"][0]["end_line"] == 2
    assert 0.2 < miss["suggestions"][0]["score"] < 1.0


def test_not_found_unrelated_search_has_no_suggestions(tmp_path):
    p = tmp_path / "u.txt"
    p.write_text("short\n", encoding="utf-8")
    miss = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="zzzzzzzzzzzzzzzz", replace="x")
    )
    assert miss["ok"] is False and miss["matched"] is False
    assert miss["suggestions"] == []
    assert miss["suggestions_total"] == 0


def test_stale_file_sha256_guard(tmp_path):
    p = tmp_path / "stale.txt"
    p.write_text("version one", encoding="utf-8")
    stale_hash = sha256_hex_of_bytes(b"version zero")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="one",
            replace="two",
            expected_file_sha256=stale_hash,
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["error"]["type"] == "stale_file"
    assert p.read_text(encoding="utf-8") == "version one"


def test_expected_file_sha256_allows_match(tmp_path):
    p = tmp_path / "fresh.txt"
    text = "keep me"
    p.write_text(text, encoding="utf-8")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="keep",
            replace="changed",
            expected_file_sha256=sha256_hex_of_bytes(text.encode("utf-8")),
        )
    )
    assert res["ok"] is True and res["applied"] is True
    assert p.read_text(encoding="utf-8") == "changed me"


def test_crlf_file_info_hash_allows_edit_without_false_stale(tmp_path):
    from gc_core.tools.fs.file_info import FileInfo

    data = b"version one\r\n"
    p = tmp_path / "crlf.txt"
    p.write_bytes(data)
    info = json.loads(with_local_ctx(FileInfo)(path=str(p), include_hash=True))
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="one",
            replace="two",
            expected_file_sha256=info["sha256"],
        )
    )
    assert res["ok"] is True and res["applied"] is True
    assert p.read_bytes() == b"version two\r\n"
    assert res["sha256"] == sha256_hex_of_bytes(b"version two\r\n")


def test_search_and_replace_rejects_nul_binary(tmp_path):
    p = tmp_path / "blob.bin"
    original = b"aaa\x00bbb"
    p.write_bytes(original)
    res = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="aaa", replace="XXX")
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["error"]["type"] == "is_binary"
    assert p.read_bytes() == original


def test_search_and_replace_utf16_le_round_trip(tmp_path):
    p = tmp_path / "utf16.txt"
    p.write_bytes(b"\xff\xfeH\x00i\x00\n\x00")
    res = json.loads(
        with_local_ctx(SearchAndReplace)(path=str(p), search="Hi", replace="Yo")
    )
    assert res["ok"] is True and res["applied"] is True
    assert res["encoding_used"] == "utf-16-le"
    assert p.read_bytes() == b"\xff\xfeY\x00o\x00\n\x00"


def test_search_and_replace_cp1251_round_trip(tmp_path):
    text = "Привет, мир\n"
    p = tmp_path / "legacy.txt"
    p.write_bytes(text.encode("cp1251"))
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="мир",
            replace="код",
            encoding="cp1251",
        )
    )
    assert res["ok"] is True and res["applied"] is True
    assert res["encoding_used"] == "cp1251"
    assert p.read_bytes() == "Привет, код\n".encode("cp1251")


def test_search_and_replace_encode_failed_keeps_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"Hi\n")
    original = p.read_bytes()
    res = json.loads(
        with_local_ctx(SearchAndReplace)(
            path=str(p),
            search="Hi",
            replace="Привет",
            encoding="latin-1",
        )
    )
    assert res["ok"] is False and res["applied"] is False
    assert res["error"]["type"] == "encode_failed"
    assert p.read_bytes() == original


def test_reject_large_file(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("small", encoding="utf-8")
    tool = with_local_ctx(
        SearchAndReplace,
        config_dict={"fs_tools": {"max_mutate_bytes": 1}},
    )
    res = json.loads(tool(path=str(p), search="x y", replace="z"))
    assert res["ok"] is False and res["matched"] is False
    assert res["error"]["type"] == "file_too_large"
    assert res["error"]["actual_bytes"] > res["error"]["max_bytes"]
    assert res["error"]["max_bytes"] == 1
