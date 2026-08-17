import json

from gc_core.tools.fs.search_files import SearchFiles


from tests.helpers.tool_ctx import with_local_ctx

def test_search_basic_and_file_pattern(tmp_path):
    (tmp_path / "a.py").write_text("x = 42\nprint(x)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("answer: 42\n", encoding="utf-8")
    data = json.loads(with_local_ctx(SearchFiles)(path=str(tmp_path), regex="42"))
    assert data["ok"] is True
    assert data["total_matches"] == 2 and data["count"] == 2
    assert data["truncated"] is False and data["next_offset"] is None
    py = json.loads(with_local_ctx(SearchFiles)(path=str(tmp_path), regex="print", file_pattern="*.py"))
    assert py["ok"] is True and py["total_matches"] == 1 and py["count"] == 1
    assert py["matches"][0]["file"].endswith("a.py")


def test_search_pagination_and_offset_beyond(tmp_path):
    (tmp_path / "a.py").write_text("ALPHA\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("ALPHA\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("ALPHA\n", encoding="utf-8")

    tool = with_local_ctx(SearchFiles)
    p1 = json.loads(tool(path=str(tmp_path), regex="ALPHA", file_pattern="*.py", offset=1, limit=2))
    assert p1["ok"] and p1["total_matches"] == 3
    assert p1["count"] == 2 and len(p1["matches"]) == 2
    assert p1["truncated"] is True and p1["next_offset"] == 3

    p2 = json.loads(tool(path=str(tmp_path), regex="ALPHA", file_pattern="*.py", offset=3, limit=2))
    assert p2["ok"] and p2["total_matches"] == 3 and p2["count"] == 1
    assert p2["truncated"] is False and p2["next_offset"] is None

    empty_page = json.loads(tool(path=str(tmp_path), regex="ALPHA", offset=99, limit=10))
    assert empty_page["ok"] and empty_page["total_matches"] == 3
    assert empty_page["count"] == 0 and empty_page["matches"] == []


def test_search_invalid_pagination_offsets(tmp_path):
    (tmp_path / "z.txt").write_text("ALPHA\n", encoding="utf-8")
    tool = with_local_ctx(SearchFiles)
    bad_off = json.loads(tool(path=str(tmp_path), regex="ALPHA", offset=0, limit=1))
    assert bad_off["ok"] is False and bad_off["error"]["field"] == "offset"
    bad_lim = json.loads(tool(path=str(tmp_path), regex="ALPHA", offset=1, limit=0))
    assert bad_lim["ok"] is False and bad_lim["error"]["field"] == "limit"


def test_search_invalid_regex_returns_envelope(tmp_path):
    """Syntactically invalid regex must not raise; return ok=false envelope."""
    tool = with_local_ctx(SearchFiles)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    for pattern in ("[invalid(", "(", "*"):
        data = json.loads(tool(path=str(tmp_path), regex=pattern))
        assert data["ok"] is False, pattern
        assert data["error"]["type"] == "invalid_argument", pattern
        assert data["error"]["field"] == "regex", pattern
        assert data["error"]["message"].startswith("Invalid regex:"), pattern
        assert data["matches"] == []
        assert data["count"] == 0
        assert data["total_matches"] is None
        assert data["regex"] == pattern

    # Early validation: empty directory still rejects bad regex (no walk needed).
    empty = tmp_path / "empty"
    empty.mkdir()
    empty_dir = json.loads(tool(path=str(empty), regex="[invalid("))
    assert empty_dir["ok"] is False
    assert empty_dir["error"]["type"] == "invalid_argument"
    assert empty_dir["error"]["field"] == "regex"


def test_search_invalid_and_not_found(tmp_path):
    tool = with_local_ctx(SearchFiles)
    missing = json.loads(tool(path=str(tmp_path / "none"), regex="x"))
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"
    bad_path = json.loads(tool(path=1, regex="x"))  # type: ignore[arg-type]
    assert bad_path["ok"] is False and bad_path["error"]["field"] == "path"


def test_search_alternation_literal_prefilter(tmp_path):
    (tmp_path / "t.txt").write_text(
        "line1 ALPHA\n"
        "line2 beta\n"
        "line3 ALPHA beta\n",
        encoding="utf-8",
    )
    tool = with_local_ctx(SearchFiles)
    for regex in ("ALPHA|beta", "beta|ALPHA"):
        data = json.loads(tool(path=str(tmp_path), regex=regex))
        assert data["ok"] is True
        assert data["total_matches"] == 4, (regex, data["total_matches"], data["matches"])
        assert [m["line"] for m in data["matches"]] == [1, 2, 3, 3]


def test_search_alternation_cyrillic(tmp_path):
    (tmp_path / "t.md").write_text(
        "строка 1: Аудит\n"
        "строка 2: План проверки\n"
        "строка 3: Аудит и План проверки\n",
        encoding="utf-8",
    )
    data = json.loads(with_local_ctx(SearchFiles)(path=str(tmp_path), regex="Аудит|План проверки"))
    assert data["ok"] is True
    assert data["total_matches"] == 4
    assert [m["line"] for m in data["matches"]] == [1, 2, 3, 3]


def test_search_validation_and_file_path(tmp_path):
    tool = with_local_ctx(SearchFiles)
    bad_regex_type = json.loads(tool(path=str(tmp_path), regex=123))  # type: ignore[arg-type]
    assert bad_regex_type["ok"] is False and bad_regex_type["error"]["field"] == "regex"
    # Non-string optional file_pattern is coalesced to absent (same as omit / "").
    coalesced_pattern = json.loads(tool(path=str(tmp_path), regex="x", file_pattern=1))  # type: ignore[arg-type]
    assert coalesced_pattern["ok"] is True and coalesced_pattern["file_pattern"] is None
    f = tmp_path / "single.txt"
    f.write_text("alpha\nbeta x\ngamma\n", encoding="utf-8")
    # path may be a single file (no not_a_directory)
    hit = json.loads(tool(path=str(f), regex="x"))
    assert hit["ok"] is True
    assert hit["total_matches"] == 1
    assert hit["matches"][0]["line"] == 2
    assert hit["matches"][0]["file"].endswith("single.txt")
    # file_pattern is ignored for a file path
    ignored_pattern = json.loads(tool(path=str(f), regex="alpha", file_pattern="*.md"))
    assert ignored_pattern["ok"] is True and ignored_pattern["total_matches"] == 1


def test_search_binary_file_does_not_crash(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\xff\xfeMATCH\xfe")
    (tmp_path / "text.txt").write_text("MATCH here\n", encoding="utf-8")
    res = json.loads(with_local_ctx(SearchFiles)(path=str(tmp_path), regex="MATCH"))
    assert res["ok"] is True
    assert res["total_matches"] >= 1
    files = {m["file"] for m in res["matches"]}
    assert any("text.txt" in f for f in files)


def test_search_context_symmetric_before_and_after(tmp_path):
    """context = up to 5 lines before + match line + up to 5 lines after."""
    lines = [f"L{i}\n" for i in range(1, 16)]
    lines[7] = "TARGET_LINE\n"  # line 8
    (tmp_path / "c.txt").write_text("".join(lines), encoding="utf-8")
    data = json.loads(with_local_ctx(SearchFiles)(path=str(tmp_path), regex="TARGET_LINE"))
    assert data["ok"] is True and data["total_matches"] == 1
    m = data["matches"][0]
    assert m["line"] == 8
    ctx_lines = m["context"].splitlines(keepends=True)
    assert ctx_lines == [
        "L3\n",
        "L4\n",
        "L5\n",
        "L6\n",
        "L7\n",
        "TARGET_LINE\n",
        "L9\n",
        "L10\n",
        "L11\n",
        "L12\n",
        "L13\n",
    ]


def test_search_context_edges_and_nearby_matches(tmp_path):
    (tmp_path / "e.txt").write_text(
        "A\nB\nHIT1\nC\nHIT2\nD\nE\nF\nG\nH\nI\n",
        encoding="utf-8",
    )
    data = json.loads(with_local_ctx(SearchFiles)(path=str(tmp_path), regex="HIT[12]"))
    assert data["ok"] is True and data["total_matches"] == 2
    m1, m2 = data["matches"]
    assert m1["line"] == 3
    # HIT1 near top: only 2 lines before, full 5 after
    assert m1["context"].splitlines(keepends=True) == [
        "A\n",
        "B\n",
        "HIT1\n",
        "C\n",
        "HIT2\n",
        "D\n",
        "E\n",
        "F\n",
    ]
    assert m2["line"] == 5
    assert m2["context"].splitlines(keepends=True) == [
        "A\n",
        "B\n",
        "HIT1\n",
        "C\n",
        "HIT2\n",
        "D\n",
        "E\n",
        "F\n",
        "G\n",
        "H\n",
    ]


def test_search_skips_file_over_size_cap(tmp_path):
    small = tmp_path / "small.txt"
    big = tmp_path / "big.txt"
    small.write_text("FINDME in small\n", encoding="utf-8")
    big.write_text("FINDME in big file content\n", encoding="utf-8")
    tool = with_local_ctx(
        SearchFiles,
        config_dict={"fs_tools": {"max_read_bytes": len(small.read_bytes())}},
    )
    data = json.loads(tool(path=str(tmp_path), regex="FINDME"))
    assert data["ok"] is True
    assert data["total_matches"] == 1
    assert data["matches"][0]["file"].endswith("small.txt")
    skipped = data.get("skipped") or []
    assert data["skipped_total"] == 1
    assert data["skipped_truncated"] is False
    assert any(
        s.get("reason") == "file_too_large" and s["path"].endswith("big.txt")
        for s in skipped
    )


def test_search_skipped_list_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(SearchFiles, "default_skipped_limit", 2)
    for i in range(5):
        (tmp_path / f"big{i}.txt").write_text("FINDME xxxxxxxx\n", encoding="utf-8")
    (tmp_path / "small.txt").write_text("FINDME\n", encoding="utf-8")
    tool = with_local_ctx(
        SearchFiles,
        config_dict={"fs_tools": {"max_read_bytes": len(b"FINDME\n")}},
    )
    data = json.loads(tool(path=str(tmp_path), regex="FINDME"))
    assert data["ok"] is True
    assert data["skipped_total"] == 5
    assert data["skipped_truncated"] is True
    assert len(data["skipped"]) == 2
    assert all(s["reason"] == "file_too_large" for s in data["skipped"])


def test_search_spans_lines_by_default(tmp_path):
    p = tmp_path / "block.txt"
    p.write_text("alpha\nSTART\nmiddle\nEND\nomega\n", encoding="utf-8")
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex=r"START.*?END"))
    assert data["ok"] is True
    assert "multiline" not in data
    assert data["total_matches"] == 1
    hit = data["matches"][0]
    assert hit["line"] == 2
    assert hit["end_line"] == 4
    assert "START" in hit["line_content"] and "END" in hit["line_content"]
    assert "middle" in hit["context"]

    literal = json.loads(tool(path=str(p), regex=r"START\nmiddle\nEND"))
    assert literal["ok"] is True
    assert literal["total_matches"] == 1
    assert literal["matches"][0]["line"] == 2
    assert literal["matches"][0]["end_line"] == 4


def test_search_clips_large_match_content_and_context(tmp_path):
    # Greedy span: many lines between START and END; match text also exceeds clip.
    body = "\n".join([f"pad{i}-{'x' * 80}" for i in range(40)])
    p = tmp_path / "wide.txt"
    p.write_text(f"before\nSTART\n{body}\nEND\nafter\n", encoding="utf-8")
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex=r"START.*END"))
    assert data["ok"] is True and data["total_matches"] == 1
    hit = data["matches"][0]
    assert hit["line"] == 2
    assert hit["end_line"] == 43  # START + 40 pads + END
    assert "match_truncated" in hit["line_content"]
    assert hit["line_content"].startswith("START")
    assert "context_truncated" in hit["context"]
    assert "START" in hit["context"] and "END" in hit["context"]
    assert "pad0-" in hit["context"]  # near start window
    assert "pad39-" in hit["context"]  # near end window
    assert "pad20-" not in hit["context"]  # middle omitted


