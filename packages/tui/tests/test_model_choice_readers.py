"""Читатели треда TUI и запись смены модели (поток `model_choices`).

Запись привязана к последнему записанному сообщению перед применением
смены (`after`). Восстановление ленты и превью рисуют на её месте ту же
черту, что живая смена (`ModelMark`), с теми же правилами склейки: черта
сразу за чертой заменяет её, возврат к модели до первой из них — без
черты. Живой хвост записи не рисует — черту дала живая смена. Сообщение
привязки удалено — запись сирота, черты нет. Сообщением запись не
считается; экспорт — одна строка на смену.
"""

from __future__ import annotations

import json
import os

import pytest
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.persistence.model_choice_record import ModelChoiceRecord
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.threads.facade import run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit.thread_writes import (
    acompact,
    model_choice_record,
    turn_outcome_record,
)
from gc_testkit.tui_support import tui_settings
from gc_tui.services.export import export_thread
from gc_tui.services.stores.file_store import FileTaskStore

pytest.importorskip("textual")

_SYS = "cli"
_TID = "t1"
_SLOTS = {"light": "glm-flash", "default": "glm-4.6", "max": "giga-max"}
_CFG = {
    "agent.model": "m",
    "backend.api_mode": "chat_completions",
    "models": {"glm-flash": {}, "glm-4.6": {}, "giga-max": {}, "qwen": {}},
    "slots": _SLOTS,
}
_TO_MAX = ModelChoiceRecord("max", "giga-max", "glm-4.6")
_TO_QWEN = ModelChoiceRecord("qwen", "qwen", "giga-max")
_BACK_TO_DEFAULT = ModelChoiceRecord("default", "glm-4.6", "giga-max")


def _user(mid: str) -> dict:
    return {"role": "user", "content": f"{mid} вопрос", "id": mid}


def _assistant(mid: str) -> dict:
    return {"role": "assistant", "content": f"{mid} ответ", "id": mid}


class _Thread:
    """Тред хранилища v2: сообщения, записи смены и итога, сжатие."""

    def __init__(self, root) -> None:
        self.dir = str(root)
        os.makedirs(self.dir, exist_ok=True)

    def turn(self, n: int) -> _Thread:
        self.store.append_messages(_TID, [_user(f"u{n}"), _assistant(f"a{n}")])
        return self

    def choice(self, record: ModelChoiceRecord) -> _Thread:
        model_choice_record(self.dir, _SYS, _TID, record)
        return self

    def outcome(self) -> _Thread:
        last = self.store.read_all_events(_TID)[0][-1]["payload"]["id"]
        outcome = TurnOutcome(
            status="done",
            reason=None,
            opened_by=f"u{last[1:]}",
            started_at="2026-10-25T11:32:00.000Z",
            ended_at="2026-10-25T11:32:12.000Z",
            duration_sec=12.0,
            error=None,
        )
        turn_outcome_record(self.dir, _SYS, _TID, outcome)
        return self

    def compact(self) -> _Thread:
        summary = {
            "role": "user",
            "content": wrap_technical_user_content(
                "Summarized context from previous dialogue.\nкратко",
                technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
            ),
            "id": "sum_S",
        }
        key = ThreadKey(system=_SYS, thread=_TID)
        run_sync(acompact(FileThreadStore(self.dir), key, [summary]))
        return self

    def discard(self, message_id: str) -> _Thread:
        self.store.discard_message_by_id(_TID, message_id)
        return self

    @property
    def store(self) -> FileTaskStore:
        return FileTaskStore(self.dir, system=_SYS)

    def records(self) -> list[dict]:
        return self.store.read_all_events(_TID)[0]

    def segments(self) -> list[dict]:
        return self.store.read_segments(_TID)


def _marks(root):
    from gc_tui.widgets.model_mark import ModelMark

    return list(root.query(ModelMark))


def _labels(root) -> list[str]:
    return [m._content.plain.strip("─ ") for m in _marks(root)]


def _kinds(root) -> list[str]:
    return [type(w).__name__ for w in root.children]


# ---- лента ----------------------------------------------------------------


