"""Пикер модели ACP: алиас в протоколе, уровень в сессии."""

import asyncio
from types import SimpleNamespace

from gc_acp.facade import AcpFacade
from gc_acp.model_config import active_model_line, model_config_option
from gc_core.config import cli_forces_legacy_model
from gc_core.model_router import CircuitBreaker, ModelRouter
from gc_core.processing_responses import resolve_main_for_call
from gc_core.turn_boundary import begin_turn
from gc_testkit.settings import make_settings
from test_facade import (
    FakeAgent,
    MemoryWriter,
    _by_id,
    _init,
    _line,
    _new,
    _updates,
)


def _loaded(tmp_path, extra=None, *, cli_model=None):
    cfg = {
        "paths.threads_data_path": str(tmp_path),
        "os_backend": "local",
    }
    if extra:
        cfg.update(extra)
    loaded = make_settings(cfg)
    if cli_model is not None:
        loaded = loaded.with_overrides({"agent.model": cli_model}, layer="cli")
    return loaded


def _models(**slots):
    return {
        "models": {
            "glm-local": {"model": "glm-local"},
            "flash": {"model": "qwen/qwen-2.5-flash"},
            "opus": {"model": "opus"},
            "judge": {"model": "judge-only"},
        },
        "slots": {
            "default": slots.get("default", "glm-local"),
            "light": slots.get("light", "flash"),
            "max": slots.get("max", "opus"),
        },
        "assign": {"summary": "judge"},
    }


def _open(loaded, factory=None):
    agents = []

    async def default_factory(config, **kwargs):
        agent = FakeAgent()
        agent.factory_kwargs = kwargs
        agent.router = ModelRouter(
            config,
            level_provider=kwargs["level_provider"],
            force_legacy=cli_forces_legacy_model(config),
        )
        agents.append(agent)
        return agent

    writer = MemoryWriter()
    facade = AcpFacade(
        settings=loaded,
        writer=writer,
        agent_factory=factory or default_factory,
    )
    return facade, writer, agents


def _option(result):
    options = {item["id"]: item for item in result["configOptions"]}
    assert set(options) <= {"model", "mode"}
    option = options["model"]
    assert option["category"] == "model"
    assert option["type"] == "select"
    return option


def _values(option):
    return [item["value"] for item in option["options"]]


async def _set(facade, session_id, value, request_id=9, **extra):
    params = {
        "sessionId": session_id,
        "configId": "model",
        "value": value,
    }
    params.update(extra)
    await facade.handle_line(
        _line("session/set_config_option", params, request_id)
    )


async def test_new_load_resume_list_aliases_in_level_order(tmp_path):
    facade, writer, _agents = _open(_loaded(tmp_path, _models()))
    await _init(facade)
    await _new(facade, "/work")
    created = _by_id(writer, 2)["result"]
    option = _option(created)
    assert option["currentValue"] == "glm-local"
    assert _values(option) == ["glm-local", "flash", "opus"]
    assert "description" not in option["options"][0]
    assert option["options"][1]["description"] == "qwen/qwen-2.5-flash"
    assert "description" not in option["options"][2]
    assert "judge" not in _values(option)
    dumped = writer.sent
    assert all(item.get("method") != "config_option_update" for item in dumped)
    session_id = created["sessionId"]
    await facade.handle_line(
        _line("session/close", {"sessionId": session_id}, 3)
    )
    await facade.handle_line(
        _line(
            "session/resume",
            {"sessionId": session_id, "cwd": "/work"},
            4,
        )
    )
    assert _values(_option(_by_id(writer, 4)["result"])) == [
        "glm-local",
        "flash",
        "opus",
    ]
    await facade.handle_line(
        _line(
            "session/load",
            {"sessionId": session_id, "cwd": "/work", "mcpServers": []},
            5,
        )
    )
    assert _option(_by_id(writer, 5)["result"])["currentValue"] == "glm-local"
    await facade.shutdown()


