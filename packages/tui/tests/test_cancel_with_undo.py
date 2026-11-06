"""TraceScreen счётчик _events_since_last_user_send + cancel-with-undo."""

import pytest
from gc_testkit.develop_files import meta_of
from gc_testkit.tui_support import tui_settings


async def _worker_session(tmp_path, tid):
    """Сессия хода воркера над файловым тредом (берёт аренду на записи)."""
    from gc_core.thread_store import ThreadSession
    from gc_core.threads.file_store import FileThreadStore

    return await ThreadSession.afrom_config(
        settings=None,
        system_id="cli",
        thread_id=tid,
        store=FileThreadStore(str(tmp_path)),
    )


@pytest.mark.asyncio
async def test_counter_resets_on_user_send():
    """_events_since_last_user_send обнуляется при user-send."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._events_since_last_user_send = 5  # симулируем накопленные события
        scr._on_user_text_submitted("hello")
        assert scr._events_since_last_user_send == 0


@pytest.mark.asyncio
async def test_counter_increments_on_non_user_event():
    """_events_since_last_user_send растёт на каждом не-user trace event."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._events_since_last_user_send = 0

        # agent event
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-05-28T00:00:00Z",
            kind="agent",
            title="",
            summary="ответ",
            tool_name=None,
            call_id=None,
            full_payload=None,
        )
        scr._render_event(ev)
        await pilot.pause()
        assert scr._events_since_last_user_send == 1

        # tool call event
        ev2 = TraceEvent(
            no=2,
            event_ts_utc_iso="2026-05-28T00:00:00Z",
            kind="call",
            title="",
            summary="read_file(path='x')",
            tool_name="read_file",
            call_id="c1",
            full_payload=None,
        )
        scr._render_event(ev2)
        await pilot.pause()
        assert scr._events_since_last_user_send == 2


@pytest.mark.asyncio
async def test_counter_not_incremented_on_user_event():
    """user kind событие НЕ инкрементирует (user-send сам обнуляет)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._events_since_last_user_send = 3

        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-05-28T00:00:00Z",
            kind="user",
            title="",
            summary="привет",
            tool_name=None,
            call_id=None,
            full_payload=None,
        )
        scr._render_event(ev)
        await pilot.pause()
        # User event не должен инкрементировать счётчик
        assert scr._events_since_last_user_send == 3


@pytest.mark.asyncio
async def test_cancel_with_undo_removes_user_widget_and_restores_input():
    """0 событий от агента + input пуст → Esc-stop: убрать user-widget, восстановить input."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        # Симулируем user-send
        scr._on_user_text_submitted("исходный запрос")
        # Смонтируем user-widget вручную
        widget = UserReplyContainer("исходный запрос", event_no=0)
        scr._trace_mount(widget)
        await pilot.pause()
        assert len(scr.query(UserReplyContainer)) == 1
        assert scr._events_since_last_user_send == 0

        # Stop при 0 событий И пустом input → cancel-with-undo
        scr._cmd_stop()
        await pilot.pause()

        # user-widget удалён
        assert len(scr.query(UserReplyContainer)) == 0
        # input восстановлен
        input_field = scr.query_one("#cmdinput")
        assert input_field.text == "исходный запрос"
        # курсор стоит в самом конце восстановленной строки (готов к продолжению)
        assert input_field.cursor_location == input_field.document.end
        # никакой системной «·»-строки не появилось
        from gc_tui.widgets.trace_row import TraceRow

        meta_rows = [
            r for r in scr.query(TraceRow) if r.has_class("line-meta")
        ]
        assert not any(
            "·" in str(r.query_one(".trace-prefix").render())
            for r in meta_rows
        )


