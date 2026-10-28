"""Zombie-recovery в ask_human-режиме — Фиксы C+D."""

from __future__ import annotations

from gc_testkit.tui_support import compact_thread, tui_settings
import json
from pathlib import Path

import pytest
from gc_tui.app import TuiApp
from gc_tui.models import TraceEvent


class _DeadRunner:
    """Stub `AgentRunner`, который представляется мёртвым (`is_running() == False`)."""

    def is_running(self) -> bool:
        return False

    def submit_ask_human_reply(self, text: str) -> None:
        raise AssertionError(
            "submit_ask_human_reply не должен вызываться на мёртвом runner"
        )

    def cancel_ask_human(self) -> None:
        raise AssertionError(
            "cancel_ask_human не должен вызываться на мёртвом runner"
        )


class _LiveRunner:
    def __init__(self):
        self.cancelled = 0
        self.submitted: list[str] = []

    def is_running(self) -> bool:
        return True

    def submit_ask_human_reply(self, text: str) -> None:
        self.submitted.append(text)

    def cancel_ask_human(self) -> None:
        self.cancelled += 1


def _mount_zombie_banner(screen) -> None:
    """Смонтировать banner вручную в state, эквивалентный replay-у с dangling
    ask_human-call (без матчинга live-runner-а)."""
    ev = TraceEvent(
        no=1,
        event_ts_utc_iso="2026-05-28T10:00:00+00:00",
        kind="ask_human",
        title="ask_human",
        summary="Готовы?",
        full_payload={},
        message_id="ah_zombie",
        call_id="call_zombie_1",
        tool_name="ask_human",
    )
    screen._on_ask_human_event(ev)


@pytest.mark.asyncio
async def test_esc_in_live_ask_human_clean_stops(tmp_path):
    """Esc в live ask_human → clean stop треда:
    - runner.stop() вызван (флаг stop_requested выставлен);
    - banner снят, ask_human_state сброшен;
    - ThinkingIndicator переходит в состояние «cancelled» (вместо [stopped]-строки в трейсе);
    - active_runner обнулён (clean-restart pattern)."""
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen

        stop_called: list[bool] = []
        unblock_called: list[bool] = []

        class _LiveStoppable:
            def __init__(self):
                self._running = True

            def is_running(self) -> bool:
                return self._running

            def stop(self) -> None:
                self._running = False
                stop_called.append(True)

            def cancel_ask_human(self) -> None:
                # mechanical unblock-stdin, не cancel в семантике юзера
                unblock_called.append(True)

            def submit_ask_human_reply(self, text: str) -> None:
                raise AssertionError(
                    "submit_ask_human_reply не должен вызываться при Esc-stop"
                )

        runner = _LiveStoppable()
        app.active_runner = runner
        # Смонтировать индикатор (как при живом прогоне) и banner.
        screen._show_thinking_indicator(state="thinking")
        _mount_zombie_banner(screen)
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        # runner.stop() вызван — флаг stop_requested выставлен в реальном executor-е
        assert stop_called == [True], "runner.stop() должен быть вызван"
        # cancel_ask_human вызван для разблокировки stdin (agent-thread в input())
        assert unblock_called == [True], (
            "cancel_ask_human должен быть вызван (unblock stdin)"
        )
        # UI-state очищен
        assert screen._ask_human_banner is None, "banner должен быть снят"
        assert app.tui_state.ask_human_state is None, (
            "ask_human_state должен быть сброшен"
        )
        assert app.active_runner is None, (
            "active_runner должен быть обнулён (clean-restart pattern)"
        )
        # Индикатор в состоянии cancelled (вместо [stopped]-строки в трейсе).
        assert screen._thinking is not None, (
            "ThinkingIndicator должен остаться (cancelled-state)"
        )
        assert screen._thinking._state == "cancelled", (
            f"ThinkingIndicator должен быть в состоянии 'cancelled', got {screen._thinking._state!r}"
        )


