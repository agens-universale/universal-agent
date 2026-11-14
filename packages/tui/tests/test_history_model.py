"""Модель истории ленты — построение (спека шага 2 окна ленты, §5.1)."""

from __future__ import annotations

import json
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest
from feed_dump import HISTORY_THREAD, load_history_fixture, write_history
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.persistence.turn_outcome import parse_turn_outcome
from gc_testkit.tui_support import tui_settings
from gc_tui.services.history_model import (
    HistoryInputs,
    HistoryItem,
    HistoryModel,
    build_history,
)
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.services.trace_retention import (
    is_light_payload,
    message_ids_in_retention_window,
    retention_window_start_index,
)

_FIXTURES = [
    "calls",
    "compaction",
    "subagents",
    "ask_human",
    "withdrawn",
    "model_change",
    "plan",
    "finish_attempts",
]
_INPUTS = HistoryInputs(root="/work/proj", injected_fields=())


def _segments(name: str, tmp_path) -> list[dict]:
    """Сегменты фикстуры так, как их читает открытие сессии."""
    write_history(str(tmp_path), load_history_fixture(name))
    segments, _cursor = FileTaskStore(str(tmp_path)).read_history(
        HISTORY_THREAD
    )
    return segments


def _model(name: str, tmp_path) -> HistoryModel:
    return build_history(_segments(name, tmp_path), inputs=_INPUTS)


def _raw(records) -> list[dict]:
    """Записи одним сегментом, как есть (без отсева сирот хранилищем)."""
    return [{"index": 0, "messages": list(records), "compacted_by": None}]


def _events(model: HistoryModel) -> list:
    return [it.event for it in model.items if it.event is not None]


@pytest.mark.parametrize("name", _FIXTURES)
def test_numbers_are_final_and_dense(tmp_path, name):
    model = _model(name, tmp_path)
    events = _events(model)
    n = len(events)
    assert [ev.no for ev in events] == list(range(1, n + 1))
    assert model.next_no == n + 1
    assert [model.event_by_no(no) for no in range(1, n + 1)] == events
    assert model.event_by_no(0) is None
    assert model.event_by_no(n + 1) is None


@pytest.mark.parametrize(
    ("name", "turns"),
    [("calls", 3), ("ask_human", 2), ("plan", 2), ("compaction", 3)],
)
def test_turn_starts_on_real_replies_only(tmp_path, name, turns):
    """Ответ на вопрос агента (ask_human) и техническое продолжение (calls,
    plan) хода не открывают; ходы режутся только по настоящим репликам."""
    model = _model(name, tmp_path)
    assert model.turn_count() == turns
    assert model.turn_starts[0] == 0
    for start in model.turn_starts:
        payload = model.items[start].event.full_payload
        assert extract_real_user_message_text(payload)
    assert [it for i in range(turns) for it in model.turn_items(i)] == (
        model.items
    )
    for i in range(turns):
        for it in model.turn_items(i):
            if it.event is not None:
                assert model.turn_index_of(it.event.no) == i


def test_ask_human_answer_inside_its_turn(tmp_path):
    model = _model("ask_human", tmp_path)
    answer = next(
        ev
        for ev in _events(model)
        if ev.tool_name == "ask_human" and ev.kind == "user"
    )
    assert model.turn_index_of(answer.no) == 0
    assert model.unanswered == set()


def test_preserved_copy_rendered_once(tmp_path):
    """Preserved-копия (тот же id в следующей части сжатия) — один раз, на
    месте первого вхождения."""
    segments = _segments("compaction", tmp_path)
    copied = next(
        r
        for r in segments[0]["messages"]
        if (r.get("payload") or {}).get("type") == "function_call_output"
    )
    segments[1]["messages"].insert(0, dict(copied))
    model = build_history(segments, inputs=_INPUTS)
    ids = [ev.message_id for ev in _events(model) if ev.message_id]
    assert len(ids) == len(set(ids))
    first = next(
        ev for ev in _events(model) if ev.message_id == copied["payload"]["id"]
    )
    assert model.turn_index_of(first.no) == 0
    assert [it.kind for it in model.items].count("compaction") == (
        len(segments) - 1
    )


def test_orphan_outcome_skipped():
    records = load_history_fixture("withdrawn").records
    outcomes = [r for r in records if r.get("type") == "turn_outcome"]
    model = build_history(_raw(records), inputs=_INPUTS)
    sides = [it.side for it in model.items if it.kind == "outcome"]
    assert sides == outcomes[:2]
    assert model.last_turn_outcome == parse_turn_outcome(outcomes[1])


