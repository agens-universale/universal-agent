from pathlib import Path

import pytest

from states import State


def _make_state(tmp_path: Path) -> State:
    return State(
        verbose=False,
        model="test-model",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=1,
        system_prompt_id="test",
        use_server_subagents=False,
        project_gc_dir=tmp_path / ".gc",
    )


def test_set_call_metadata_injects_empty_status_when_file_absent(tmp_path):
    state = _make_state(tmp_path)
    pv = state.call_metadata["promptVariables"]
    assert "longMemoryStatus" in pv
    assert "doesn't have long memory" in pv["longMemoryStatus"]


def test_set_call_metadata_injects_populated_status(tmp_path):
    (tmp_path / ".gc").mkdir()
    body = "saved fact one\n\nsaved fact two"
    (tmp_path / ".gc" / "long_memory.md").write_text(body, encoding="utf-8")
    state = _make_state(tmp_path)
    pv = state.call_metadata["promptVariables"]
    assert "project has long memory" in pv["longMemoryStatus"]
    assert f"{len(body)} chars" in pv["longMemoryStatus"]


def test_status_does_not_leak_path_or_tool_name(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "long_memory.md").write_text("body", encoding="utf-8")
    state = _make_state(tmp_path)
    text = state.call_metadata["promptVariables"]["longMemoryStatus"]
    assert ".gc" not in text
    assert ".md" not in text
    assert "long_memory" not in text


def test_set_call_metadata_safe_when_project_gc_dir_missing(tmp_path):
    """If State is constructed without project_gc_dir (legacy callers), no crash."""
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=1,
        system_prompt_id="test",
        use_server_subagents=False,
        # project_gc_dir omitted on purpose
    )
    pv = state.call_metadata["promptVariables"]
    # Either the key is absent, or it carries the empty status. Both are acceptable.
    if "longMemoryStatus" in pv:
        assert "doesn't have long memory" in pv["longMemoryStatus"]


def test_main_prompt_template_renders_long_memory_status(tmp_path):
    """Render the full chat_completions_system_prompt.md and assert the status text appears."""
    from utils.local_render_system_prompt import render_system_prompt
    from pathlib import Path as _P

    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "long_memory.md").write_text("seeded body", encoding="utf-8")
    state = _make_state(tmp_path)

    repo_root = _P(__file__).resolve().parent.parent
    template = (repo_root / "prompts" / "chat_completions_system_prompt.md").read_text(
        encoding="utf-8"
    )

    rendered = render_system_prompt(
        system_prompt=template,
        tools_spec=[],
        project_info=state.call_metadata["promptVariables"],
        specific_role=None,
    )

    assert "project has long memory" in rendered
    assert "seeded body" not in rendered  # body content must NOT be inlined


def test_subagent_prompt_template_renders_long_memory_status(tmp_path):
    from utils.local_render_system_prompt import render_system_prompt
    from pathlib import Path as _P

    state = _make_state(tmp_path)  # no file → empty status

    repo_root = _P(__file__).resolve().parent.parent
    template = (
        repo_root / "prompts" / "chat_completions_subagent_system_prompt.md"
    ).read_text(encoding="utf-8")

    rendered = render_system_prompt(
        system_prompt=template,
        tools_spec=[],
        project_info=state.call_metadata["promptVariables"],
        specific_role=None,
    )

    assert "doesn't have long memory" in rendered
