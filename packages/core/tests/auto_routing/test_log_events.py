"""События лога автовыбора пишутся в переданный логгер — презентер ctx не читает."""

from types import SimpleNamespace

import pytest

from gc_core.auto_routing.log_events import (
    log_artifact_reroute,
    log_routing_decision,
)
from gc_core.auto_routing.policy import (
    Decision,
    REROUTE_IMPORTANT,
    REROUTE_INVALID_CALL,
    REROUTE_NO_TOOL_CALL,
    RoutingParams,
)


class _Logger:
    def __init__(self):
        self.events = []
        self.messages = []

    def info(self, msg, *args, **kwargs):
        self.events.append(kwargs.get("extra", {}))
        self.messages.append(msg % args if args else msg)


@pytest.mark.parametrize("active", [True, False])
def test_routing_decision_reports_the_current_call(active):
    logger = _Logger()
    state = SimpleNamespace(
        last_routing_reason="hold",
        steps_in_turn=4,
        steps_on_current_level=2,
        default_model="mid-1",
    )
    log_routing_decision(
        logger,
        state,
        active=active,
        decision=Decision("default", "hold", (), False),
        level="default",
        resolved=SimpleNamespace(alias="mid", model="mid-1"),
        params=RoutingParams(),
    )
    reason = "hold" if active else "manual"
    expected = {
        "event": "routing_decision",
        "mode": "auto" if active else "manual",
        "routing_level": "default",
        "model": "mid",
        "reason": reason,
        "steps_in_turn": 4,
        "steps_on_current_level": 2,
    }
    if active:
        expected.update(policy="two_level", heuristics=[])
    assert logger.events == [expected]
    assert logger.messages == [
        f"routing_decision level=default reason={reason} model=mid",
    ]


def test_routing_decision_carries_the_fallback_only_when_there_was_one():
    logger = _Logger()
    state = SimpleNamespace(
        last_routing_reason="hold",
        steps_in_turn=4,
        steps_on_current_level=4,
        default_model="m",
    )
    resolved = SimpleNamespace(alias="mid", model="mid-1")
    replaced = Decision("light", "hold", (), False, fallback_from="light")
    log_routing_decision(
        logger,
        state,
        active=True,
        decision=replaced,
        level="default",
        resolved=resolved,
        params=RoutingParams(),
    )
    log_routing_decision(
        logger,
        state,
        active=True,
        decision=Decision("light", "hold", (), True),
        level="light",
        resolved=resolved,
        params=RoutingParams(),
    )
    assert (
        logger.events[0]["fallback_from"] == "light"
        and logger.events[0]["routing_level"] == "default"
    )
    assert "fallback_from" not in logger.events[1]


def test_artifact_reroute_keeps_its_name_and_carries_the_reroute_codes():
    """Имя события прежнее — его читают разборщики стенда; `triggers` несёт коды
    поводов, `tools` — важные тулы ответа (у повода без тула список пуст)."""
    logger = _Logger()
    state = SimpleNamespace(usage=None)
    log_artifact_reroute(
        logger,
        state,
        from_level="light",
        to_level="default",
        tools=("fs_write_file", "turn_finish"),
        triggers=(REROUTE_IMPORTANT, REROUTE_INVALID_CALL),
        usage_before=None,
        discarded_calls=[],
        discarded_text="",
    )
    log_artifact_reroute(
        logger,
        state,
        from_level="light",
        to_level="default",
        tools=(),
        triggers=(REROUTE_NO_TOOL_CALL,),
        usage_before=None,
        discarded_calls=[],
        discarded_text="",
    )
    assert logger.events == [
        {
            "event": "artifact_reroute",
            "triggers": ["important", "invalid_call"],
            "from_level": "light",
            "to_level": "default",
            "tools": ["fs_write_file", "turn_finish"],
        },
        {
            "event": "artifact_reroute",
            "triggers": ["no_tool_call"],
            "from_level": "light",
            "to_level": "default",
            "tools": [],
        },
    ]


def test_artifact_reroute_carries_the_discarded_calls_and_prose_only_when_there_are_any():
    """Выброшенный ответ слабой — материал сравнения с тем, что сделала сильная:
    вызовы с аргументами как есть и проза. Пустые поля не пишутся."""
    logger = _Logger()
    state = SimpleNamespace(usage=None)
    calls = [
        {
            "name": "fs_write_file",
            "arguments": '{"path": "a.py", "content": "x = 1"}',
        }
    ]
    log_artifact_reroute(
        logger,
        state,
        from_level="light",
        to_level="default",
        tools=("fs_write_file",),
        triggers=(REROUTE_IMPORTANT,),
        usage_before=None,
        discarded_calls=calls,
        discarded_text="Правлю файл.",
    )
    log_artifact_reroute(
        logger,
        state,
        from_level="light",
        to_level="default",
        tools=(),
        triggers=(REROUTE_NO_TOOL_CALL,),
        usage_before=None,
        discarded_calls=[],
        discarded_text="",
    )
    assert logger.events[0]["discarded_calls"] == calls
    assert logger.events[0]["discarded_text"] == "Правлю файл."
    assert (
        "discarded_calls" not in logger.events[1]
        and "discarded_text" not in logger.events[1]
    )
