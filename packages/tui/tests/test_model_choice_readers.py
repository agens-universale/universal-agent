"""Читатели треда TUI и запись смены модели (`type: "model_choice"`).

Запись встаёт в `data_*.jsonl` за последним сообщением перед применением
смены и повторяет его `seq`. Восстановление ленты и превью рисуют на её
месте ту же черту, что живая смена (`ModelMark`), с теми же правилами
склейки: черта сразу за чертой заменяет её, возврат к модели до первой из
них — без черты. Живой хвост записи не рисует — черту дала живая смена.
Сообщением запись не считается; экспорт — одна строка на смену.
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
from gc_core.thread_store import (
    append_model_choice_record,
    append_new_thread_messages,
    append_turn_outcome_record,
    archive_and_replace_thread_files,
    load_thread_meta,
    save_thread_meta,
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
    """Файлы треда: сообщения, записи смены и итога, сжатие."""

    def __init__(self, root) -> None:
        self.dir = str(root)
        os.makedirs(self.dir, exist_ok=True)
        self.seq = 0
        save_thread_meta(self.dir, _SYS, _TID, {})

    def turn(self, n: int) -> _Thread:
        msgs = [_user(f"u{n}"), _assistant(f"a{n}")]
        append_new_thread_messages(
            self.dir, _SYS, _TID, msgs, set(), start_seq=self.seq
        )
        self.seq += len(msgs)
        return self

    def choice(self, record: ModelChoiceRecord) -> _Thread:
        append_model_choice_record(self.dir, _SYS, _TID, record, seq=self.seq)
        return self

    def outcome(self) -> _Thread:
        outcome = TurnOutcome(
            status="done",
            reason=None,
            opened_by=f"u{self.seq // 2}",
            started_at="2026-10-25T11:32:00.000Z",
            ended_at="2026-10-25T11:32:12.000Z",
            duration_sec=12.0,
            error=None,
        )
        append_turn_outcome_record(self.dir, _SYS, _TID, outcome, seq=self.seq)
        return self

    def compact(self) -> _Thread:
        meta = load_thread_meta(self.dir, _SYS, _TID)
        summary = {
            "role": "user",
            "content": wrap_technical_user_content(
                "Summarized context from previous dialogue.\nкратко",
                technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
            ),
            "id": "sum_S",
        }
        archive_and_replace_thread_files(
            self.dir, _SYS, _TID, [summary], archive_suffix="S"
        )
        save_thread_meta(
            self.dir,
            _SYS,
            _TID,
            {**meta, "last_summary_archive_suffix": "S"},
        )
        self.seq = 1
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
        thread.segments(), fmt=fmt, meta={}, thread_id=_TID, system=_SYS
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
