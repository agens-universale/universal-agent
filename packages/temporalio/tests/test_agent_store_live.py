"""Хранилище агента под Temporal: ``AgentPlugin(agent_store=…)``.

Системный промпт хода и роль запуска (L0) — из своего хранилища агента, а не из
файлов на диске воркера; читаются они в activity воркера, код workflow ресурсов
не касается.
"""

import asyncio

import pytest
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core_temporalio import AgentPlugin, AgentSession
from gc_stores.sqlite import SQLiteThreadStore
from gc_testkit.memory_agent_store import MemoryAgentStore
from live_support import temporal_cli
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_workflow_controls import finish, scripted_http  # noqa: F401


@pytest.mark.parametrize("own_system_prompt", [True, False])
async def test_worker_reads_prompts_from_the_agent_store(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
    own_system_prompt,
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.append([finish("ok")])
    settings = LoadedSettings(
        settings=resolve_paths(
            AppConfig.model_validate(
                {
                    "backend": {
                        "api_mode": "chat_completions",
                        "completions_url": url,
                        "chat_completion_kwargs": {"stream": False},
                    },
                    "agent": {
                        "model": "scripted",
                        "max_steps": 3,
                        "use_local_subagents": False,
                        "strict_turn_finish_validation": False,
                    },
                }
            ),
            str(tmp_path),
        ),
        runtime=RuntimeEnv(launch_root=str(tmp_path)),
    )
    # Свой системный промпт — шаблон без места под инструкции; роль запуска
    # видна в штатном шаблоне (его хранилище берёт у файлового).
    agent_store = MemoryAgentStore(
        prompts={"system": "СИСТЕМА-ИЗ-ХРАНИЛИЩА"}
        if own_system_prompt
        else {},
        instructions={"L0": "РОЛЬ-ИЗ-ХРАНИЛИЩА"},
    )
    plugin = AgentPlugin(
        {"a": settings},
        thread_store=SQLiteThreadStore(str(tmp_path / "threads.sqlite")),
        workspace=tmp_path,
        agent_store=agent_store,
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="q", plugins=[plugin]):
            session = AgentSession(
                env.client, agent="a", thread_id="t1", task_queue="q"
            )
            await asyncio.wait_for(session.ask("привет"), 60)
            await session.close()

    system = requests[0]["messages"][0]
    assert system["role"] == "system"
    if own_system_prompt:
        assert system["content"] == "СИСТЕМА-ИЗ-ХРАНИЛИЩА"
    else:
        assert "РОЛЬ-ИЗ-ХРАНИЛИЩА" in system["content"]
    assert ("prompt", "system") in agent_store.reads
    assert ("instructions", "L0") in agent_store.reads


async def test_oversized_prompt_from_the_store_fails_the_turn_not_the_session(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Промпт из хранилища агента больше записи истории: подготовка хода не
    ложится в историю — отказ хода (``TurnFailed``), а не сессии; сессия
    принимает следующие сообщения."""
    from gc_core_temporalio.client import TurnFailed

    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.append([finish("ok")])
    settings = LoadedSettings(
        settings=resolve_paths(
            AppConfig.model_validate(
                {
                    "backend": {
                        "api_mode": "chat_completions",
                        "completions_url": url,
                        "chat_completion_kwargs": {"stream": False},
                    },
                    "agent": {"model": "scripted", "max_steps": 3},
                }
            ),
            str(tmp_path),
        ),
        runtime=RuntimeEnv(launch_root=str(tmp_path)),
    )
    store = MemoryAgentStore(prompts={"system": "ж" * 400_000})
    plugin = AgentPlugin(
        {"a": settings},
        thread_store=SQLiteThreadStore(str(tmp_path / "threads.sqlite")),
        workspace=tmp_path,
        agent_store=store,
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="q", plugins=[plugin]):
            session = AgentSession(
                env.client, agent="a", thread_id="t1", task_queue="q"
            )
            for text in ("первый", "второй"):
                with pytest.raises(TurnFailed, match="prepare"):
                    await asyncio.wait_for(session.ask(text), 60)
            status = await session.status()
            assert status["phase"] == "idle"
            await session.close()
