from project_memory.status import build_project_memory_status


def test_status_missing_file(tmp_path):
    text = build_project_memory_status(tmp_path / "project_memory.md")
    assert "doesn't have long memory yet" in text
    assert "persist it for future sessions" in text
    assert "appropriate memory tool" in text


def test_status_empty_file(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("", encoding="utf-8")
    text = build_project_memory_status(p)
    assert "doesn't have long memory yet" in text


def test_status_whitespace_only_file(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("   \n\n  ", encoding="utf-8")
    text = build_project_memory_status(p)
    assert "doesn't have long memory yet" in text


def test_status_populated_file(tmp_path):
    p = tmp_path / "project_memory.md"
    body = "fact one\n\nfact two\n\nfact three"
    p.write_text(body, encoding="utf-8")
    text = build_project_memory_status(p)
    assert "project has long memory" in text
    assert "MUST" not in text
    assert "FIRST STEP" not in text
    assert f"{len(body)} chars" in text
    assert "lines" in text
    assert "appropriate memory tool" in text
    assert "not on every turn" in text


def test_status_no_path_or_tool_name_leaked(tmp_path):
    """Must not leak the file path or the tool name to the model."""
    p = tmp_path / "project_memory.md"
    p.write_text("body", encoding="utf-8")
    text_full = build_project_memory_status(p)
    text_empty = build_project_memory_status(tmp_path / "missing.md")
    for text in (text_full, text_empty):
        assert ".gc/" not in text
        assert ".md" not in text
        assert "project_memory" not in text  # tool name must not appear