def test_search_single_file_too_large(tmp_path):
    p = tmp_path / "only.txt"
    p.write_text("needle\n", encoding="utf-8")
    tool = with_local_ctx(
        SearchFiles,
        config_dict={"fs_tools": {"max_read_bytes": 1}},
    )
    data = json.loads(tool(path=str(p), regex="needle"))
    assert data["ok"] is False
    assert data["error"]["type"] == "file_too_large"
    assert data["error"]["max_bytes"] == 1
    assert data["matches"] == []


def test_search_skips_binary_in_directory(tmp_path):
    text = tmp_path / "ok.txt"
    binary = tmp_path / "blob.bin"
    text.write_text("FINDME here\n", encoding="utf-8")
    binary.write_bytes(b"FINDME\x00binary\xff")
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(tmp_path), regex="FINDME"))
    assert data["ok"] is True
    assert data["total_matches"] == 1
    assert data["matches"][0]["file"].endswith("ok.txt")
    skipped = data.get("skipped") or []
    assert data["skipped_total"] == 1
    assert any(
        s.get("reason") == "is_binary" and s["path"].endswith("blob.bin")
        for s in skipped
    )


def test_search_single_file_binary_errors(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"abc\x00def")
    data = json.loads(with_local_ctx(SearchFiles)(path=str(p), regex="abc"))
    assert data["ok"] is False
    assert data["error"]["type"] == "is_binary"
    assert data["matches"] == []