@pytest.mark.asyncio
async def test_cancel_with_undo_removes_pending_record(tmp_path):
    """Быстрая отмена (Esc, агент не начал) должна полностью откатывать реплику:
    запись из meta.tui.pending_user_messages удаляется — иначе на re-entry
    _mount_failed_from_meta покажет её как фантомную [!]."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app.active_runner = _FakeRunner()
        tid = "t1"
        app.tui_state.active_task_id = tid
        scr._active_task_id = tid
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")

        # Симулируем быструю отправку: счётчик + pending-запись + user-widget.
        scr._on_user_text_submitted("исходный запрос")
        scr._track_pending(
            display_text="исходный запрос", wire_content="исходный запрос"
        )
        scr._trace_mount(UserReplyContainer("исходный запрос", event_no=0))
        await pilot.pause()
        pum = (meta_of(scr._store.get_record(tid)).get("tui") or {}).get(
            "pending_user_messages", []
        )
        assert len(pum) == 1  # запись записана

        # Быстрая отмена (0 событий, пустой input) → cancel-with-undo.
        scr._cmd_stop()
        await pilot.pause()

        pum2 = (meta_of(scr._store.get_record(tid)).get("tui") or {}).get(
            "pending_user_messages", []
        )
        assert pum2 == [], (
            f"pending должен быть очищен после cancel-with-undo, got {pum2}"
        )


@pytest.mark.asyncio
async def test_cancel_with_undo_removes_orphan_from_thread(tmp_path):
    """Быстрая отмена ставит заявку, а poll (когда тред idle) удаляет осиротевшую
    реплику из data_*.jsonl по её id — после того, как остановленный ход
    отпустит тред."""
    pytest.importorskip("textual")
    from gc_testkit.develop_files import load_thread_messages
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _FakeRunner:
        def __init__(self):
            self._running = True

        def is_running(self) -> bool:
            return self._running

        def stop(self) -> None:
            self._running = False

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        tid, msg_id = "t1", "m_orphan"
        app.tui_state.active_task_id = tid
        scr._active_task_id = tid
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")

        # Симулируем, что worker уже записал реплику в тред (persist до ответа).
        ts = await _worker_session(tmp_path, tid)
        await ts.apersist_batch(
            [{"role": "user", "content": "отменяю", "id": msg_id}],
            previous_response_id=None,
        )

        app.active_runner = _FakeRunner()
        scr._on_user_text_submitted("отменяю")
        scr._last_user_message_id = msg_id
        scr._trace_mount(UserReplyContainer("отменяю", event_no=0))
        await pilot.pause()

        scr._cmd_stop()  # 0 событий + пустой input → cancel-with-undo
        await pilot.pause()

        # Тред ещё держит остановленный ход (аренда его сессии), а раннер
        # уже не бежит: тик опроса удалить не может — запись в чужой ход
        # дала бы ThreadBusy. Заявка остаётся, реплика пока на месте.
        scr._run_orphan_cleanups()
        assert scr._pending_orphan_cleanups
        msgs, _, _ = load_thread_messages(str(tmp_path), "cli", tid)
        assert any(m.get("id") == msg_id for m in msgs)

        # Ход закончился и отпустил тред — опрос удаляет осиротевшую.
        await ts.arelease_lease()
        scr._run_orphan_cleanups()
        msgs, _, _ = load_thread_messages(str(tmp_path), "cli", tid)
        assert all(m.get("id") != msg_id for m in msgs)


@pytest.mark.asyncio
async def test_cancel_with_undo_no_stream_reset_flicker(tmp_path):
    """После немедленного удаления курсор трейса пересинхронизирован: следующий
    tail_events НЕ репортит stream_reset (иначе была бы полная перерисовка =
    моргание)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _FakeRunner:
        def __init__(self):
            self._running = True

        def is_running(self) -> bool:
            return self._running

        def stop(self) -> None:
            self._running = False

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        tid, msg_id = "t1", "m_orphan"
        app.tui_state.active_task_id = tid
        scr._active_task_id = tid
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        await (await _worker_session(tmp_path, tid)).apersist_batch(
            [{"role": "user", "content": "ты тут?", "id": msg_id}],
            previous_response_id=None,
        )

        # Курсор указывает на конец файла С осиротевшей записью.
        _records, cur = scr._store.read_all_events(tid)
        scr._cursor = cur

        app.active_runner = _FakeRunner()
        scr._on_user_text_submitted("ты тут?")
        scr._last_user_message_id = msg_id
        scr._trace_mount(UserReplyContainer("ты тут?", event_no=0))
        await pilot.pause()

        scr._cmd_stop()
        await pilot.pause()

        # Курсор пересинхронизирован на ужавшийся файл → нет stream_reset.
        tail = scr._store.tail_events(tid, scr._cursor)
        assert tail.stream_reset is False


