from long_memory.status import build_long_memory_status


def test_status_missing_file(tmp_path):
    text = build_long_memory_status(tmp_path / "long_memory.md")
    assert "doesn't have long memory" in text
    assert "appropriate tool" in text
    assert "store important facts" in text


def test_status_empty_file(tmp_path):
    p = tmp_path / "long_memory.md"
    p.write_text("", encoding="utf-8")
    text = build_long_memory_status(p)
    assert "doesn't have long memory" in text


def test_status_whitespace_only_file(tmp_path):
    p = tmp_path / "long_memory.md"
    p.write_text("   \n\n  ", encoding="utf-8")
    text = build_long_memory_status(p)
    assert "doesn't have long memory" in text


def test_status_populated_file(tmp_path):
    p = tmp_path / "long_memory.md"
    body = "fact one\n\nfact two\n\nfact three"
    p.write_text(body, encoding="utf-8")
    text = build_long_memory_status(p)
    assert "project has long memory" in text
    assert f"{len(body)} chars" in text
    assert "lines" in text
    assert "Read it with appropriate tool" in text


def test_status_no_path_or_tool_name_leaked(tmp_path):
    """Must not leak the file path or the tool name to the model."""
    p = tmp_path / "long_memory.md"
    p.write_text("body", encoding="utf-8")
    text_full = build_long_memory_status(p)
    text_empty = build_long_memory_status(tmp_path / "missing.md")
    for text in (text_full, text_empty):
        assert ".gc/" not in text
        assert ".md" not in text
        assert "long_memory" not in text  # tool name must not appear