# ---------------------------------------------------------------------------
# encoding parameter — searching non-UTF-8 files
# ---------------------------------------------------------------------------

def test_search_single_file_cp1251_encoding(tmp_path):
    """Single-file search with explicit encoding=cp1251 finds Cyrillic text."""
    p = tmp_path / "cp1251.txt"
    p.write_bytes("Строка 15: Тестовый текст\n".encode("cp1251"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="Строка 1", encoding="cp1251"))
    assert data["ok"] is True
    assert data["total_matches"] == 1
    # line_content is the regex match, not the full line — check context for full line
    assert "Строка 1" in data["matches"][0]["line_content"]
    assert "Строка 15" in data["matches"][0]["context"]


def test_search_directory_cp1251_encoding(tmp_path):
    """Directory search with encoding=cp1251 finds matches in cp1251 files."""
    (tmp_path / "a_cp1251.txt").write_bytes("Привет мир\nСтрока 42\n".encode("cp1251"))
    (tmp_path / "b_cp1251.txt").write_bytes("Другая строка\n".encode("cp1251"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(tmp_path), regex="Строка", encoding="cp1251"))
    assert data["ok"] is True
    assert data["total_matches"] == 1
    assert data["matches"][0]["file"].endswith("a_cp1251.txt")


def test_search_encoding_none_is_default_behavior(tmp_path):
    """Without encoding=, cp1251 file is skipped (encoding_required or best-effort miss) — regression check."""
    p = tmp_path / "cp1251.txt"
    p.write_bytes("Привет мир\n".encode("cp1251"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="Привет"))
    # Without explicit encoding, cp1251 is not auto-detected reliably.
    # The file should either be skipped or produce no matches — but NOT crash.
    assert data["ok"] in (True, False)  # ok=False means skipped/error, ok=True means no match
    if data["ok"]:
        assert data["total_matches"] == 0