@pytest.mark.asyncio
async def test_orphan_cleanup_deferred_while_thread_busy(tmp_path):
    """Пока на треде активен run — удаление откладывается (нет гонки с дозаписью)."""
    pytest.importorskip("textual")
    from gc_testkit.develop_files import load_thread_messages
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore

    class _Runner:
        def __init__(self, running):
            self._running = running

        def is_running(self) -> bool:
            return self._running

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        tid, msg_id = "t1", "m_orphan"
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        await (await _worker_session(tmp_path, tid)).apersist_batch(
            [{"role": "user", "content": "x", "id": msg_id}],
            previous_response_id=None,
        )

        # Старый runner уже стоп, НО на треде активен новый run (повтор) → откладываем.
        app.tui_state.active_task_id = tid
        app.active_runner = _Runner(running=True)
        scr._pending_orphan_cleanups = [(_Runner(running=False), tid, msg_id)]

        scr._run_orphan_cleanups()
        msgs, _, _ = load_thread_messages(str(tmp_path), "cli", tid)
        assert any(
            m.get("id") == msg_id for m in msgs
        )  # не удалили — тред занят
        assert scr._pending_orphan_cleanups  # заявка осталась


@pytest.mark.asyncio
async def test_cancel_with_undo_skipped_when_events_present():
    """≥1 agent event → НЕ cancel-with-undo, нормальная отмена: индикатор → cancelled."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        running = True

        def is_running(self) -> bool:
            return self.running

        def stop(self) -> None:
            self.running = False  # ход кончился сразу: итог ровный

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        scr._on_user_text_submitted("запрос")
        scr._events_since_last_user_send = 2  # агент уже что-то выдал
        # Без паузы до стопа: тик без треда убрал бы индикатор «думаю».
        scr._show_thinking_indicator(state="thinking")
        scr._cmd_stop()
        await pilot.pause(0.5)
        # Индикатор остаётся в терминальном состоянии cancelled.
        ind = scr._thinking
        assert ind is not None
        assert ind._state == "cancelled"


@pytest.mark.asyncio
async def test_cancel_with_undo_skipped_when_input_not_empty():
    """input.text != "" → НЕ cancel-with-undo, нормальная отмена: индикатор → cancelled."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        running = True

        def is_running(self) -> bool:
            return self.running

        def stop(self) -> None:
            self.running = False  # ход кончился сразу: итог ровный

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        scr._on_user_text_submitted("первый запрос")
        scr._events_since_last_user_send = 0
        scr.query_one("#cmdinput").text = "новая печать"
        # Без паузы до стопа: тик без треда убрал бы индикатор «думаю».
        scr._show_thinking_indicator(state="thinking")
        scr._cmd_stop()
        await pilot.pause(0.5)
        # Индикатор остаётся в терминальном состоянии cancelled.
        ind = scr._thinking
        assert ind is not None
        assert ind._state == "cancelled"


@pytest.mark.asyncio
async def test_orphan_cleanup_keeps_claim_on_store_error(tmp_path, caplog):
    """Сбой хранилища при удалении не снимает заявку: тик повторяет её, в
    лог — одна запись на заявку; после сбоя удаление проходит."""
    pytest.importorskip("textual")
    from gc_testkit.develop_files import load_thread_messages
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        tid, msg_id = "t1", "m_orphan"
        store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        scr._store = store
        session = await _worker_session(tmp_path, tid)
        await session.apersist_batch(
            [{"role": "user", "content": "x", "id": msg_id}],
            previous_response_id=None,
        )
        await session.arelease_lease()
        app.tui_state.active_task_id = tid
        app.active_runner = None
        scr._pending_orphan_cleanups = [(None, tid, msg_id)]

        real_discard = store.discard_message_by_id

        def broken(*_args, **_kwargs):
            raise OSError("диск недоступен")

        store.discard_message_by_id = broken
        with caplog.at_level("WARNING", logger="gc_tui.screens.main"):
            scr._run_orphan_cleanups()
            scr._run_orphan_cleanups()
        assert scr._pending_orphan_cleanups == [(None, tid, msg_id)]
        assert len(caplog.records) == 1

        store.discard_message_by_id = real_discard
        scr._run_orphan_cleanups()
        msgs, _, _ = load_thread_messages(str(tmp_path), "cli", tid)
        assert all(m.get("id") != msg_id for m in msgs)
        assert scr._pending_orphan_cleanups == []
