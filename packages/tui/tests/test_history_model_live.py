"""Модель истории — единственный владелец истории экрана (спека шага 2
окна ленты, §5.1, §5.6): живой путь пишет в неё, читатели читают её."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from feed_dump import (
    HISTORY_THREAD,
    feed_live,
    load_history_fixture,
)
from gc_testkit.develop_files import record_from_meta
from gc_testkit.tui_support import tui_settings
from gc_tui.models import TraceEvent
from gc_tui.services.history_model import (
    HistoryInputs,
    HistoryModel,
    build_history,
)
from gc_tui.services.stores.file_store import FileTaskStore

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
_SETTINGS = {
    "agent.model": "M",
    "models": {
        "fast-model": {"url": "http://127.0.0.1:9/v1", "token": ""},
        "strong-model": {"url": "http://127.0.0.1:9/v1", "token": ""},
    },
    "slots": {"default": "fast-model", "max": "strong-model"},
}
_TID = "withdrawnaaaaaaaaaaaaaaaaaaaaaaa"


def _app(threads_dir: Path):
    # Импорт приложения — в тесте: палитра выбирается до импорта gc_tui.app.
    from gc_tui.app import TuiApp

    threads_dir.mkdir(exist_ok=True)
    return TuiApp(
        threads_dir=str(threads_dir),
        system="cli",
        settings=tui_settings(_SETTINGS),
    )


def _state(model: HistoryModel) -> dict:
    """Поля модели, которые живой путь обязан вести как построение.
    Контекст отображения и окно удержания живой путь берёт с пачки. В
    хвосте ответа — только сообщения: запись сбоку (payload None) хвост
    опроса несёт и сиротой, которую чтение истории отсеивает."""
    return {
        "items": model.items,
        "turn_starts": model.turn_starts,
        "segment_starts": model.segment_starts,
        "rendered_ids": model.rendered_ids,
        "response_tail": [p for p in model.response_tail if p is not None],
        "next_no": model.next_no,
        "last_model_choice": model.last_model_choice,
        "last_turn_outcome": model.last_turn_outcome,
        "outcome_superseded": model.outcome_superseded,
        "unanswered": model.unanswered,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _FIXTURES)
async def test_live_path_appends_through_model(tmp_path, name):
    """Записи, поданные живым путём, дают ту же модель, что построение из
    тех же записей треда."""
    fixture = load_history_fixture(name)
    async with _app(tmp_path / "threads").run_test(size=(160, 45)) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        feed_live(scr, fixture.records, splits=(3,), sidecar=fixture.sidecar)
        segments, _cursor = scr._store.read_history(HISTORY_THREAD)
        inputs = HistoryInputs(
            root=str(Path.cwd()),
            injected_fields=tuple(sorted(scr._injected_fields())),
        )
        built = build_history(segments, inputs=inputs)
        assert _state(scr._feed_history) == _state(built)
        for no in range(1, built.next_no):
            assert scr._feed_history.event_by_no(no) == built.event_by_no(no)


# ---- отзыв реплики --------------------------------------------------------


class _StoppedRunner:
    """Ход, остановленный откатом: уже не бежит, вывода не было."""

    def is_running(self) -> bool:
        return False

    def produced_output(self) -> bool:
        return False

    def stop(self) -> None:
        pass


async def _withdrawn_reply(pilot, threads_dir: Path) -> str:
    """Реплика открытого треда записана, дочитана лентой и снята откатом."""
    scr = pilot.app.screen
    (threads_dir / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")
    assert scr.attach_active_task(_TID, is_resume=True)
    msg_id = "m_withdrawn"
    FileTaskStore(str(threads_dir)).append_messages(
        _TID, [{"role": "user", "content": "снять", "id": msg_id}]
    )
    scr._poll_trace_tail()
    assert msg_id in scr._feed_history.rendered_ids
    pilot.app.active_runner = _StoppedRunner()
    scr._last_user_message_id = msg_id
    scr._cancel_with_undo()
    await pilot.pause()
    return msg_id


@pytest.mark.asyncio
async def test_withdrawn_is_permanent(tmp_path):
    """Запись снятой реплики удалена из файла (заявка исполнена) — отметка
    модели остаётся."""
    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=(160, 45)) as pilot:
        scr = pilot.app.screen
        msg_id = await _withdrawn_reply(pilot, threads_dir)
        scr._run_orphan_cleanups()
        assert scr._pending_orphan_cleanups == []
        assert msg_id in scr._feed_history.withdrawn
        assert msg_id not in scr._feed_history.rendered_ids


@pytest.mark.asyncio
async def test_rebuild_keeps_withdrawn_reply_out(tmp_path):
    """Пересборка, пока запись снятой реплики ещё в файле, а заявки уже
    нет, реплику не воскрешает."""
    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=(160, 45)) as pilot:
        scr = pilot.app.screen
        msg_id = "m_kept"
        await _withdrawn_reply(pilot, threads_dir)
        # Запись ещё в файле (тред держал ход), заявки нет.
        FileTaskStore(str(threads_dir)).append_messages(
            _TID, [{"role": "user", "content": "снова", "id": msg_id}]
        )
        scr._feed_history.mark_withdrawn(msg_id)
        scr._pending_orphan_cleanups = []
        scr._rebuild_from_store()
        await pilot.pause()
        ids = {ev.message_id for ev in scr._feed_history.events()}
        assert msg_id not in ids
        assert msg_id in scr._feed_history.withdrawn


def test_build_history_skips_withdrawn():
    records = [
        {"ts": "", "payload": {"id": "u1", "role": "user", "content": "a"}},
        {"ts": "", "payload": {"id": "u2", "role": "user", "content": "b"}},
    ]
    segments = [{"index": 0, "messages": records, "compacted_by": None}]
    model = build_history(
        segments, inputs=HistoryInputs(None, ()), withdrawn={"u2"}
    )
    assert [ev.message_id for ev in model.events()] == ["u1"]
    assert model.rendered_ids == {"u1"}
    assert model.withdrawn == {"u2"}


# ---- читатели истории ------------------------------------------------------


def _ev(no: int, kind: str, **kw) -> TraceEvent:
    return TraceEvent(
        no=no,
        event_ts_utc_iso="",
        kind=kind,
        title=kind,
        summary=kw.pop("summary", kind),
        full_payload=kw.pop("full_payload", {"n": no}),
        **kw,
    )


@pytest.mark.asyncio
async def test_consumers_read_model(tmp_path):
    """Просмотр события, карточка вызова и сверка вопроса агента читают
    модель, а не ленту."""
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.screens.viewer import ViewerScreen

    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        scr._feed_history.append_event(_ev(1, "agent"))
        scr._feed_history.append_event(
            _ev(2, "call", call_id="c1", tool_name="fs_read_file")
        )
        scr._feed_history.append_event(_ev(3, "result", call_id="c1"))
        scr._feed_history.append_event(_ev(4, "ask_human", call_id="q1"))

        scr._open_event_by_no(1)
        await pilot.pause()
        assert isinstance(pilot.app.screen, ViewerScreen)
        pilot.app.pop_screen()
        await pilot.pause()

        scr.on_open_tool_call_detail_request(SimpleNamespace(call_id="c1"))
        await pilot.pause()
        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        pilot.app.pop_screen()
        await pilot.pause()

        assert scr._ask_human_dialog_continued() is False
        scr._feed_history.append_event(_ev(5, "user", summary="ответ"))
        assert scr._ask_human_dialog_continued() is True


def test_meta_replies_read_model(monkeypatch):
    """Реплика из meta, которая есть среди событий модели, доставлена —
    failed не монтируется."""
    from gc_tui.screens.main import TraceScreen

    pending = {"client_id": "c1", "content": "hello", "status": "pending"}
    record = record_from_meta({"tui": {"pending_user_messages": [pending]}})
    scr = TraceScreen()
    scr._store = SimpleNamespace(get_record=lambda _tid: record)
    mounted: list[dict] = []
    monkeypatch.setattr(
        scr, "_mount_orphaned_reply", lambda r, alive: mounted.append(r)
    )
    monkeypatch.setattr(scr, "_refresh_queue_hint", lambda: None)
    monkeypatch.setattr(scr, "_write_tui_meta", lambda *a: None)
    monkeypatch.setattr(scr, "_run_in_progress", lambda: False)

    scr._feed_history.append_event(_ev(1, "user", summary="hello"))
    scr._mount_failed_from_meta("t")
    assert mounted == []

    scr._feed_history = HistoryModel()
    scr._mount_failed_from_meta("t")
    assert mounted == [pending]
