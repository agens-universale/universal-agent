"""Хранилище агента: промпты, AGENTS.md, субагенты и скиллы — через ``AgentStore``.

Файловое (по умолчанию) читает то же и оттуда же, что ядро до хранилища:
отказы те же. Своё хранилище (``MemoryAgentStore``) отдаёт ресурсы по
логическим именам — без путей в конфиге и без диска.
"""

from types import SimpleNamespace

import pytest
from gc_core.agent_store import (
    FileAgentStore,
    ResourceInfo,
    agent_store_of,
)
from gc_core.instructions.loader import abuild_instructions
from gc_core.run_preparation import PromptPreparationRequest, aload_run_prompts
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


def _store(**dotted) -> FileAgentStore:
    return FileAgentStore(make_settings(dotted))


async def test_file_store_reads_like_path_read_text(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("текст", encoding="utf-8")
    store = _store(
        **{"paths.chat_completions_system_prompt_path": str(prompt)}
    )
    assert await store.read("prompt", "system") == "текст".encode()
    assert store.label("prompt", "system") == str(prompt)
    # Путь не задан — ресурса у файлового хранилища нет.
    assert await _store().read("prompt", "system") is None
    missing = _store(
        **{"paths.chat_completions_system_prompt_path": str(tmp_path / "x")}
    )
    with pytest.raises(FileNotFoundError):
        await missing.read("prompt", "system")
    (tmp_path / "dir").mkdir()
    listed = await _store(**{"agent.subagents_dir": str(tmp_path)}).list(
        "subagent"
    )
    assert listed == [ResourceInfo("dir", "dir"), ResourceInfo("p.md", "file")]
    with pytest.raises(NotADirectoryError):
        await _store(**{"agent.subagents_dir": str(prompt)}).list("subagent")
    with pytest.raises(FileNotFoundError):
        await _store().list("subagent")


def test_context_without_store_reads_files():
    assert isinstance(agent_store_of(SimpleNamespace()), FileAgentStore)


async def test_run_prompts_keep_file_semantics(tmp_path):
    with pytest.raises(FileNotFoundError):
        await aload_run_prompts(
            PromptPreparationRequest(),
            store=_store(
                **{
                    "paths.chat_completions_system_prompt_path": str(
                        tmp_path / "x"
                    )
                }
            ),
        )
    prepared = await aload_run_prompts(
        PromptPreparationRequest(),
        store=_store(
            **{
                "paths.chat_completions_special_instructions_path": str(
                    tmp_path / "missing"
                )
            }
        ),
    )
    assert prepared.special_instructions is None


async def test_run_prompts_from_own_store_without_paths():
    store = MemoryAgentStore(
        prompts={"system": "  SYS  ", "special_instructions": " SI "},
        fallback=None,
    )
    prepared = await aload_run_prompts(PromptPreparationRequest(), store=store)
    assert prepared.system_prompt == "SYS"
    assert "SI" in prepared.special_instructions
    assert ("prompt", "system") in store.reads


async def test_instructions_from_own_store(tmp_path):
    store = MemoryAgentStore(instructions={"L0": "Роль запуска."})
    loaded = await abuild_instructions(is_tui=False, store=store)
    assert loaded.loaded_ids == ["L0"]
    assert "Роль запуска." in loaded.text

    # L1 — из хранилища агента; L2 и L3 — файлы workspace (спека, 6.1).
    (tmp_path / "AGENTS.md").write_text("Проект.", encoding="utf-8")
    layers = MemoryAgentStore(instructions={"L1": "Глобально."}, fallback=None)
    loaded = await abuild_instructions(
        is_tui=True, store=layers, cwd=str(tmp_path)
    )
    assert loaded.loaded_ids == ["L1", "L3"]
    assert "Глобально." in loaded.text and "Проект." in loaded.text


async def test_instructions_without_store_content_stay_empty_outside_tui():
    loaded = await abuild_instructions(
        is_tui=False, store=MemoryAgentStore(fallback=None)
    )
    assert loaded.text == "" and loaded.loaded_ids == []


async def test_subagents_from_own_store_without_dir():
    from gc_core.tools.subagents.custom.file_definitions import (
        adiscover_subagent_definitions,
    )

    source = SimpleNamespace(
        settings=make_settings({}),
        agent_store=MemoryAgentStore(
            subagents={"helper.md": SUBAGENT, "notes.txt": "x"}, fallback=None
        ),
    )
    definitions = await adiscover_subagent_definitions(
        source, builtin_names=()
    )
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
    # Без среды исполнения скрипт выложить некуда — понятный отказ.
    with pytest.raises(FileNotFoundError, match="среды исполнения"):
        await registry.resolve_script("demo", "run.sh")


class _DictBackend:
    """Среда исполнения в памяти: что и сколько раз туда записали."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.writes = 0

    async def makedirs(self, path: str) -> None:
        return None

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data
        self.writes += 1

    async def write(self, path: str, text: str) -> None:
        self.files[path] = text.encode()

    async def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path].decode()


async def test_scripts_of_own_store_skill_are_provisioned_once(tmp_path):
    from gc_core.tools.skills.store_files import store_skills_registry

    store = MemoryAgentStore(
        skills={
            "user/demo/SKILL.md": SKILL,
            "user/demo/scripts/run.sh": "echo hi",
        },
        fallback=None,
    )
    runtime = _DictBackend()
    registry = store_skills_registry(
        store,
        make_settings({"launch_root": str(tmp_path)}),
        runtime_backend=runtime,
    )
    resolved = await registry.resolve_script("demo", "run.sh")
    root = f"{tmp_path}/.gc/skills_runtime/user/demo"
    assert resolved["path"] == f"{root}/scripts/run.sh"
    assert runtime.files[resolved["path"]] == b"echo hi"
    assert f"{root}/.manifest.sha256" in runtime.files
    copied = runtime.writes
    await registry.resolve_script("demo", "scripts/run.sh")
    assert runtime.writes == copied, "неизменённый скилл выложен заново"


async def test_service_prompts_go_through_the_store():
    from gc_core.intent_guard.final_report_consistency import (
        aload_guard_prompt,
    )
    from gc_core.security.command_moderation import (
        aload_command_judge_prompt,
    )
    from gc_core.summarize.prompt_loading import aload_summary_prompt

    store = MemoryAgentStore(
        prompts={
            "summary": "Сожми.",
            "finish_gate": "Проверь.",
            "command_judge_system": "Суди.",
        }
    )
    assert await aload_summary_prompt(store) == "Сожми."
    assert await aload_guard_prompt(store) == "Проверь."
    assert await aload_command_judge_prompt("system", store=store) == "Суди."
    # Чего нет ни у своего, ни у файлового — отказ тот же, что раньше.
    missing = _store(
        **{"paths.chat_completions_intent_guard_prompt_path": "/nowhere.md"}
    )
    with pytest.raises(FileNotFoundError, match="Intent guard prompt"):
        await aload_guard_prompt(MemoryAgentStore(fallback=missing))


async def test_context_takes_the_store_and_rejects_a_wrong_one():
    from gc_core.run_context import acreate_run_context

    cfg = make_settings({"agent.model": "M", "logging.save_logs": False})
    store = MemoryAgentStore()
    ctx = await acreate_run_context(cfg, agent_store=store)
    assert ctx.agent_store is store
    default = await acreate_run_context(cfg)
    assert isinstance(default.agent_store, FileAgentStore)
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


async def test_context_finds_subagents_in_its_own_store():
    """Сборка контекста ищет кастомных субагентов в хранилище агента
    контекста, а не на диске: субагент из ``MemoryAgentStore`` — среди тулов
    и определений."""
    from gc_core.run_context import acreate_run_context

    store = MemoryAgentStore(subagents={"helper.md": SUBAGENT})
    ctx = await acreate_run_context(
        make_settings(
            {
                "agent.model": "M",
                "backend.api_mode": "chat_completions",
                "agent.use_local_subagents": True,
                "logging.save_logs": False,
            }
        ),
        agent_store=store,
    )
    assert "subagent_helper" in ctx.subagent_definitions
    assert "subagent_helper" in ctx.tools_data
    assert ("subagent", "helper.md") in store.reads
