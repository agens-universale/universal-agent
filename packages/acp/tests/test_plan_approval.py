"""Одобрение плана в ACP: свой session/request_permission после хода планом."""

import json

import pytest
from gc_acp.plan_approval import (
    PLAN_REWORK,
    REWORK_HINT,
    plan_choice_from_incoming,
)
from gc_acp.protocol import Incoming
from gc_core.message_policy.plan_messages import accepted_plan_messages
from test_facade import _by_id, _init, _line, _new, _open, _updates
from test_permission import (
    _permission_calls,
    _response,
    _selected,
    _wait_permission,
)

_PLAN = "1. Добавить тест\n2. Починить"


async def _ready(tmp_path, *, status="proposed", **settings):
    facade, writer, agents = _open(tmp_path, settings_extra=settings)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agent = agents[0]
    agent.deltas = []
    agent.answer = json.dumps(
        {"plan": _PLAN, "status": status}, ensure_ascii=False
    )
    agent.outcome = "plan_proposed"
    agent.executor.plan = {"text": _PLAN, "status": status}
    return facade, writer, agent, session_id


async def _prompt(facade, session_id):
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "спланируй"}],
            },
            3,
        )
    )


def _answers(writer):
    return [
        item["content"]["text"]
        for item in _updates(writer)
        if item["sessionUpdate"] == "agent_message_chunk"
    ]


def _text(update):
    """Текст чанка; у карточки тула content — список, текста нет."""
    content = update.get("content")
    return content.get("text") if isinstance(content, dict) else None


def _is_plan_text(item):
    return (
        item.get("method") == "session/update"
        and _text(item["params"]["update"]) == _PLAN
    )


def _card_closed(writer, card_id, status):
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": card_id,
        "status": status,
    } in _updates(writer)


async def test_question_is_its_own_permission_after_the_plan_text(tmp_path):
    facade, writer, agent, session_id = await _ready(tmp_path)
    await _prompt(facade, session_id)
    msg = await _wait_permission(writer)

    params = msg["params"]
    tool = params["toolCall"]
    assert tool["toolCallId"].startswith("plan_approval:")
    assert tool["kind"] == "switch_mode"
    assert tool["status"] == "pending"
    assert [item["optionId"] for item in params["options"]] == [
        "edit",
        "auto",
        "manual",
        "rework",
    ]
    assert [item["kind"] for item in params["options"]] == [
        "allow_once",
        "allow_once",
        "allow_once",
        "reject_once",
    ]
    # План показан один раз — текстом ответа, до вопроса; в вопросе его нет.
    assert "Добавить тест" not in json.dumps(params, ensure_ascii=False)
    plan_at = next(
        i for i, item in enumerate(writer.sent) if _is_plan_text(item)
    )
    assert plan_at < writer.sent.index(msg)

    await facade.handle_line(
        _response(msg["id"], {"outcome": {"outcome": "cancelled"}})
    )
    await facade._sessions[session_id].prompt_task
    assert agent.executor.accepted == []
    assert agent.executor.modes == []
    assert _card_closed(writer, tool["toolCallId"], "failed")
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


