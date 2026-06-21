from instructions.init_agents_md import build_init_prompt


def test_no_file_branch_instructs_create(tmp_path):
    p = build_init_prompt(cwd=str(tmp_path), can_ask=True)
    assert "write_to_file" in p
    assert "create it from scratch" in p
    assert "ask_human" not in p


def test_exists_can_ask_branch_instructs_ask(tmp_path):
    (tmp_path / "AGENTS.md").write_text("old", encoding="utf-8")
    p = build_init_prompt(cwd=str(tmp_path), can_ask=True)
    assert "ask_human" in p
    assert "UPDATE" in p and "RECREATE" in p


def test_exists_headless_branch_updates_no_ask(tmp_path):
    (tmp_path / "AGENTS.md").write_text("old", encoding="utf-8")
    p = build_init_prompt(cwd=str(tmp_path), can_ask=False)
    assert "UPDATE it in place" in p
    assert "Do NOT recreate" in p
    assert "do NOT ask" in p
    assert "ask_human" not in p
