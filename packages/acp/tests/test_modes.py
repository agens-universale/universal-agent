"""Режимы разрешений сессии ACP: опция mode, modes, смена и уведомления."""

import pytest
from gc_acp.modes import (
    mode_choice,
    mode_config_option,
    mode_updates,
    session_modes,
)
from gc_core.thread_store import save_thread_meta
from gc_testkit.develop_writer import append_new_thread_messages
from test_facade import _by_id, _init, _line, _new, _open, _updates

_LABELS = {
    "edit": "edit mode",
    "manual": "manual mode",
    "plan": "plan mode",
    "auto": "auto mode",
}


def _mode_option(result):
    options = {item["id"]: item for item in result["configOptions"]}
    return options["mode"]


async def _session(facade, writer, request_id=2):
    await _init(facade)
    await _new(facade, "/work/app", request_id)
    return _by_id(writer, request_id)["result"]["sessionId"]


def test_option_and_modes_list_every_mode_in_cycle_order():
    option = mode_config_option("plan")
    assert option == {
        "id": "mode",
        "name": "Mode",
        "category": "mode",
        "type": "select",
        "currentValue": "plan",
        "options": [
            {"value": value, "name": name} for value, name in _LABELS.items()
        ],
    }
    assert session_modes("plan") == {
        "currentModeId": "plan",
        "availableModes": [
            {"id": value, "name": name} for value, name in _LABELS.items()
        ],
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("auto", "auto"),
        ("plan", "plan"),
        ("Plan", None),
        (" edit", None),
        ("bypass", None),
        (None, None),
        (1, None),
    ],
)
def test_protocol_value_must_be_an_exact_mode_id(value, expected):
    assert mode_choice(value) == expected


def test_mode_change_is_announced_in_both_views():
    options = [mode_config_option("auto")]
    assert mode_updates("auto", options) == [
        {"sessionUpdate": "current_mode_update", "currentModeId": "auto"},
        {"sessionUpdate": "config_option_update", "configOptions": options},
    ]


async def test_new_session_offers_mode_next_to_model(tmp_path):
    facade, writer, _agents = _open(tmp_path)
    await _session(facade, writer)
    created = _by_id(writer, 2)["result"]
    assert [item["id"] for item in created["configOptions"]] == [
        "model",
        "mode",
    ]
    assert _mode_option(created)["currentValue"] == "edit"
    assert created["modes"]["currentModeId"] == "edit"
    await facade.shutdown()


async def test_config_default_mode_is_the_current_one(tmp_path):
    facade, writer, _agents = _open(
        tmp_path,
        settings_extra={"security.permissions.default_mode": "manual"},
    )
    await _session(facade, writer)
    created = _by_id(writer, 2)["result"]
    assert _mode_option(created)["currentValue"] == "manual"
    assert created["modes"]["currentModeId"] == "manual"
    await facade.shutdown()


async def test_set_config_option_switches_the_executor_and_notifies(
    tmp_path,
):
    facade, writer, agents = _open(tmp_path)
    session_id = await _session(facade, writer)
    await facade.handle_line(
        _line(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "mode", "value": "plan"},
            3,
        )
    )
    result = _by_id(writer, 3)["result"]
    assert [item["id"] for item in result["configOptions"]] == [
        "model",
        "mode",
    ]
    assert _mode_option(result)["currentValue"] == "plan"
    assert agents[0].executor.modes == ["plan"]
    updates = _updates(writer)
    assert {
        "sessionUpdate": "current_mode_update",
        "currentModeId": "plan",
    } in updates
    option_update = next(
        item
        for item in updates
        if item["sessionUpdate"] == "config_option_update"
    )
    assert option_update["configOptions"] == result["configOptions"]
    await facade.shutdown()


async def test_set_mode_switches_the_executor_and_notifies(tmp_path):
    facade, writer, agents = _open(tmp_path)
    session_id = await _session(facade, writer)
    await facade.handle_line(
        _line(
            "session/set_mode",
            {"sessionId": session_id, "modeId": "auto"},
            3,
        )
    )
    assert _by_id(writer, 3)["result"] == {}
    assert agents[0].executor.modes == ["auto"]
    updates = _updates(writer)
    assert {
        "sessionUpdate": "current_mode_update",
        "currentModeId": "auto",
    } in updates
    option_update = next(
        item
        for item in updates
        if item["sessionUpdate"] == "config_option_update"
    )
    assert _mode_option(option_update)["currentValue"] == "auto"
    await facade.shutdown()


async def test_foreign_mode_values_are_invalid_params(tmp_path):
    facade, writer, agents = _open(tmp_path)
    session_id = await _session(facade, writer)
    requests = [
        ("session/set_mode", {"sessionId": session_id, "modeId": "bypass"}),
        ("session/set_mode", {"sessionId": "missing", "modeId": "auto"}),
        ("session/set_mode", {"sessionId": session_id}),
        (
            "session/set_config_option",
            {"sessionId": session_id, "configId": "mode", "value": "Plan"},
        ),
        (
            "session/set_config_option",
            {"sessionId": session_id, "configId": "effort", "value": "x"},
        ),
    ]
    for request_id, (method, params) in enumerate(requests, start=3):
        await facade.handle_line(_line(method, params, request_id))
        assert _by_id(writer, request_id)["error"]["code"] == -32602
    assert agents[0].executor.modes == []
    await facade.shutdown()