@pytest.mark.asyncio
async def test_after_esc_stop_user_can_continue_thread(tmp_path):
    """После Esc-stop в live ask_human юзер пишет любой текст + Enter →
    стартует новый run на том же thread_id через runner.start()."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({}),
    )
    (tmp_path / "threads").mkdir(exist_ok=True)
    thread_id = "ahstop00000000000000000000000001"
    app.tui_state.active_task_id = thread_id

    started: list[dict] = []

    class _CapturingRunner:
        def __init__(self):
            self._running = False  # после stop живой не считаем

        def is_running(self) -> bool:
            return self._running

        def start(self, *, query: str, thread_id: str, **kwargs) -> bool:
            self._running = True
            started.append({"query": query, "thread_id": thread_id})
            return True

        def stop(self) -> None:
            self._running = False

        def cancel_ask_human(self) -> None:
            pass

        def submit_ask_human_reply(self, text: str) -> None:
            pass

        def add_message(self, message: dict) -> None:
            pass

    # Pre-state: ask_human уже остановлен (banner снят, ask_human_state=None).
    # _executor_factory подменён на dummy, чтобы get_runner не строил реальный
    # AgentExecutor; active_runner проставляем напрямую _CapturingRunner-ом —
    # get_runner вернёт его как есть (см. tui/app.py::get_runner).
    cap = _CapturingRunner()
    app._executor_factory = lambda: object()  # type: ignore[assignment]
    from gc_tui.services.stores.file_store import FileTaskStore

    async with app.run_test() as pilot:
        screen = app.screen
        screen._active_task_id = thread_id
        screen._store = FileTaskStore(
            threads_dir=str(tmp_path / "threads"), system="cli"
        )
        app.active_runner = cap
        await pilot.pause()

        # Симулируем submit: юзер пишет текст + Enter (как продолжение треда).
        from gc_tui.widgets.command_input import CommandTextArea

        inp = screen.query_one("#cmdinput")
        inp.text = "продолжаем"
        await screen.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="продолжаем")
        )
        await pilot.pause()

        assert started, "runner.start должен быть вызван для продолжения треда"
        assert started[0]["thread_id"] == thread_id, (
            f"runner.start должен быть с thread_id={thread_id!r}, got {started[0]['thread_id']!r}"
        )
        assert "продолжаем" in started[0]["query"], (
            f"query должен содержать текст юзера, got {started[0]['query']!r}"
        )


@pytest.mark.asyncio
async def test_esc_in_zombie_state_unmounts_banner_and_falls_through(tmp_path):
    """Zombie ask_human-state (нет/мёртв runner): Esc не съедается —
    banner снят, Esc-state-machine отработала (пустой input → модалка выхода)."""
    from gc_tui.screens.confirm import ConfirmScreen

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        app.active_runner = _DeadRunner()
        _mount_zombie_banner(screen)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        # Banner снят.
        assert screen._ask_human_banner is None
        # tui_state.ask_human_state сброшен.
        assert app.tui_state.ask_human_state is None
        # Esc-state-machine отработала: input пуст, run нет → открыта модалка выхода.
        modal = next(
            (s for s in app.screen_stack if isinstance(s, ConfirmScreen)), None
        )
        assert (
            modal is not None
            and modal.query_one("#confirm-dialog").border_title == "Выход"
        ), (
            "общая Esc-state-machine должна была открыть модалку выхода после снятия banner-а"
        )


def _write_thread_dangling_ask_human(
    tmp_path: Path, *, thread_id: str
) -> Path:
    """Persisted-тред: ask_human-call БЕЗ парного function_call_output."""
    threads = tmp_path / "threads"
    threads.mkdir(exist_ok=True)
    data_path = threads / f"data_cli_{thread_id}.jsonl"
    meta_path = threads / f"meta_cli_{thread_id}.json"
    records = [
        {"role": "user", "content": "запусти"},
        {
            "id": "ah_call_dangling",
            "type": "function_call",
            "name": "ask_human",
            "call_id": "call_dangling_1",
            "arguments": json.dumps({"question": "Подтверждаете?"}),
        },
    ]
    with open(data_path, "w", encoding="utf-8") as f:
        for i, payload in enumerate(records, start=1):
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "thread_id": thread_id,
                        "system": "cli",
                        "seq": i,
                        "ts": f"2026-05-28T10:0{i}:00+00:00",
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    meta_path.write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-05-28T10:10:00+00:00",
                "messages_count": len(records),
                "tui": {},
            }
        ),
        encoding="utf-8",
    )
    return threads


@pytest.mark.asyncio
async def test_attach_dangling_ask_human_notes_interrupted_question(
    tmp_path,
):
    """Attach старого треда с dangling ask_human-call → banner НЕ остаётся
    на экране, под вопросом — строка «прервано» в его рамке."""
    thread_id = "ahdangling000000000000000000000a"
    threads_dir = _write_thread_dangling_ask_human(
        tmp_path, thread_id=thread_id
    )
    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        # Нет live-агента (runner мог быть прогрет на старте через warm, но не запущен).
        assert app.active_runner is None or not app.active_runner.is_running()
        screen.attach_active_task(thread_id)
        await pilot.pause()
        assert screen._ask_human_banner is None
        assert app.tui_state.ask_human_state is None
        # «прервано» — строкой `└` в рамке вопроса (спека P3 §3), вопрос —
        # полным текстом из persisted ask_human-call payload (Фикс F).
        from gc_tui.widgets.trace_row import QuestionRow, TraceRow

        (question,) = screen.query(QuestionRow)
        assert str(question._content) == "? Подтверждаете?"
        assert [b.plain for b in question._consequences] == [
            "прервано — продолжите сообщением или Esc"
        ]
        trace_text = " ".join(
            f"{getattr(w, '_prefix', '')}{getattr(w, '_content', '')}"
            for w in screen.query(TraceRow)
        )
        assert "спрашивал" not in trace_text


# --- компакция: ask_human-плашка не висит для уже отвеченного вопроса ---


def _ah_envelope_lines(tid, payloads):
    lines = [
        json.dumps(
            {
                "type": "message",
                "thread_id": tid,
                "system": "cli",
                "seq": i,
                "ts": f"2026-06-05T10:0{i}:00+00:00",
                "payload": p,
            },
            ensure_ascii=False,
        )
        for i, p in enumerate(payloads, 1)
    ]
    return "\n".join(lines) + "\n"


def _ah_dialog_summary_payload(text):
    return {
        "role": "user",
        "id": "m_sum",
        "content": (
            '<technical type="dialog_summary">Summarized context from '
            f"previous dialogue.\n<summary>{text}</summary></technical>"
        ),
    }


def _ah_call_payload(question, call_id):
    return {
        "id": "ah_call",
        "type": "function_call",
        "name": "ask_human",
        "call_id": call_id,
        "arguments": json.dumps({"question": question}),
    }


@pytest.mark.asyncio
async def test_attach_compacted_answered_ask_human_no_remnant(tmp_path):
    """Компакция: ask_human-call в архивном сегменте, его output суммаризован
    (не отдельная запись), диалог продолжился (summary + agent после вопроса).
    Attach НЕ должен показывать remnant 'прервано' — вопрос отвечён (репро #2)."""
    tid = "ahcompact0000000000000000000000a"
    (
        tmp_path / f"data_cli_{tid}__pre_summary__20260605_100000.jsonl"
    ).write_text(
        _ah_envelope_lines(
            tid,
            [
                {"id": "u1", "role": "user", "content": "запусти задачу"},
                _ah_call_payload("Подтверждаете?", "call_ah_x"),
            ],
        ),
        encoding="utf-8",
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        _ah_envelope_lines(
            tid,
            [
                _ah_dialog_summary_payload("вопрос подтверждён, продолжаем"),
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "Продолжаю работу.",
                },
            ],
        ),
        encoding="utf-8",
    )
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "updated_at": "x",
                "messages_count": 2,
                "last_summary_archive_suffix": "20260605_100000",
                "tui": {},
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = app.screen
        # Нет live-агента (runner мог быть прогрет на старте через warm, но не запущен).
        assert app.active_runner is None or not app.active_runner.is_running()
        scr.attach_active_task(tid)
        await pilot.pause()
        assert scr._ask_human_banner is None
        assert app.tui_state.ask_human_state is None
        from gc_tui.widgets.trace_row import TraceRow

        trace_text = " ".join(
            f"{getattr(w, '_prefix', '')}{getattr(w, '_content', '')}"
            for w in scr.query(TraceRow)
        )
        assert "прервано" not in trace_text


