"""TraceScreen cancel-with-undo: откат отправки, пока ядро не отметило
вывод прогона (`produced_output` раннера)."""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_cancel_with_undo_removes_user_widget_and_restores_input():
    """Агент ничего не выдал + input пуст → Esc-stop: убрать user-widget,
    восстановить input."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def produced_output(self) -> bool:
            return False

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        # Симулируем user-send
        scr._on_user_text_submitted("исходный запрос")
        scr._last_user_message_id = "m_undo"
        # Смонтируем user-widget вручную
        widget = UserReplyContainer("исходный запрос", event_no=0)
        scr._trace_mount(widget)
        await pilot.pause()
        assert len(scr.query(UserReplyContainer)) == 1

        # Stop без вывода агента И при пустом input → cancel-with-undo
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

        def produced_output(self) -> bool:
            return False

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

        # Симулируем быструю отправку: pending-запись + user-widget.
        scr._on_user_text_submitted("исходный запрос")
        scr._last_user_message_id = "m_undo"
        scr._track_pending(
            display_text="исходный запрос", wire_content="исходный запрос"
        )
        scr._trace_mount(UserReplyContainer("исходный запрос", event_no=0))
        await pilot.pause()
        pum = (scr._store.get_task_meta(tid).get("tui") or {}).get(
            "pending_user_messages", []
        )
        assert len(pum) == 1  # запись записана

        # Быстрая отмена (агент ничего не выдал, пустой input) → откат.
        scr._cmd_stop()
        await pilot.pause()

        pum2 = (scr._store.get_task_meta(tid).get("tui") or {}).get(
            "pending_user_messages", []
        )
        assert pum2 == [], (
            f"pending должен быть очищен после cancel-with-undo, got {pum2}"
        )


@pytest.mark.asyncio
async def test_cancel_with_undo_removes_orphan_from_thread(tmp_path):
    """Быстрая отмена ставит заявку, а poll (когда тред idle) удаляет осиротевшую
    реплику из data_*.jsonl по её id."""
    pytest.importorskip("textual")
    from gc_core.thread_store import ThreadSession, load_thread_messages
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _FakeRunner:
        def __init__(self):
            self._running = True

        def is_running(self) -> bool:
            return self._running

        def produced_output(self) -> bool:
            return False

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
        ts = ThreadSession(
            threads_dir=str(tmp_path),
            system_id="cli",
            thread_id=tid,
            history_messages=[],
            known_message_keys=set(),
            persisted_messages_count=0,
            meta={},
            previous_response_id=None,
            backend_api_mode="chat_completions",
        )
        await ts.apersist_batch(
            [{"role": "user", "content": "отменяю", "id": msg_id}],
            previous_response_id=None,
        )

        app.active_runner = _FakeRunner()
        scr._on_user_text_submitted("отменяю")
        scr._last_user_message_id = msg_id
        scr._trace_mount(UserReplyContainer("отменяю", event_no=0))
        await pilot.pause()

        scr._cmd_stop()  # вывода нет + пустой input → cancel-with-undo
        await pilot.pause()

        # Немедленное удаление: реплики в треде уже нет (не ждём остановки worker'а).
        msgs, _, _ = load_thread_messages(str(tmp_path), "cli", tid)
        assert all(
            m.get("id") != msg_id for m in msgs
        )  # осиротевшая удалена сразу


@pytest.mark.asyncio
async def test_cancel_with_undo_no_stream_reset_flicker(tmp_path):
    """После немедленного удаления курсор трейса пересинхронизирован: следующий
    tail_events НЕ репортит stream_reset (иначе была бы полная перерисовка =
    моргание)."""
    pytest.importorskip("textual")
    from gc_core.thread_store import ThreadSession
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _FakeRunner:
        def __init__(self):
            self._running = True

        def is_running(self) -> bool:
            return self._running

        def produced_output(self) -> bool:
            return False

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
        await ThreadSession(
            threads_dir=str(tmp_path),
            system_id="cli",
            thread_id=tid,
            history_messages=[],
            known_message_keys=set(),
            persisted_messages_count=0,
            meta={},
            previous_response_id=None,
            backend_api_mode="chat_completions",
        ).apersist_batch(
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
    from gc_core.thread_store import ThreadSession, load_thread_messages
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
        await ThreadSession(
            threads_dir=str(tmp_path),
            system_id="cli",
            thread_id=tid,
            history_messages=[],
            known_message_keys=set(),
            persisted_messages_count=0,
            meta={},
            previous_response_id=None,
            backend_api_mode="chat_completions",
        ).apersist_batch(
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
    """Ядро отметило вывод прогона → НЕ cancel-with-undo, нормальная
    отмена: откат не заказан, исхода в индикаторе нет (S10 §3.3)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        running = True

        def is_running(self) -> bool:
            return self.running

        def produced_output(self) -> bool:
            return True  # агент уже что-то выдал

        def stop(self) -> None:
            self.running = False  # ход кончился сразу: итог ровный

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        scr._on_user_text_submitted("запрос")
        scr._last_user_message_id = "m_1"
        # Без паузы до стопа: тик без треда убрал бы индикатор «думаю».
        scr._show_thinking_indicator(state="thinking")
        scr._cmd_stop()
        await pilot.pause(0.5)
        assert scr._pending_orphan_cleanups == []
        assert scr._thinking is None


@pytest.mark.asyncio
async def test_cancel_with_undo_skipped_when_input_not_empty():
    """input.text != "" → НЕ cancel-with-undo, нормальная отмена: откат не
    заказан, исхода в индикаторе нет (S10 §3.3)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        running = True

        def is_running(self) -> bool:
            return self.running

        def produced_output(self) -> bool:
            return False

        def stop(self) -> None:
            self.running = False  # ход кончился сразу: итог ровный

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        scr._on_user_text_submitted("первый запрос")
        scr._last_user_message_id = "m_1"
        scr.query_one("#cmdinput").text = "новая печать"
        # Без паузы до стопа: тик без треда убрал бы индикатор «думаю».
        scr._show_thinking_indicator(state="thinking")
        scr._cmd_stop()
        await pilot.pause(0.5)
        assert scr._pending_orphan_cleanups == []
        assert scr._thinking is None


class _OutputRunner:
    """Раннер с флагом ядра «прогон что-то выдал»."""

    def __init__(self, produced: bool = False):
        self.running = True
        self.produced = produced

    def is_running(self) -> bool:
        return self.running

    def produced_output(self) -> bool:
        return self.produced

    def stop(self) -> None:
        self.running = False


@pytest.mark.asyncio
async def test_single_esc_does_not_undo_once_reasoning_streamed():
    """Мысли модели уже на экране (живой стрим, в тред ещё не записаны):
    ядро отметило вывод — откат отправки запрещён, хоть хвост и пуст."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.stream_sink import StreamSink

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        runner = _OutputRunner()
        pilot.app.active_runner = runner
        scr._on_user_text_submitted("запрос")
        scr._last_user_message_id = "m_1"
        assert scr._can_cancel_with_undo() is True
        sink = StreamSink()
        sink.push("reasoning", "думаю над задачей")
        pilot.app._stream_sink = sink
        runner.produced = True

        assert scr._can_cancel_with_undo() is False


@pytest.mark.asyncio
async def test_no_undo_after_leaving_the_thread_view():
    """Сменили тред — отправки этого вида нет, откатывать нечего."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _OutputRunner()
        scr._on_user_text_submitted("запрос")
        scr._last_user_message_id = "m_1"
        scr._leave_thread_view()

        assert scr._can_cancel_with_undo() is False