async def test_duplicate_alias_is_kept_once(tmp_path):
    loaded = _loaded(
        tmp_path, _models(default="glm-local", light="flash", max="glm-local")
    )
    facade, writer, _agents = _open(loaded)
    await _init(facade)
    await _new(facade, "/work")
    assert _values(_option(_by_id(writer, 2)["result"])) == [
        "glm-local",
        "flash",
    ]
    await facade.shutdown()


async def test_set_writes_level_not_config(tmp_path):
    loaded = _loaded(tmp_path, _models())
    before = loaded.settings.model_dump()
    facade, writer, agents = _open(loaded)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await _set(facade, session_id, "flash")
    option = _option(_by_id(writer, 9)["result"])
    assert option["currentValue"] == "flash"
    assert _values(option) == ["glm-local", "flash", "opus"]
    live = facade._sessions[session_id]
    assert live.level == "light"
    assert agents[0].factory_kwargs["level_provider"]() == "light"
    assert len(agents) == 1
    assert loaded.settings.model_dump() == before
    assert all(
        item.get("method") != "config_option_update" for item in writer.sent
    )
    await facade.shutdown()


async def test_same_alias_does_not_rewrite_level(tmp_path):
    loaded = _loaded(
        tmp_path,
        {
            **_models(default="flash", light="flash", max="opus"),
            "level": "light",
        },
    )
    facade, writer, agents = _open(loaded)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    assert _option(_by_id(writer, 2)["result"])["currentValue"] == "flash"
    await _set(facade, session_id, "flash")
    assert facade._sessions[session_id].level is None
    assert agents[0].factory_kwargs["level_provider"]() == "light"
    await facade.shutdown()


async def test_first_level_wins_when_alias_repeats(tmp_path):
    loaded = _loaded(
        tmp_path,
        {
            **_models(default="flash", light="flash", max="opus"),
            "level": "max",
        },
    )
    facade, writer, agents = _open(loaded)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    assert _option(_by_id(writer, 2)["result"])["currentValue"] == "opus"
    await _set(facade, session_id, "flash")
    assert facade._sessions[session_id].level == "default"
    assert agents[0].factory_kwargs["level_provider"]() == "default"
    await facade.shutdown()


async def test_foreign_value_and_boolean_type_are_invalid(tmp_path):
    facade, writer, agents = _open(_loaded(tmp_path, _models()))
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await _set(facade, session_id, "judge", 3)
    assert _by_id(writer, 3)["error"]["code"] == -32602
    await _set(facade, session_id, "flash", 4, type="boolean")
    assert _by_id(writer, 4)["error"]["code"] == -32602
    await _set(facade, "missing", "flash", 5)
    assert _by_id(writer, 5)["error"]["code"] == -32602
    await facade.handle_line(
        _line(
            "session/set_config_option",
            {"sessionId": session_id, "value": "flash"},
            6,
        )
    )
    assert _by_id(writer, 6)["error"]["code"] == -32602
    assert facade._sessions[session_id].level is None
    assert agents[0].factory_kwargs["level_provider"]() == "default"
    await _set(facade, session_id, "opus", 7, type="select")
    assert _option(_by_id(writer, 7)["result"])["currentValue"] == "opus"
    await facade.shutdown()


async def test_second_session_keeps_its_level(tmp_path):
    facade, writer, agents = _open(_loaded(tmp_path, _models()))
    await _init(facade)
    await _new(facade, "/work", 2)
    await _new(facade, "/work", 3)
    first = _by_id(writer, 2)["result"]["sessionId"]
    second = _by_id(writer, 3)["result"]["sessionId"]
    await _set(facade, first, "flash")
    assert agents[0].factory_kwargs["level_provider"]() == "light"
    assert agents[1].factory_kwargs["level_provider"]() == "default"
    await facade.handle_line(
        _line(
            "session/resume",
            {"sessionId": second, "cwd": "/work"},
            8,
        )
    )
    assert _option(_by_id(writer, 8)["result"])["currentValue"] == "glm-local"
    await facade.shutdown()