@pytest.mark.asyncio
async def test_stream_reset_compacted_answered_clears_stale_banner(tmp_path):
    """Симптом #1: post-compaction stream_reset не оставляет залипшую ask_human-
    плашку для уже отвеченного вопроса (иначе ввод уходит в stdin 'вникуда')."""
    tid = "ahreset000000000000000000000000a"
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        _ah_envelope_lines(
            tid,
            [
                {"id": "u1", "role": "user", "content": "запусти"},
                _ah_call_payload("Подтверждаете?", "call_ah_x"),
            ],
        ),
        encoding="utf-8",
    )
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "updated_at": "t0",
                "messages_count": 2,
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    app.active_runner = (
        _LiveRunner()
    )  # живой агент ждёт → attach оставит баннер
    async with app.run_test() as pilot:
        scr = app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.05)
        assert scr._ask_human_banner is not None  # живой вопрос — баннер есть

        # компакция ядром: straddle-layout (call в архиве, summary+продолжение в live)
        compact_thread(
            str(tmp_path),
            "cli",
            tid,
            [
                _ah_dialog_summary_payload("вопрос подтверждён, продолжаем"),
                {"id": "a1", "role": "assistant", "content": "Продолжаю."},
            ],
        )
        await pilot.pause(0.4)  # poll → stream_reset → reconcile

        assert scr._ask_human_banner is None
        assert app.tui_state.ask_human_state is None


