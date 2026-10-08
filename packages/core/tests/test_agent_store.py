"""Хранилище агента: промпты, AGENTS.md, субагенты и скиллы — через ``AgentStore``.

Файловое (по умолчанию) читает то же и оттуда же, что ядро до хранилища:
отказы те же. Своё хранилище (``MemoryAgentStore``) отдаёт ресурсы по
логическим именам — без путей в конфиге и без диска.
"""

from types import SimpleNamespace

import pytest
from gc_core.agent_store import (
    DEFAULT_AGENT_STORE,
    AgentResource,
    FileAgentStore,
    agent_store_of,
)
from gc_core.instructions.loader import build_instructions
from gc_core.run_preparation import PromptPreparationRequest, load_run_prompts
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import make_settings

SUBAGENT = """---
name: subagent_helper
description: Помощник из хранилища агента.
---
Ты помощник."""

SKILL = """---
name: Демо
description: Скилл из хранилища агента.
---
Делай по шагам."""


def test_file_store_reads_like_path_read_text(tmp_path):
    store = FileAgentStore()
    prompt = tmp_path / "p.md"
    prompt.write_text("текст", encoding="utf-8")
    assert store.read_text(AgentResource("prompt", "system", str(prompt))) == (
        "текст"
    )
    with pytest.raises(FileNotFoundError):
        store.read_text(AgentResource("prompt", "system", None))
    with pytest.raises(FileNotFoundError):
        store.read_text(AgentResource("prompt", "system", str(tmp_path / "x")))
    broken = tmp_path / "utf16.md"
    broken.write_bytes("текст".encode("utf-16"))
    with pytest.raises(UnicodeDecodeError):
        store.read_text(AgentResource("prompt", "system", str(broken)))
    (tmp_path / "dir").mkdir()
    assert store.list_dir(AgentResource("skills", "", str(tmp_path))) == [
        ("dir", "dir"),
        ("p.md", "file"),
        ("utf16.md", "file"),
    ]
    with pytest.raises(NotADirectoryError):
        store.list_dir(AgentResource("skills", "", str(prompt)))


def test_context_without_store_reads_files():
    assert agent_store_of(SimpleNamespace()) is DEFAULT_AGENT_STORE
    assert isinstance(DEFAULT_AGENT_STORE, FileAgentStore)