async def test_close_forgets_the_choice(tmp_path):
    facade, writer, agents = _open(_loaded(tmp_path, _models()))
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await _set(facade, session_id, "flash")
    await facade.handle_line(
        _line("session/close", {"sessionId": session_id}, 4)
    )
    await facade.handle_line(
        _line(
            "session/load",
            {"sessionId": session_id, "cwd": "/work", "mcpServers": []},
            5,
        )
    )
    assert _option(_by_id(writer, 5)["result"])["currentValue"] == "glm-local"
    assert agents[-1].factory_kwargs["level_provider"]() == "default"
    await facade.shutdown()


class _TurningAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.router = None
        self.pinned = []

    async def stream(self, prompt, **kwargs):
        state = SimpleNamespace(parent_state_id=None)
        begin_turn(state, SimpleNamespace(model_router=self.router))
        self.pinned.append(state)
        async for item in super().stream(prompt, **kwargs):
            yield item


async def test_mid_turn_switch_applies_from_the_next_request(tmp_path):
    agents = []

    async def factory(config, **kwargs):
        agent = _TurningAgent()
        agent.factory_kwargs = kwargs
        agent.router = ModelRouter(
            config,
            level_provider=kwargs["level_provider"],
            force_legacy=cli_forces_legacy_model(config),
        )
        agents.append(agent)
        return agent

    facade, writer, _ignored = _open(
        _loaded(tmp_path, _models()), factory=factory
    )
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agent = agents[0]
    agent.hold = True
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "go"}],
            },
            3,
        )
    )
    await agent.started.wait()
    running = agent.pinned[0]
    assert running.default_model == "glm-local"
    await _set(facade, session_id, "flash", 4)
    assert _option(_by_id(writer, 4)["result"])["currentValue"] == "flash"
    ctx = SimpleNamespace(model_router=agent.router)
    assert resolve_main_for_call(running, ctx).alias == "flash"
    assert running.default_model == "qwen/qwen-2.5-flash"
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    agent.hold = False
    agent.started = asyncio.Event()
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "next"}],
            },
            5,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert agent.pinned[1].default_model == "qwen/qwen-2.5-flash"
    assert len(agents) == 1
    await facade.shutdown()


async def test_empty_registry_is_one_row_and_does_not_write_level(tmp_path):
    loaded = _loaded(tmp_path, {"agent.model": "solo"})
    facade, writer, agents = _open(loaded)
    await _init(facade)
    await _new(facade, "/work")
    option = _option(_by_id(writer, 2)["result"])
    assert option["currentValue"] == "solo"
    assert option["options"] == [{"value": "solo", "name": "solo"}]
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await _set(facade, session_id, "flash", 3)
    assert _by_id(writer, 3)["error"]["code"] == -32602
    await _set(facade, session_id, "solo", 4)
    assert _option(_by_id(writer, 4)["result"])["currentValue"] == "solo"
    assert facade._sessions[session_id].level is None
    assert agents[0].factory_kwargs["level_provider"]() == "default"
    await facade.shutdown()


async def test_legacy_force_rejects_registry_alias(tmp_path):
    loaded = _loaded(tmp_path, _models(), cli_model="forced")
    assert cli_forces_legacy_model(loaded) is True
    facade, writer, agents = _open(loaded)
    await _init(facade)
    await _new(facade, "/work")
    option = _option(_by_id(writer, 2)["result"])
    assert option["options"] == [{"value": "forced", "name": "forced"}]
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await _set(facade, session_id, "flash", 3)
    assert _by_id(writer, 3)["error"]["code"] == -32602
    await _set(facade, session_id, "forced", 4)
    assert facade._sessions[session_id].level is None
    assert agents[0].factory_kwargs["level_provider"]() == "default"
    await facade.shutdown()