@pytest.mark.asyncio
async def test_enter_in_zombie_continues_thread_as_new_user_message(tmp_path):
    """После recovery-снятия banner-а в attach-е юзер может ввести любой
    текст + Enter — он улетит как обычное user-сообщение (через runner.start),
    тред продолжится. Pending user-реплика на экране есть."""
    thread_id = "ahdangling000000000000000000000b"
    threads_dir = _write_thread_dangling_ask_human(
        tmp_path, thread_id=thread_id
    )

    started: list[dict] = []

    class _CapturingRunner(_LiveRunner):
        def start(self, *, query: str, thread_id: str, **kwargs) -> bool:
            started.append({"query": query, "thread_id": thread_id})
            return True

        def is_running(self) -> bool:
            # после start считаем что побежал.
            return bool(started)

    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    app._executor_factory = lambda: (
        object()
    )  # чтобы get_runner не строил реальный AgentExecutor
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        # Replace runner-фабрику: подменим в app, чтобы get_runner вернул наш capturing.
        cap = _CapturingRunner()
        app.active_runner = cap
        # Banner снят recovery-блоком. ask_human_state == None — submit пойдёт обычным путём.
        from gc_tui.widgets.command_input import CommandTextArea

        inp = screen.query_one("#cmdinput")
        inp.text = "продолжаем"
        await screen.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="продолжаем")
        )
        await pilot.pause()
        assert started, "runner.start должен быть вызван для продолжения треда"
        assert started[0]["thread_id"] == thread_id
        from gc_tui.widgets.user_reply import UserReplyContainer

        assert list(screen.query(UserReplyContainer))


@pytest.mark.asyncio
async def test_reconcile_keeps_active_ask_human_banner(tmp_path):
    """Гвард к Task 5: reconcile в live-потоке НЕ снимает активный баннер
    (живой раннер ждёт, продолжения нет) — иначе юзер не смог бы ответить."""
    pytest.importorskip("textual")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = app.screen

        class _Live:
            def is_running(self):
                return True

        app.active_runner = _Live()
        _mount_zombie_banner(scr)
        await pilot.pause()
        assert scr._ask_human_banner is not None
        scr._reconcile_ask_human_after_render(allow_remnant=False)
        assert scr._ask_human_banner is not None  # активный сохранён