async def test_load_and_resume_take_the_mode_saved_in_the_session(tmp_path):
    """До первого прогона исполнитель режима сессии не знает — он в мете."""
    facade, writer, _agents = _open(tmp_path)
    session_id = await _session(facade, writer)
    await facade.shutdown()
    append_new_thread_messages(
        str(tmp_path),
        "acp",
        session_id,
        [{"role": "user", "content": "привет"}],
        set(),
    )
    save_thread_meta(
        str(tmp_path),
        "acp",
        session_id,
        {"shared": {"permission_mode": "plan"}},
    )

    restored, writer2, agents2 = _open(tmp_path)
    await _init(restored)
    await restored.handle_line(
        _line(
            "session/load",
            {"sessionId": session_id, "cwd": "/work/app", "mcpServers": []},
            3,
        )
    )
    loaded = _by_id(writer2, 3)["result"]
    assert loaded["modes"]["currentModeId"] == "plan"
    assert _mode_option(loaded)["currentValue"] == "plan"
    assert agents2[0].executor.modes == []  # прочитали, а не выбрали
    await restored.handle_line(
        _line(
            "session/resume",
            {"sessionId": session_id, "cwd": "/work/app"},
            4,
        )
    )
    assert _by_id(writer2, 4)["result"]["modes"]["currentModeId"] == "plan"
    await restored.shutdown()


async def _manual_write_in_the_ide(tmp_path, *, answer, settings_extra=None):
    """Сессия ACP в manual, выбранном из IDE; запись в проект спрашивает в
    IDE. ``answer`` — optionId ответа клиента или None (клиент молчит).
    Возвращает (ответ тула, запрос в IDE, писатель)."""
    import asyncio

    from gc_acp.main import apply_acp_runtime_defaults
    from gc_core.execution.state import SharedStateData
    from gc_core.security.call_scope import call_scope
    from gc_core.security.path_policy import enforce_write_guard
    from gc_core.security.permission_store import PermissionStore
    from gc_testkit.settings import fake_ctx
    from test_facade import FakeAgent
    from test_permission import (
        _permission_calls,
        _response,
        _selected,
        _wait_permission,
    )

    @enforce_write_guard(path_arg="path")
    def write(path: str, ctx=None) -> str:
        return "written"

    shared = SharedStateData()

    async def factory(config, **kwargs):
        agent = FakeAgent()
        agent.executor.shared = shared
        # Настройки — как их отдаёт `gc_acp.main`: с объявлением канала.
        agent.executor._ctx = fake_ctx(
            loaded=apply_acp_runtime_defaults(config),
            permission_store=PermissionStore(
                shared.confirm_rules, shared=shared
            ),
        )
        return agent

    project = tmp_path / "proj"
    project.mkdir()
    facade, writer, _agents = _open(
        tmp_path, factory=factory, settings_extra=settings_extra
    )
    await _init(facade)
    await _new(facade, str(project))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await facade.handle_line(
        _line(
            "session/set_mode",
            {"sessionId": session_id, "modeId": "manual"},
            3,
        )
    )
    assert _by_id(writer, 3)["result"] == {}
    ctx = facade._sessions[session_id].agent.executor._ctx

    async def reply():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _response(msg["id"], _selected(answer, kind=answer))
        )

    def dispatched() -> str:
        # Привязку вызова ставит диспетчер тулов; по ней текст отказа
        # отличает «не ответил вовремя» от «отказал».
        with call_scope(state=None, call_id="c1", tool_name="fs_write_file"):
            return write(path=str(project / "a.txt"), ctx=ctx)

    answering = asyncio.create_task(reply()) if answer else None
    out = await asyncio.to_thread(dispatched)
    if answering is not None:
        await answering
    msg = _permission_calls(writer)[-1]
    assert not (project / "a.txt").exists()
    await facade.shutdown()
    return out, msg, writer


async def test_manual_from_the_ide_reject_is_declined(tmp_path):
    """Режим выбран в IDE, запись спрашивает там же, «Reject» — это отказ
    человека, а не «спросить некого»: ACP объявляет, что человек есть."""
    import json

    out, msg, _writer = await _manual_write_in_the_ide(
        tmp_path, answer="reject_once"
    )
    assert msg["params"]["toolCall"]["kind"] == "edit"
    assert json.loads(out)["error"]["type"] == "declined_by_user"


async def test_manual_from_the_ide_without_an_answer_is_not_answered_in_time(
    tmp_path,
):
    """Review Focus №1: человек есть, но ответа нет (клиент не умеет
    отвечать или молчит) — «не ответил вовремя», а не «отказал»; диалог в
    IDE закрывается `$/cancel_request` на этот самый запрос."""
    import json

    out, msg, writer = await _manual_write_in_the_ide(
        tmp_path,
        answer=None,
        settings_extra={"acp.confirm_timeout_sec": 0.05},
    )
    assert json.loads(out)["error"]["type"] == "confirmation_timed_out"
    assert {
        "jsonrpc": "2.0",
        "method": "$/cancel_request",
        "params": {"requestId": msg["id"]},
    } in writer.sent