def test_last_model_choice_and_outcome():
    records = load_history_fixture("model_change").records
    model = build_history(_raw(records), inputs=_INPUTS)
    choices = [r for r in records if r.get("type") == "model_choice"]
    last_outcome = [r for r in records if r.get("type") == "turn_outcome"][-1]
    assert model.last_model_choice == choices[-1]
    assert [it.side for it in model.items if it.kind == "model_choice"] == (
        choices
    )
    assert model.last_turn_outcome == parse_turn_outcome(last_outcome)
    assert model.outcome_superseded is False


def test_reply_after_outcome_supersedes_it():
    records = load_history_fixture("calls").records
    reply = {
        "type": "message",
        "ts": "2026-11-13T23:00:00+00:00",
        "payload": {"id": "m_next", "role": "user", "content": "дальше"},
    }
    model = build_history(_raw([*records, reply]), inputs=_INPUTS)
    assert model.outcome_superseded is True
    assert model.turn_count() == 4


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (("event", "outcome", "compaction"), None),
        (("event", "outcome", "model_choice", "compaction"), None),
        (("event", "compaction"), 0),
        (("event", "outcome", "event"), 0),
    ],
)
def test_open_turn_skips_compaction_marker(kinds, expected):
    """Маркер сжатия встаёт после итога последнего хода части треда: ход
    за ним закончен, как и за чертой смены модели."""
    model = HistoryModel(
        items=[HistoryItem(kind) for kind in kinds], turn_starts=[0]
    )
    assert model.open_turn() == expected


def test_unanswered_questions():
    records = list(load_history_fixture("ask_human").records)
    i, answer = next(
        (i, r)
        for i, r in enumerate(records)
        if '"turn_ask_human"' in str((r.get("payload") or {}).get("output"))
    )
    output = json.loads(answer["payload"]["output"])
    output.update(ok=False, response="(empty response)")
    payload = {**answer["payload"], "output": json.dumps(output)}
    records[i] = {**answer, "payload": payload}
    model = build_history(_raw(records), inputs=_INPUTS)
    assert model.unanswered == {payload["call_id"]}


def test_window_ids_match_retention(tmp_path):
    segments = _segments("compaction", tmp_path)
    model = build_history(segments, inputs=_INPUTS)
    window_ids = message_ids_in_retention_window(segments)
    assert model.window_ids == window_ids
    window_start = retention_window_start_index(len(segments))
    bounds = [*model.segment_starts, len(model.items)]
    light = {"call", "result", "error"}
    for seg, (a, b) in enumerate(pairwise(bounds)):
        for it in model.items[a:b]:
            ev = it.event
            if ev is None or ev.kind not in light:
                continue
            kept = seg >= window_start or ev.message_id in window_ids
            assert is_light_payload(ev) is not kept


def test_withdrawn_empty_at_build(tmp_path):
    assert _model("withdrawn", tmp_path).withdrawn == set()


def test_build_history_imports_no_textual(tmp_path):
    fixture = load_history_fixture("calls")
    path = tmp_path / "segments.json"
    path.write_text(json.dumps(_raw(fixture.records)), encoding="utf-8")
    code = (
        "import json, sys\n"
        "from gc_tui.services.history_model import HistoryInputs, "
        "build_history\n"
        f"segments = json.loads(open({str(path)!r}).read())\n"
        "build_history(segments, inputs=HistoryInputs(None, ()))\n"
        "assert 'textual' not in sys.modules, 'textual'\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


# ---- то же, что нынешнее открытие сессии ----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _FIXTURES)
async def test_model_matches_open_path(tmp_path, name):
    """Открытие сессии: модель экрана — модель из ``read_history`` со
    входами сессии (события, номера, состояние)."""
    # Импорт приложения — в тесте: палитра выбирается до импорта gc_tui.app.
    from gc_tui.app import TuiApp

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    write_history(str(threads_dir), load_history_fixture(name))
    app = TuiApp(
        threads_dir=str(threads_dir),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(160, 45)) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        inputs = HistoryInputs(
            root=str(Path.cwd()),
            injected_fields=tuple(sorted(scr._injected_fields())),
        )
        segments, _cursor = scr._store.read_history(HISTORY_THREAD)
        model = build_history(segments, inputs=inputs)
        screen = scr._feed_history
        assert _events(model) == screen.events()
        assert model.items == screen.items
        assert model.rendered_ids == screen.rendered_ids
        assert model.next_no == screen.next_no
        assert model.last_turn_outcome == screen.last_turn_outcome
        assert model.outcome_superseded == screen.outcome_superseded
        assert model.response_tail == screen.response_tail
        assert model.segment_starts == screen.segment_starts
