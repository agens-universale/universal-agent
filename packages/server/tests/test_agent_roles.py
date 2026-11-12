"""Роли поверхностей при хранилище агента в базе.

Один агент на сервер (``storage.agent.agent_id``): промпты, скиллы и память
проекта — общие. Роль поверхности — свой вариант L0 и специнструкций внутри
агента (``<поверхность>.role``, по умолчанию имя поверхности); у
поверхности может быть и свой агент (``<поверхность>.agent_id``). На файлах
всё как раньше: роль задаёт ``<поверхность>.agent_md_path``.
"""

import pytest
from gc_core.agent_store.roles import RoleAgentStore
from gc_core.settings import settings_of
from gc_server.agent_roles import acheck_roles, aopen_surface_agents
from gc_server.app import ServerRuntime
from gc_server.chat.agent_config import prepare_agent_settings
from gc_server.config import ServerConfig, ServerConfigError, surface_settings
from gc_server.stores import aopen_process_stores
from gc_stores.sync import copy_defaults
from gc_testkit.settings import make_settings


def _db_settings(tmp_path, **extra):
    return make_settings(
        {
            "paths.threads_data_path": str(tmp_path / "threads"),
            "storage": {
                "agent": {
                    "type": "sqlite",
                    "path": str(tmp_path / "a.db"),
                    "agent_id": "srv",
                },
                "ensure_schema": True,
            },
            **extra,
        }
    )


async def _seed_prompts(tmp_path, *agent_ids):
    """Промпты ядра — без ролей: роли каждый тест кладёт сам."""
    from gc_core.agent_store import REQUIRED_PROMPTS, FileAgentStore
    from gc_stores import sqlite

    bundled = FileAgentStore()
    for agent_id in agent_ids or ("srv",):
        store = sqlite.SQLiteAgentStore(
            str(tmp_path / "a.db"), agent_id=agent_id
        )
        try:
            for prompt in REQUIRED_PROMPTS:
                await store.put_resource(
                    "prompt",
                    prompt.value,
                    await bundled.read("prompt", prompt),
                )
        finally:
            await store.close()


def _config(**sections):
    return ServerConfig.model_validate(sections)


def test_surface_settings_carry_the_role_with_an_agent_store(tmp_path):
    base = _db_settings(tmp_path)
    cfg = _config(chat={"role": "support"})
    webhook = surface_settings(base, cfg, "webhook")
    assert settings_of(webhook).agent.role == "webhook"
    # Роль в базе — не путь: L0 поверхности читается из хранилища.
    assert not settings_of(webhook).paths.agent_md_path
    assert settings_of(surface_settings(base, cfg, "chat")).agent.role == (
        "support"
    )


def test_files_agent_store_keeps_the_role_path(tmp_path):
    base = make_settings({"paths.threads_data_path": str(tmp_path)})
    webhook = settings_of(surface_settings(base, ServerConfig(), "webhook"))
    assert webhook.agent.role is None
    assert webhook.paths.agent_md_path


def test_completions_facade_takes_its_special_instructions_variant(tmp_path):
    base = surface_settings(_db_settings(tmp_path), ServerConfig(), "chat")
    agent = settings_of(
        prepare_agent_settings(
            base,
            cwd=str(tmp_path),
            instructions_path="/x.md",
            variant="completions",
        )
    ).agent
    assert (agent.role, agent.special_instructions_role) == (
        "chat",
        "chat.completions",
    )


async def _runtime(tmp_path, cfg, names=("webhook", "chat", "a2a")):
    base = _db_settings(tmp_path)
    await _seed_prompts(tmp_path, "srv", "a2a-bot")
    stores = await aopen_process_stores(base)
    runtime = ServerRuntime(base=base, config=cfg, stores=stores)
    runtime.surface_agents = await aopen_surface_agents(base, cfg, list(names))
    return runtime


async def test_missing_role_is_a_start_error_with_a_hint(tmp_path):
    runtime = await _runtime(tmp_path, ServerConfig())
    try:
        with pytest.raises(ServerConfigError) as err:
            await acheck_roles(runtime, ["webhook"])
        assert "roles/webhook/L0.md" in str(err.value)
        assert "gc-stores sync" in str(err.value)
    finally:
        await runtime.stores.aclose()


async def test_default_roles_let_the_server_start(tmp_path):
    runtime = await _runtime(tmp_path, ServerConfig())
    try:
        await copy_defaults(runtime.stores.agent)
        await acheck_roles(runtime, ["webhook", "chat", "a2a", "swarm"])
    finally:
        await runtime.stores.aclose()