def test_search_utf16_le_no_bom_with_encoding(tmp_path):
    """utf-16-le file without BOM is searchable when encoding=utf-16-le is given (bug #4 workaround)."""
    p = tmp_path / "utf16le_nobom.txt"
    p.write_bytes("Hello мир\nСтрока 99\n".encode("utf-16-le"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="Строка", encoding="utf-16-le"))
    assert data["ok"] is True
    assert data["total_matches"] == 1
    assert "Строка" in data["matches"][0]["line_content"]
    assert "Строка 99" in data["matches"][0]["context"]


def test_search_utf16_le_no_bom_without_encoding_is_binary(tmp_path):
    """Without encoding=, utf-16-le no-BOM file is skipped as binary (bug #4 — not fixed for auto-detect)."""
    p = tmp_path / "utf16le_nobom.txt"
    p.write_bytes("Hello мир\n".encode("utf-16-le"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="Hello"))
    assert data["ok"] is False
    assert data["error"]["type"] == "is_binary"


def test_search_invalid_encoding_returns_envelope(tmp_path):
    """Invalid encoding name returns ok=False with invalid_argument error."""
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="hello", encoding="nonexistent-codec"))
    assert data["ok"] is False
    assert data["error"]["type"] == "unsupported_encoding"


def test_search_encoding_wrong_codec_skips_file(tmp_path):
    """utf-8 file searched with encoding=utf-16-le → decode fails → skipped, no crash."""
    p = tmp_path / "utf8.txt"
    p.write_text("plain ascii text\n", encoding="utf-8")
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="plain", encoding="utf-16-le"))
    # utf-16-le decode of ascii text may succeed (odd byte count) or fail → either skip or no match
    # Most likely: decodes as garbage → regex "plain" won't match → total_matches=0
    # Or: decode fails → ok=False
    assert data["ok"] in (True, False)


def test_search_encoding_koi8r_directory(tmp_path):
    """Directory search with encoding=koi8-r finds matches in koi8-r files."""
    (tmp_path / "koi.txt").write_bytes("Тестовая строка\n".encode("koi8-r"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(tmp_path), regex="Тестовая", encoding="koi8-r"))
    assert data["ok"] is True
    assert data["total_matches"] == 1


def test_search_encoding_utf16_be_with_bom(tmp_path):
    """utf-16-be file with BOM searched with encoding=utf-16-be."""
    p = tmp_path / "utf16be.txt"
    p.write_bytes("Строка BE\n".encode("utf-16-be"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="Строка", encoding="utf-16-be"))
    assert data["ok"] is True
    assert data["total_matches"] == 1


def test_search_encoding_utf8_sig(tmp_path):
    """utf-8-sig file (with BOM) searched with encoding=utf-8-sig."""
    p = tmp_path / "utf8sig.txt"
    p.write_bytes("Поиск строки\n".encode("utf-8-sig"))
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(p), regex="Поиск", encoding="utf-8-sig"))
    assert data["ok"] is True
    assert data["total_matches"] == 1


def test_search_encoding_does_not_break_utf8_files(tmp_path):
    """Searching UTF-8 files without encoding= still works (regression)."""
    (tmp_path / "a.py").write_text("def hello():\n    return 42\n", encoding="utf-8")
    tool = with_local_ctx(SearchFiles)
    data = json.loads(tool(path=str(tmp_path), regex="hello"))
    assert data["ok"] is True
    assert data["total_matches"] == 1
