import json

from tools.fs.search_files import SearchFiles


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
        assert data["total_matches"] == 3, (regex, data["total_matches"], data["matches"])
        assert [m["line"] for m in data["matches"]] == [1, 2, 3]


def test_search_alternation_cyrillic(tmp_path):
    (tmp_path / "t.md").write_text(
        "строка 1: Аудит\n"
        "строка 2: План проверки\n"
        "строка 3: Аудит и План проверки\n",
        encoding="utf-8",
    )
    data = json.loads(with_local_ctx(SearchFiles)(path=str(tmp_path), regex="Аудит|План проверки"))
    assert data["ok"] is True
    assert data["total_matches"] == 3
    assert [m["line"] for m in data["matches"]] == [1, 2, 3]


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