async def test_chat_needs_both_special_instruction_variants(tmp_path):
    runtime = await _runtime(tmp_path, ServerConfig())
    try:
        await runtime.stores.agent.put_resource(
            "prompt", "special_instructions@chat", b"x"
        )
        with pytest.raises(ServerConfigError) as err:
            await acheck_roles(runtime, ["chat"])
        assert "roles/chat.completions/special_instructions.md" in str(
            err.value
        )
    finally:
        await runtime.stores.aclose()


async def test_role_path_with_an_agent_store_is_a_start_error(tmp_path):
    runtime = await _runtime(
        tmp_path, _config(webhook={"agent_md_path": "./prompts/x.md"})
    )
    try:
        await copy_defaults(runtime.stores.agent)
        with pytest.raises(ServerConfigError) as err:
            await acheck_roles(runtime, ["webhook"])
        assert "webhook.agent_md_path" in str(err.value)
        assert "roles/webhook/L0.md" in str(err.value)
    finally:
        await runtime.stores.aclose()


async def test_webhook_without_a_role_needs_no_l0(tmp_path):
    runtime = await _runtime(
        tmp_path, _config(webhook={"agent_md_path": None})
    )
    try:
        await acheck_roles(runtime, ["webhook"])
    finally:
        await runtime.stores.aclose()


async def test_surface_may_run_its_own_agent(tmp_path):
    """``a2a.agent_id``: свой агент со своими ресурсами и памятью; остальные
    поверхности — на общем."""
    runtime = await _runtime(tmp_path, _config(a2a={"agent_id": "a2a-bot"}))
    try:
        own = runtime.store_kwargs("a2a")["agent_store"]
        assert own.agent_id == "a2a-bot"
        assert runtime.store_kwargs("webhook")["agent_store"].agent_id == "srv"
        assert runtime.store_kwargs()["agent_store"].agent_id == "srv"
        assert not isinstance(own, RoleAgentStore)
    finally:
        for store in runtime.surface_agents.values():
            await store.close()
        await runtime.stores.aclose()


async def test_own_agent_id_on_files_is_a_start_error(tmp_path):
    base = make_settings({"paths.threads_data_path": str(tmp_path)})
    with pytest.raises(ServerConfigError, match="a2a.agent_id"):
        await aopen_surface_agents(
            base, _config(a2a={"agent_id": "x"}), ["a2a"]
        )


async def test_webhook_turn_uses_the_role_from_the_store(
    tmp_path, monkeypatch
):
    """Ход вебхука на хранилище агента в базе: L0 — вариант роли
    ``L0@webhook`` из хранилища, а не путь роли на диске."""
    import asyncio
    import json

    import httpx
    from gc_server.app import build_surface
    from gc_testkit import scripted_model
    from server_harness import LOOPBACK_URL

    seen = scripted_model.install(monkeypatch)
    base = _db_settings(
        tmp_path,
        **{
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://scripted.invalid/v1/chat/completions",
            "backend.chat_completion_kwargs": {"stream": False},
            "agent.model": "scripted",
            "agent.max_steps": 3,
            "agent.use_local_subagents": False,
            "agent.strict_turn_finish_validation": False,
            "logging.save_logs": False,
            "logging.console_trace": False,
        },
    )
    await _seed_prompts(tmp_path)
    stores = await aopen_process_stores(base)
    await copy_defaults(stores.agent)
    await stores.agent.put_resource(
        "instructions", "L0@webhook", "РОЛЬ-ВЕБХУКА-ИЗ-БАЗЫ".encode()
    )
    runtime = ServerRuntime(base=base, config=ServerConfig(), stores=stores)
    await acheck_roles(runtime, ["webhook"])
    surface = build_surface(runtime, "webhook")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=surface.app()),
            base_url=LOOPBACK_URL,
        ) as client:
            await client.post("/v1/tasks", json={"task_id": "t", "text": "x"})
            for _ in range(200):
                body = (await client.get("/v1/tasks/generic/t")).json()
                if body.get("status") != "running":
                    break
                await asyncio.sleep(0.05)
    finally:
        await surface.astop()
        await stores.aclose()
    assert seen, body
    assert "РОЛЬ-ВЕБХУКА-ИЗ-БАЗЫ" in json.dumps(seen[0], ensure_ascii=False)