@pytest.mark.parametrize("mode", ["edit", "auto", "manual"])
async def test_execution_mode_runs_the_plan_in_the_same_prompt(tmp_path, mode):
    facade, writer, agent, session_id = await _ready(tmp_path)
    agent.next_runs = [([("answer", "Готово.")], "Готово.", None)]
    await _prompt(facade, session_id)
    msg = await _wait_permission(writer)
    await facade.handle_line(_response(msg["id"], _selected(mode)))
    await facade._sessions[session_id].prompt_task

    assert agent.executor.accepted == [(mode, session_id, "")]
    # Продолжение — обычный прогон с сообщениями последовательности ядра.
    assert agent.calls[-1] == (
        accepted_plan_messages(mode),
        {"thread_id": session_id, "system": "acp"},
    )
    card_id = msg["params"]["toolCall"]["toolCallId"]
    assert _card_closed(writer, card_id, "completed")
    assert _answers(writer) == [_PLAN, "Готово."]
    updates = _updates(writer)
    assert all(item["sessionUpdate"] != "plan" for item in updates)
    mode_at = updates.index(
        {"sessionUpdate": "current_mode_update", "currentModeId": mode}
    )
    option_update = next(
        item
        for item in updates
        if item["sessionUpdate"] == "config_option_update"
    )
    mode_option = next(
        item for item in option_update["configOptions"] if item["id"] == "mode"
    )
    assert mode_option["currentValue"] == mode
    done_at = next(
        i for i, item in enumerate(updates) if _text(item) == "Готово."
    )
    assert mode_at < done_at  # IDE узнаёт режим до работы продолжения
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_rework_keeps_plan_mode_and_waits_for_remarks(tmp_path):
    facade, writer, agent, session_id = await _ready(tmp_path)
    await _prompt(facade, session_id)
    msg = await _wait_permission(writer)
    await facade.handle_line(
        _response(msg["id"], _selected(PLAN_REWORK, kind="reject_once"))
    )
    await facade._sessions[session_id].prompt_task

    assert agent.executor.accepted == []
    assert agent.executor.reworked == [session_id]
    assert agent.executor.modes == ["plan"]
    card_id = msg["params"]["toolCall"]["toolCallId"]
    assert _card_closed(writer, card_id, "completed")
    assert {
        "sessionUpdate": "current_mode_update",
        "currentModeId": "plan",
    } in _updates(writer)
    assert _answers(writer) == [_PLAN, REWORK_HINT]
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_cancel_while_asking_runs_nothing(tmp_path):
    facade, writer, agent, session_id = await _ready(tmp_path)
    await _prompt(facade, session_id)
    msg = await _wait_permission(writer)
    await facade.handle_line(
        _line("session/cancel", {"sessionId": session_id}, notify=True)
    )
    await facade._sessions[session_id].prompt_task

    assert agent.executor.accepted == []
    assert _by_id(writer, 3)["result"] == {"stopReason": "cancelled"}
    assert {
        "jsonrpc": "2.0",
        "method": "$/cancel_request",
        "params": {"requestId": msg["id"]},
    } in writer.sent
    await facade.shutdown()


async def test_silent_client_fails_the_card_and_runs_nothing(tmp_path):
    """Клиент не ответил за таймаут: карточка сорвана, пикер закрыт
    ``$/cancel_request``, режим не выбран, ничего не исполнено."""
    facade, writer, agent, session_id = await _ready(
        tmp_path, **{"acp.confirm_timeout_sec": 0.05}
    )
    await _prompt(facade, session_id)
    await facade._sessions[session_id].prompt_task

    msg = _permission_calls(writer)[0]
    card_id = msg["params"]["toolCall"]["toolCallId"]
    assert _card_closed(writer, card_id, "failed")
    assert {
        "jsonrpc": "2.0",
        "method": "$/cancel_request",
        "params": {"requestId": msg["id"]},
    } in writer.sent
    assert agent.executor.accepted == []
    assert agent.executor.reworked == []
    assert agent.executor.modes == []
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_plan_that_is_not_waiting_is_shown_without_a_question(
    tmp_path,
):
    """Повторный показ утверждённого плана: решать нечего — как в CLI."""
    facade, writer, agent, session_id = await _ready(
        tmp_path, status="approved"
    )
    await _prompt(facade, session_id)
    await facade._sessions[session_id].prompt_task

    assert _permission_calls(writer) == []
    assert _answers(writer) == [_PLAN]
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


def _selected_reply(option_id):
    return Incoming(
        has_result=True,
        result={"outcome": {"outcome": "selected", "optionId": option_id}},
    )


@pytest.mark.parametrize(
    ("incoming", "expected"),
    [
        (_selected_reply("auto"), "auto"),
        (_selected_reply("edit"), "edit"),
        (_selected_reply("rework"), PLAN_REWORK),
        # Варианты подтверждения гварда — не ответ на вопрос о плане.
        (_selected_reply("allow_once"), None),
        (
            Incoming(
                has_result=True, result={"outcome": {"outcome": "cancelled"}}
            ),
            None,
        ),
        (
            Incoming(has_error=True, error={"code": -32603, "message": "x"}),
            None,
        ),
        (Incoming(has_result=True, result="garbage"), None),
    ],
)
def test_reply_is_parsed_by_its_own_options(incoming, expected):
    assert plan_choice_from_incoming(incoming) == expected