def test_run_prompts_keep_file_semantics(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_run_prompts(
            PromptPreparationRequest(system_prompt_path=str(tmp_path / "x"))
        )
    prepared = load_run_prompts(
        PromptPreparationRequest(
            special_instructions_path=str(tmp_path / "missing")
        )
    )
    assert prepared.system_prompt is None
    assert prepared.special_instructions is None


def test_run_prompts_from_own_store_without_paths():
    store = MemoryAgentStore(
        prompts={"system": "  SYS  ", "special_instructions": " SI "},
        fallback=None,
    )
    prepared = load_run_prompts(PromptPreparationRequest(), store=store)
    assert prepared.system_prompt == "SYS"
    assert "SI" in prepared.special_instructions
    assert ("prompt", "system") in store.reads


def test_instructions_from_own_store():
    store = MemoryAgentStore(instructions={"L0": "Роль запуска."})
    loaded = build_instructions(agent_md_path="", is_tui=False, store=store)
    assert loaded.loaded_ids == ["L0"]
    assert "Роль запуска." in loaded.text

    layers = MemoryAgentStore(
        instructions={"L1": "Глобально.", "L3": "Проект."}, fallback=None
    )
    loaded = build_instructions(agent_md_path="", is_tui=True, store=layers)
    assert loaded.loaded_ids == ["L1", "L3"]
    assert "Глобально." in loaded.text and "Проект." in loaded.text


def test_instructions_without_store_content_stay_empty_outside_tui():
    loaded = build_instructions(
        agent_md_path="", is_tui=False, store=MemoryAgentStore()
    )
    assert loaded.text == "" and loaded.loaded_ids == []


def test_subagents_from_own_store_without_dir():
    from gc_core.tools.subagents.custom.file_definitions import (
        discover_subagent_definitions,
    )

    source = SimpleNamespace(
        settings=make_settings({}),
        agent_store=MemoryAgentStore(
            subagents={"helper.md": SUBAGENT, "notes.txt": "x"}, fallback=None
        ),
    )
    definitions = discover_subagent_definitions(source, builtin_names=())
    assert [d.name for d in definitions] == ["subagent_helper"]
    assert definitions[0].role == "Ты помощник."


async def test_skills_from_own_store():
    from gc_core.tools.skills.store_files import store_skills_registry

    store = MemoryAgentStore(
        skills={
            "user/demo/SKILL.md": SKILL,
            "user/demo/references/guide.md": "Справка.",
            "user/demo/scripts/run.sh": "echo hi",
        },
        fallback=None,
    )
    registry = store_skills_registry(store, make_settings({}))
    listed = await registry.list_skills()
    assert [s["id"] for s in listed] == ["demo"]
    detail = await registry.load_skill_detail("demo")
    assert detail["instructions"].strip() == "Делай по шагам."
    assert detail["references"] == ["guide"]
    assert detail["scripts"] == ["run.sh"]
    reference = await registry.load_reference("demo", "guide")
    assert reference["content"] == "Справка."
    with pytest.raises(FileNotFoundError, match="не запускаются"):
        await registry.resolve_script("demo", "run.sh")


def test_service_prompts_go_through_the_store():
    from gc_core.intent_guard.final_report_consistency import (
        _load_guard_prompt,
    )
    from gc_core.security.command_moderation import load_command_judge_prompt
    from gc_core.summarize.prompt_loading import load_prompt_template

    store = MemoryAgentStore(
        prompts={
            "summary": "Сожми.",
            "finish_gate": "Проверь.",
            "command_judge_system": "Суди.",
        }
    )
    assert load_prompt_template("/nowhere.md", store=store) == "Сожми."
    assert _load_guard_prompt("/nowhere.md", store=store) == "Проверь."
    assert load_command_judge_prompt("system", store=store) == "Суди."
    # Чего нет у своего — у файлового: отказ тот же, что раньше.
    with pytest.raises(FileNotFoundError, match="Intent guard prompt"):
        _load_guard_prompt(
            "/nowhere.md", store=MemoryAgentStore(fallback=FileAgentStore())
        )


async def test_context_takes_the_store_and_rejects_a_wrong_one():
    from gc_core.run_context import acreate_run_context

    cfg = make_settings({"agent.model": "M", "logging.save_logs": False})
    store = MemoryAgentStore()
    ctx = await acreate_run_context(cfg, agent_store=store)
    assert ctx.agent_store is store
    default = await acreate_run_context(cfg)
    assert default.agent_store is DEFAULT_AGENT_STORE
    with pytest.raises(TypeError, match="AgentStore"):
        await acreate_run_context(cfg, agent_store=object())


async def test_project_memory_status_follows_stop_tools():
    """Выключенная память не рекламируется в промпте (D36)."""
    from gc_core.state_preparation import prepare_state

    on = await prepare_state(workspace_config=make_settings({}))
    off = await prepare_state(
        workspace_config=make_settings(
            {"agent.stop_tools": ["project_memory"]}
        )
    )
    assert on.long_memory_status
    assert off.long_memory_status is None


async def test_sdk_agent_uses_the_given_store(tmp_path, monkeypatch):
    """``AsyncAgent.create(..., agent_store=)``: системный промпт хода — из
    хранилища агента, а не из файла по пути конфига."""
    from gc_core import processing_responses as pr
    from gc_core.aio import AsyncAgent
    from helpers.turn_finish_apply import stub_turn_finish_apply
    from parity_harness import (
        ScriptedBackend,
        assistant_tool_call,
        install,
        judge_accept,
        parity_config,
    )

    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)
    backend = install(
        monkeypatch,
        ScriptedBackend(
            [
                assistant_tool_call(
                    "turn_finish",
                    {
                        "solution_type": "direct_answer",
                        "task_report": "готово",
                        "handoff": "{}",
                    },
                    call_id="c1",
                ),
                judge_accept(),
            ]
        ),
    )
    store = MemoryAgentStore(prompts={"system": "СИСТЕМА-ИЗ-ХРАНИЛИЩА"})
    cfg = parity_config(tmp_path, monkeypatch)
    async with await AsyncAgent.create(cfg, agent_store=store) as agent:
        assert await agent.run("привет", thread_id="t1")
    system = backend.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "СИСТЕМА-ИЗ-ХРАНИЛИЩА" in system["content"]
    assert ("prompt", "system") in store.reads