async def _restored(thread: _Thread, *, live: bool = False):
    """Лента после `_render_records`: (виды детей, подписи черт)."""
    from gc_tui.app import TuiApp

    records = thread.records()
    app = TuiApp(
        threads_dir=thread.dir, system=_SYS, settings=tui_settings(_CFG)
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(records, live=live)
        await pilot.pause()
        trace = scr.query_one("#trace")
        return _kinds(trace), _labels(trace)


@pytest.mark.asyncio
async def test_restore_draws_rule_after_its_message(tmp_path):
    first = _Thread(tmp_path / "first").turn(1)
    kinds_first, _ = await _restored(first)
    full = _Thread(tmp_path / "full").turn(1).choice(_TO_MAX).turn(2)

    kinds, labels = await _restored(full)

    assert labels == ["● giga-max"]
    assert kinds.index("ModelMark") == len(kinds_first)
    assert kinds.index("ModelMark") < len(kinds) - 1


@pytest.mark.asyncio
async def test_live_tail_does_not_draw_record(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX).turn(2)

    _, labels = await _restored(thread, live=True)

    assert labels == []


@pytest.mark.asyncio
async def test_restore_run_end_rule_follows_turn_line(tmp_path):
    thread = _Thread(tmp_path).turn(1).outcome().choice(_TO_MAX)

    kinds, labels = await _restored(thread)

    assert labels == ["● giga-max"]
    assert kinds.index("TurnLine") < kinds.index("ModelMark")


@pytest.mark.asyncio
async def test_restore_adjacent_records_leave_the_last(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX).choice(_TO_QWEN)
    thread.turn(2)

    _, labels = await _restored(thread)

    assert labels == ["● qwen"]


@pytest.mark.asyncio
async def test_restore_return_to_previous_model_leaves_no_rule(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX)
    thread.choice(_BACK_TO_DEFAULT).turn(2)

    _, labels = await _restored(thread)

    assert labels == []


@pytest.mark.asyncio
async def test_restore_records_with_message_between_keep_both(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX).turn(2)
    thread.choice(_TO_QWEN)

    _, labels = await _restored(thread)

    assert labels == ["● giga-max", "● qwen"]


@pytest.mark.asyncio
async def test_restore_hides_record_whose_message_is_gone(tmp_path):
    """Сообщение привязки откатили — запись сирота, черты нет; откат
    реплики после неё черту не трогает."""
    orphan = _Thread(tmp_path / "o").turn(1).choice(_TO_MAX).discard("a1")
    kept = _Thread(tmp_path / "k").turn(1).choice(_TO_MAX).turn(2)
    kept.discard("u2")

    assert (await _restored(orphan))[1] == []
    assert (await _restored(kept))[1] == ["● giga-max"]


# ---- превью ---------------------------------------------------------------


async def _previewed(segments: list[dict]):
    from gc_tui.services.trace_render import render_segments_readonly
    from textual.app import App
    from textual.containers import VerticalScroll

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(
                self.query_one("#t"), segments, model_slots=_SLOTS
            )

    async with _A().run_test() as pilot:
        await pilot.pause()
        trace = pilot.app.query_one("#t")
        return _kinds(trace), _labels(trace)


@pytest.mark.asyncio
async def test_preview_draws_rule_after_its_message(tmp_path):
    first = _Thread(tmp_path / "first").turn(1)
    kinds_first, _ = await _previewed(first.segments())
    full = _Thread(tmp_path / "full").turn(1).choice(_TO_MAX).turn(2)

    kinds, labels = await _previewed(full.segments())

    assert labels == ["● giga-max"]
    assert kinds.index("ModelMark") == len(kinds_first)


@pytest.mark.asyncio
async def test_preview_collapses_like_the_feed(tmp_path):
    adjacent = _Thread(tmp_path / "a").turn(1).choice(_TO_MAX)
    adjacent.choice(_TO_QWEN).turn(2)
    back = _Thread(tmp_path / "b").turn(1).choice(_TO_MAX)
    back.choice(_BACK_TO_DEFAULT).turn(2)

    assert (await _previewed(adjacent.segments()))[1] == ["● qwen"]
    assert (await _previewed(back.segments()))[1] == []


@pytest.mark.asyncio
async def test_preview_rule_survives_compaction(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX).compact()

    kinds, labels = await _previewed(thread.segments())

    assert labels == ["● giga-max"]
    assert kinds.index("ModelMark") < kinds.index("CompactionRule")


# ---- сегменты и экспорт ---------------------------------------------------


def test_segment_counts_only_messages(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX).compact()

    segments = thread.segments()

    assert segments[0]["compacted_by"]["messages_in_segment"] == 2
    assert segments[0]["messages"][-1]["type"] == "model_choice"


def _export(thread: _Thread, fmt: str) -> str:
    return export_thread(
        thread.segments(),
        fmt=fmt,
        api_mode="chat_completions",
        thread_id=_TID,
        system=_SYS,
    )


def test_export_txt_has_one_line_per_change(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX).choice(_TO_QWEN)
    thread.choice(ModelChoiceRecord("auto", None, "qwen")).turn(2)

    txt = _export(thread, "txt")

    lines = [ln for ln in txt.splitlines() if "модель:" in ln]
    assert [ln.strip("─ ") for ln in lines] == [
        "модель: giga-max",
        "модель: qwen",
        "модель: auto",
    ]
    assert "4 messages" in txt
    assert txt.index("a1 ответ") < txt.index(lines[0]) < txt.index("u2")


def test_export_json_keeps_only_messages(tmp_path):
    thread = _Thread(tmp_path).turn(1).choice(_TO_MAX)

    data = json.loads(_export(thread, "json"))

    assert data["thread"]["messages_count"] == 2
    assert [m["id"] for m in data["segments"][0]["messages"]] == ["u1", "a1"]