@pytest.mark.asyncio
async def test_reconcile_without_question_row_falls_back_to_line(tmp_path):
    """Строки вопроса в ленте нет (баннер ребёнка) — «прервано» не теряется:
    отдельной строкой, как до следствий в рамке."""
    pytest.importorskip("textual")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = app.screen
        _mount_zombie_banner(scr)
        scr._ask_human_question_row.remove()
        scr._ask_human_question_row = None
        await pilot.pause()
        scr._reconcile_ask_human_after_render(allow_remnant=True)
        await pilot.pause()
        from gc_tui.widgets.trace_row import TraceRow

        texts = [str(w._content) for w in scr.query(TraceRow)]
        assert "прервано — продолжите сообщением или Esc" in texts


@pytest.mark.asyncio
async def test_reconcile_unmounts_superseded_ask_human_banner(tmp_path):
    """Перекрытый ask_human (после вопроса есть событие = продолжение диалога) →
    reconcile снимает баннер (наш skip-output из Task 1 = kind=result)."""
    pytest.importorskip("textual")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = app.screen
        _mount_zombie_banner(scr)
        await pilot.pause()
        scr._task_events.append(
            TraceEvent(
                no=1,
                event_ts_utc_iso="2026-06-20T00:00:00+00:00",
                kind="ask_human",
                title="ask_human",
                summary="Готовы?",
                full_payload={},
                message_id="ah",
                call_id="call_zombie_1",
                tool_name="ask_human",
            )
        )
        scr._task_events.append(
            TraceEvent(
                no=2,
                event_ts_utc_iso="2026-06-20T00:00:00+00:00",
                kind="result",
                title="",
                summary="ok",
                full_payload={},
                message_id="r",
            )
        )
        scr._reconcile_ask_human_after_render(allow_remnant=False)
        assert scr._ask_human_banner is None  # перекрытый снят


@pytest.mark.asyncio
async def test_live_poll_keeps_active_banner_despite_flush_output(tmp_path):
    """Регресс-гвард к 4a9d9e8 (убран live-reconcile): в живом потоке приход
    флаш-output'а не-блокирующего тула ПОСЛЕ ask_human (тот же ход, агент ещё ждёт)
    НЕ должен снимать активный баннер. До фикса live-reconcile видел событие после
    вопроса → считал диалог продолженным → сносил ЖИВОЙ баннер → агент висел зомби."""
    tid = "ahliveflush00000000000000000000a"
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        _ah_envelope_lines(
            tid,
            [
                {"id": "u1", "role": "user", "content": "запусти"},
                _ah_call_payload("Подтверждаете?", "call_ah_x"),
            ],
        ),
        encoding="utf-8",
    )
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "updated_at": "t0",
                "messages_count": 2,
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    app.active_runner = _LiveRunner()  # живой агент ждёт ответа на ask_human
    async with app.run_test() as pilot:
        scr = app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.05)
        assert scr._ask_human_banner is not None  # живой вопрос — баннер есть

        # Через poll прилетает output не-блокирующего тула (session_memory) ПОСЛЕ
        # ask_human — ровно то, что флаш кладёт в поток. Агент всё ещё ждёт.
        (tmp_path / f"data_cli_{tid}.jsonl").write_text(
            _ah_envelope_lines(
                tid,
                [
                    {"id": "u1", "role": "user", "content": "запусти"},
                    _ah_call_payload("Подтверждаете?", "call_ah_x"),
                    {
                        "id": "fco_sm",
                        "type": "function_call_output",
                        "call_id": "call_sm",
                        "output": json.dumps(
                            {"tool": "session_memory", "ok": True}
                        ),
                    },
                ],
            ),
            encoding="utf-8",
        )
        (tmp_path / f"meta_cli_{tid}.json").write_text(
            json.dumps(
                {
                    "thread_id": tid,
                    "updated_at": "t1",
                    "messages_count": 3,
                }
            ),
            encoding="utf-8",
        )
        await pilot.pause(0.4)  # poll → live-render (путь без reconcile)

        assert scr._ask_human_banner is not None, (
            "активный баннер НЕ должен сниматься в live при флаш-output'е после ask_human"
        )
        assert (
            app.tui_state.ask_human_state is not None
        )  # состояние диалога цело
