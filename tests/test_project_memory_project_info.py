"""Integration of the long-memory status nudge into the system prompt.

State no longer knows anything about long memory — the injection lives in
`long_memory.attach_status_to_prompt_vars`, called by the agent runtime
after state construction. These tests pin that contract and the resulting
rendered prompt.
"""
from pathlib import Path

from project_memory import attach_status_to_prompt_vars


def test_attach_status_with_no_memory_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompt_vars: dict = {}
    attach_status_to_prompt_vars(prompt_vars)
    assert "longMemoryStatus" in prompt_vars
    assert "doesn't have long memory yet" in prompt_vars["longMemoryStatus"]
    assert "Limit: 10000 chars (0% filled)." in prompt_vars["longMemoryStatus"]


def test_attach_status_with_populated_memory(tmp_path, monkeypatch):
    (tmp_path / ".gc").mkdir()
    body = "saved fact one\n\nsaved fact two"
    (tmp_path / ".gc" / "project_memory.md").write_text(body, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    prompt_vars: dict = {}
    attach_status_to_prompt_vars(prompt_vars)
    assert "project has long memory" in prompt_vars["longMemoryStatus"]
    assert f"{len(body)}/10000 chars" in prompt_vars["longMemoryStatus"]
    assert "% filled" in prompt_vars["longMemoryStatus"]
    assert "appropriate memory tool" in prompt_vars["longMemoryStatus"]
    assert "MUST" not in prompt_vars["longMemoryStatus"]
    assert "FIRST STEP" not in prompt_vars["longMemoryStatus"]


def test_attach_status_skipped_when_long_memory_in_stop_tools(tmp_path, monkeypatch):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("body", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    prompt_vars: dict = {}
    attach_status_to_prompt_vars(
        prompt_vars, config_dict={"stop_tools": ["project_memory"]}
    )
    assert "longMemoryStatus" not in prompt_vars


def test_attach_status_uses_configured_limit(tmp_path, monkeypatch):
    (tmp_path / ".gc").mkdir()
    body = "seeded"
    (tmp_path / ".gc" / "project_memory.md").write_text(body, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    prompt_vars: dict = {}
    attach_status_to_prompt_vars(
        prompt_vars,
        config_dict={"project_memory_params": {"max_chars": 1000}},
    )
    assert f"{len(body)}/1000 chars" in prompt_vars["longMemoryStatus"]
    assert "% filled" in prompt_vars["longMemoryStatus"]


def test_attach_status_with_explicit_cwd(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("seeded", encoding="utf-8")
    prompt_vars: dict = {}
    attach_status_to_prompt_vars(prompt_vars, cwd=str(tmp_path))
    assert "project has long memory" in prompt_vars["longMemoryStatus"]


def test_attach_status_does_not_leak_path_or_tool_name(tmp_path, monkeypatch):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("body", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    prompt_vars: dict = {}
    attach_status_to_prompt_vars(prompt_vars)
    text = prompt_vars["longMemoryStatus"]
    assert ".gc" not in text
    assert ".md" not in text
    assert "project_memory" not in text


def test_main_prompt_template_renders_long_memory_status(tmp_path, monkeypatch):
    """End-to-end: attach status + render full system prompt template."""
    from utils.local_render_system_prompt import render_system_prompt

    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("seeded body", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    prompt_vars: dict = {}
    attach_status_to_prompt_vars(prompt_vars)

    repo_root = Path(__file__).resolve().parent.parent
    template = (repo_root / "prompts" / "chat_completions_system_prompt.md").read_text(
        encoding="utf-8"
    )

    rendered = render_system_prompt(
        system_prompt=template,
        tools_spec=[],
        project_info=prompt_vars,
        specific_role=None,
    )

    assert "project has long memory" in rendered
    assert "appropriate memory tool" in rendered
    assert "MUST" not in rendered
    assert "FIRST STEP" not in rendered
    assert "seeded body" not in rendered  # body content must NOT be inlined


def test_subagent_prompt_template_renders_empty_status(tmp_path, monkeypatch):
    from utils.local_render_system_prompt import render_system_prompt

    monkeypatch.chdir(tmp_path)
    prompt_vars: dict = {}
    attach_status_to_prompt_vars(prompt_vars)

    repo_root = Path(__file__).resolve().parent.parent
    template = (
        repo_root / "prompts" / "chat_completions_subagent_system_prompt.md"
    ).read_text(encoding="utf-8")

    rendered = render_system_prompt(
        system_prompt=template,
        tools_spec=[],
        project_info=prompt_vars,
        specific_role=None,
    )

    assert "doesn't have long memory yet" in rendered
    assert "Limit: 10000 chars (0% filled)." in rendered