async def test_empty_model_and_unresolved_level_omit_the_option(tmp_path):
    empty, writer, _agents = _open(_loaded(tmp_path, {"agent.model": ""}))
    await _init(empty)
    await _new(empty, "/work")
    assert [o["id"] for o in _by_id(writer, 2)["result"]["configOptions"]] == [
        "mode"
    ]
    await empty.shutdown()

    unresolved = _open(
        _loaded(tmp_path, {"models": {"a": {}, "b": {}}, "level": "default"})
    )
    facade, writer2, _agents2 = unresolved
    await _init(facade)
    await _new(facade, "/work")
    created = _by_id(writer2, 2)["result"]
    assert set(created) == {"sessionId", "configOptions"}
    assert [o["id"] for o in created["configOptions"]] == ["mode"]
    await _set(facade, created["sessionId"], "a", 3)
    assert _by_id(writer2, 3)["error"]["code"] == -32602
    await facade.shutdown()


def test_open_breaker_does_not_hide_the_alias(tmp_path):
    loaded = _loaded(tmp_path, _models())
    breaker = CircuitBreaker()
    breaker.trip("flash", "down")
    option = model_config_option(loaded, "default", breaker=breaker)
    assert "flash" in _values(option)


def test_unresolved_level_is_skipped(tmp_path):
    loaded = _loaded(
        tmp_path,
        {
            "models": {"a": {"model": "a"}, "b": {"model": "prov/b"}},
            "slots": {"default": "a", "light": "ghost", "max": "b"},
        },
    )
    option = model_config_option(loaded, "default")
    assert _values(option) == ["a", "b"]
    assert option["options"][1]["description"] == "prov/b"
    hidden = _loaded(
        tmp_path,
        {
            "level": "light",
            "models": {"a": {}, "b": {}},
            "slots": {"default": "a", "light": "ghost"},
        },
    )
    assert model_config_option(hidden, "light") is None


def _thoughts(writer):
    return [
        item["content"]["text"]
        for item in _updates(writer)
        if item.get("sessionUpdate") == "agent_thought_chunk"
    ]


def test_active_model_line_names_the_turn_model(tmp_path):
    loaded = _loaded(tmp_path, _models())
    assert active_model_line(loaded, "default") == "Модель: glm-local"
    assert (
        active_model_line(loaded, "light")
        == "Модель: flash (qwen/qwen-2.5-flash)"
    )
    legacy = _loaded(tmp_path)
    assert (
        active_model_line(legacy, "default")
        == f"Модель: {legacy.settings.agent.model}"
    )
    forced = _loaded(tmp_path, _models(), cli_model="forced-id")
    assert active_model_line(forced, "light") == "Модель: forced-id"
    blank = _loaded(tmp_path, {"agent.model": ""})
    assert active_model_line(blank, "default") is None
    broken = _loaded(
        tmp_path,
        {
            "models": {"a": {"model": "a"}, "b": {"model": "b"}},
            "slots": {},
        },
    )
    assert active_model_line(broken, "default") is None


async def test_each_turn_names_the_model_in_the_trace(tmp_path):
    facade, writer, agents = _open(_loaded(tmp_path, _models()))
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "go"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert _thoughts(writer) == ["Модель: glm-local\n"]
    await _set(facade, session_id, "flash", 4)
    agents[0].started = asyncio.Event()
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "ещё"}],
            },
            5,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert _thoughts(writer) == ["Модель: flash (qwen/qwen-2.5-flash)\n"]
    await facade.shutdown()


async def test_prompt_skips_the_trace_line_without_a_model_name(tmp_path):
    facade, writer, _agents = _open(_loaded(tmp_path, {"agent.model": ""}))
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "go"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert _thoughts(writer) == []
    assert _by_id(writer, 3)["result"]["stopReason"] == "end_turn"
    await facade.shutdown()


