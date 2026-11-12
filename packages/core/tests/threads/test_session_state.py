"""Перевод State ↔ SessionState совпадает с прежним путём через meta develop
(оракул — ``legacy_session_oracle``): гидрация хода и состояние после шага
на тредах develop и синтетических случаях."""

import copy
import json
from dataclasses import asdict

import pytest
from gc_core.execution.state import ExecutionState, SharedStateData
from gc_core.intent_log.serialize import deserialize_list, serialize_for_meta
from gc_core.persistence import session_state as conv
from gc_core.persistence.schema import StateSchemaError
from gc_core.threads.meta_map import meta_from_record, parts_from_meta
from gc_core.threads.models import ThreadKey, ThreadRecord
from gc_core.usage.account import UsageAccount
from gc_testkit.paths import REPO

import legacy_session_oracle as old

FIXTURES = REPO / "tests/fixtures/legacy_threads/root/threads"


def _fixtures() -> list[tuple[str, dict]]:
    return [
        (p.stem, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(FIXTURES.glob("meta_*.json"))
        if "__pre_summary__" not in p.name
    ]


SYNTHETIC = [
    ("пустая", {}),
    (
        "неизвестный ключ shared",
        {
            "shared": {"working_memory": {"goal": "цель"}, "new_key": 1},
            "state_schema_version": "v2",
        },
    ),
    ("inbox", {"inbox_seq": 3, "user_turn_seq": 2}),
    (
        "промпт и responses",
        {
            "chat_completions_system_prompt_rendered": "SYS",
            "backend_api_mode": "chat_completions",
            "last_backend_response_id": "r0",
        },
    ),
    (
        "битые типы",
        {"user_turn_seq": "три", "last_main_prompt_tokens": -5},
    ),
    (
        "счётчики напоминаний",
        {
            "tool_usage_notice_cnt": {"session_memory": 1},
            "todo_reminder_cnt": 2,
            "project_memory_check_cnt": 5,
            "project_memory_content_hash": "h1",
        },
    ),
    ("старый счётчик памяти", {"memory_notice_cnt": 4}),
]
CORPUS = _fixtures() + SYNTHETIC


def _view(meta: dict) -> dict:
    """Meta, которую прежний путь видел через порт (meta_from_record)."""
    parts = parts_from_meta(meta)
    record = ThreadRecord(
        key=ThreadKey(system="cli", thread="t"),
        info=parts.info,
        state=parts.state,
        usage=parts.usage,
        runtime=parts.runtime,
        frontend=parts.frontend,
        extra=parts.extra,
    )
    return meta_from_record(record)


def _state() -> ExecutionState:
    state = ExecutionState()
    for name, value in {
        "parent_state_id": None,
        "shared": SharedStateData(),
        "user_intent_log": [],
        "current_user_intent": [],
        "user_turn_seq": 0,
        "dialog_steps_offset": 0,
        "inbox_seq": 0,
        "last_main_prompt_tokens": None,
        "last_main_prompt_message_count": None,
        "usage": UsageAccount(),
        "tool_usage_notice_cnt": {},
        "todo_reminder_cnt": 0,
        "project_memory_content_hash": None,
        "last_backend_response_id": None,
        "turn_tool_calls": 0,
    }.items():
        setattr(state, name, value)
    return state


def _hydrated(meta: dict):
    """(State по прежнему пути, State по новому, parts)."""
    view = _view(meta)
    parts = parts_from_meta(view)
    a, b = _state(), _state()
    old.hydrate(a, view)
    a.shared = old.shared_from_meta(view) or SharedStateData()
    conv.hydrate_state(
        b,
        parts.state,
        parts.usage,
        conv.notice_state(parts.state, parts.extra),
    )
    b.shared = (
        conv.shared_from_session(parts.state, parts.extra) or SharedStateData()
    )
    return a, b, parts, view


def _fields(state) -> dict:
    return {
        "intent": serialize_for_meta(state.user_intent_log),
        "current": serialize_for_meta(state.current_user_intent),
        "seq": state.user_turn_seq,
        "offset": state.dialog_steps_offset,
        "inbox": state.inbox_seq,
        "tokens": state.last_main_prompt_tokens,
        "count": state.last_main_prompt_message_count,
        "usage": state.usage.to_meta(),
        "notice": state.tool_usage_notice_cnt,
        "todo": state.todo_reminder_cnt,
        "hash": state.project_memory_content_hash,
        "shared": asdict(state.shared),
    }


@pytest.mark.parametrize(("name", "meta"), CORPUS, ids=[n for n, _ in CORPUS])
def test_hydration_matches_meta_path(name, meta):
    a, b, _parts, _view_meta = _hydrated(meta)
    assert _fields(b) == _fields(a)


def _turn(state) -> None:
    """Что меняет обычный ход."""
    state.current_user_intent = deserialize_list(
        [
            {
                "turn": state.user_turn_seq + 1,
                "ts": "2026-10-21T10:00:00Z",
                "action": "user_ask",
                "content": "сделай",
            }
        ]
    )
    state.inbox_seq = int(state.inbox_seq or 0) + 2
    state.last_main_prompt_tokens = 1234
    state.last_main_prompt_message_count = 7
    state.usage = UsageAccount.from_meta(
        {"by_category": {"main": {"input": 10, "output": 5}}}
    )
    state.shared.permission_mode = "plan"
    state.shared.plan = {"steps": [{"title": "шаг", "status": "pending"}]}
    state.tool_usage_notice_cnt = {"session_memory": 2}
    state.todo_reminder_cnt = 3
    state.project_memory_content_hash = "abc"
    state.last_backend_response_id = "resp_1"


def _quiet(state) -> None:
    """Шаг, который ничего не меняет (финализация хода без сообщений)."""


@pytest.mark.parametrize("turn", [_turn, _quiet], ids=["ход", "тихий"])
@pytest.mark.parametrize(("name", "meta"), CORPUS, ids=[n for n, _ in CORPUS])
@pytest.mark.parametrize(
    ("backend", "rendered"),
    [
        ("chat_completions", "SYSTEM"),
        ("chat_completions", None),
        ("responses", None),
    ],
    ids=["cc+промпт", "cc", "responses"],
)
def test_step_state_matches_meta_path(name, meta, backend, rendered, turn):
    a, b, parts, view = _hydrated(meta)
    turn(a)
    turn(b)
    session_meta = copy.deepcopy(view)
    session_meta["project_memory_check_cnt"] = 4
    expected_meta = old.meta_after_step(
        view,
        session_meta,
        a,
        rendered=rendered,
        backend=backend,
        previous_response_id="prev",
    )
    expected = parts_from_meta(expected_meta).state
    notice = conv.notice_state(parts.state, parts.extra)
    notice["project_memory_check_cnt"] = 4
    got = conv.session_from_state(
        parts.state,
        b,
        notice=notice,
        rendered_system_prompt=rendered,
        backend_api_mode=backend,
        previous_response_id="prev",
    )
    assert got.model_dump() == expected.model_dump()
    assert got.model_fields_set == expected.model_fields_set


def test_usage_from_state_matches_meta_path():
    state = _state()
    state.usage = UsageAccount.from_meta(
        {"by_category": {"main": {"input": 3}}}
    )
    expected = parts_from_meta({"token_usage": state.usage.to_meta()}).usage
    assert conv.usage_from_state(state) == expected


def test_legacy_memory_files_is_refused():
    parts = parts_from_meta({"memory_files": ["a"]})
    with pytest.raises(StateSchemaError):
        conv.shared_from_session(parts.state, parts.extra)


def test_state_without_schema_version_is_refused():
    meta = {"shared": {"notes_md": "x"}, "state_schema_version": "v1"}
    parts = parts_from_meta(meta)
    with pytest.raises(StateSchemaError):
        conv.shared_from_session(parts.state, parts.extra)


def test_extra_for_write_carries_schema_version():
    assert conv.extra_for_write({})["state_schema_version"] == "v2"
    assert conv.extra_for_write({"state_schema_version": "v2", "k": 1}) == {
        "state_schema_version": "v2",
        "k": 1,
    }


PAYLOADS = [
    ("минимум", {"summary_message_content": "кратко"}),
    (
        "всё",
        {
            "summary_message_content": "кратко",
            "dialog_steps_offset": 4,
            "last_main_prompt_tokens": 100,
            "last_main_prompt_message_count": 3,
            "user_turn_seq": 5,
            "user_intent_log": [
                {
                    "turn": 1,
                    "ts": "2026-10-21T10:00:00Z",
                    "action": "user_ask",
                    "content": "сделай",
                }
            ],
            "shared": {"working_memory": {"goal": "после"}, "notes_md": "n"},
        },
    ),
    ("битый offset", {"dialog_steps_offset": "x"}),
]


@pytest.mark.parametrize(
    ("pname", "payload"), PAYLOADS, ids=[p for p, _ in PAYLOADS]
)
@pytest.mark.parametrize(("name", "meta"), CORPUS, ids=[n for n, _ in CORPUS])
def test_compaction_state_matches_meta_path(name, meta, pname, payload):
    view = _view(meta)
    parts = parts_from_meta(view)
    now = "2026-10-21T10:00:00+00:00"
    got = conv.session_after_compaction(parts.state, payload, now_iso=now)
    try:
        expected_meta = old.compact_meta(
            view,
            payload,
            now_iso=now,
            backend="chat_completions",
            compact_count=1,
        )
    except ValueError:
        # Прежний путь падал на битом user_turn_seq; новый — нет (Review
        # Focus 1: такой тред открывается и сжимается).
        assert name == "битые типы"
        return
    expected = parts_from_meta(expected_meta).state
    assert got.model_dump() == expected.model_dump()
    assert got.model_fields_set == expected.model_fields_set