_CHAT = {"backend.api_mode": "chat_completions"}


def _auto_models(**extra):
    return {**_models(), **_CHAT, **extra}


async def _session(facade, writer):
    await _init(facade)
    await _new(facade, "/work")
    created = _by_id(writer, 2)["result"]
    return created["sessionId"], _option(created)


async def test_picker_offers_auto_when_available(tmp_path):
    facade, writer, _agents = _open(_loaded(tmp_path, _auto_models()))
    _session_id, option = await _session(facade, writer)
    assert _values(option) == ["glm-local", "flash", "opus", "auto"]
    assert option["options"][-1] == {"value": "auto", "name": "auto"}
    assert option["currentValue"] == "glm-local"
    await facade.shutdown()


async def test_auto_level_is_current_and_named_in_the_trace(tmp_path):
    loaded = _loaded(tmp_path, _auto_models(level="auto"))
    facade, writer, agents = _open(loaded)
    session_id, option = await _session(facade, writer)
    assert option["currentValue"] == "auto"
    assert agents[0].factory_kwargs["level_provider"]() == "auto"
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "go"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert _thoughts(writer) == ["Модель: auto\n"]
    await facade.shutdown()


async def test_fixed_model_from_auto_leaves_auto(tmp_path):
    loaded = _loaded(tmp_path, _auto_models(level="auto"))
    facade, writer, agents = _open(loaded)
    session_id, _first = await _session(facade, writer)
    await _set(facade, session_id, "flash", 3)
    option = _option(_by_id(writer, 3)["result"])
    assert option["currentValue"] == "flash"
    assert "auto" in _values(option)
    assert facade._sessions[session_id].level == "light"
    await _set(facade, session_id, "auto", 4)
    await _set(facade, session_id, "glm-local", 5)
    assert _option(_by_id(writer, 5)["result"])["currentValue"] == "glm-local"
    assert facade._sessions[session_id].level == "default"
    assert agents[0].factory_kwargs["level_provider"]() == "default"
    await facade.shutdown()


async def test_choosing_auto_sets_the_auto_level(tmp_path):
    facade, writer, agents = _open(_loaded(tmp_path, _auto_models()))
    session_id, _first = await _session(facade, writer)
    await _set(facade, session_id, "auto", 3)
    option = _option(_by_id(writer, 3)["result"])
    assert option["currentValue"] == "auto"
    assert facade._sessions[session_id].level == "auto"
    assert agents[0].factory_kwargs["level_provider"]() == "auto"
    await facade.shutdown()


async def test_auto_is_not_offered_when_unavailable(tmp_path):
    responses = _loaded(tmp_path, _models())
    same = _loaded(
        tmp_path,
        {
            **_CHAT,
            "models": {a: {"model": "same"} for a in ("a", "b", "c")},
            "slots": {"default": "a", "light": "b", "max": "c"},
        },
    )
    for loaded in (responses, same):
        facade, writer, _agents = _open(loaded)
        session_id, option = await _session(facade, writer)
        assert "auto" not in _values(option)
        await _set(facade, session_id, "auto", 3)
        assert _by_id(writer, 3)["error"]["code"] == -32602
        assert facade._sessions[session_id].level is None
        await facade.shutdown()


def test_unavailable_auto_shows_the_level_that_runs(tmp_path):
    loaded = _loaded(tmp_path, {**_models(), "level": "auto"})
    option = model_config_option(loaded, "auto")
    assert option["currentValue"] == "glm-local"
    assert "auto" not in _values(option)
    assert active_model_line(loaded, "auto") == "Модель: glm-local"


def test_active_model_line_in_auto_names_the_mode(tmp_path):
    loaded = _loaded(tmp_path, _auto_models())
    assert active_model_line(loaded, "auto") == "Модель: auto"
    assert active_model_line(loaded, "default") == "Модель: glm-local"
