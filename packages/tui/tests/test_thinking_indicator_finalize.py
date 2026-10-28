"""ThinkingIndicator — мгновенное гашение по submit_solution + sticky sentinel."""

from gc_testkit.develop_files import record_from_meta

from gc_testkit.tui_support import tui_settings
import json
import threading
import time
from pathlib import Path

import pytest
from gc_testkit.tui_support import wait_until


def _screen(pilot):
    return pilot.app.screen_stack[-1]


async def wait_for_finalized(pilot) -> None:
    """Дождаться sentinel _finalized, принудительно тикая _poll_active.

    Под нагрузкой полного suite interval 250мс Textual может не успевать;
    без ручного poll wait_until по _last_event_kind таймаутится."""

    def cond() -> bool:
        _screen(pilot)._poll_active()
        return _screen(pilot)._last_event_kind == "_finalized"

    await wait_until(pilot, cond, "submit_solution → sentinel _finalized")


async def wait_for_event_kind(pilot, kind: str) -> None:
    def cond() -> bool:
        _screen(pilot)._poll_active()
        return _screen(pilot)._last_event_kind == kind

    await wait_until(pilot, cond, f"_last_event_kind == {kind!r}")


async def wait_for_indicator_state(pilot, state: str) -> None:
    """Дождаться state индикатора после смены _last_event_kind (лаг 1 тик poll)."""
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    def cond() -> bool:
        _screen(pilot)._poll_active()
        indicators = pilot.app.query(ThinkingIndicator)
        return len(indicators) == 1 and indicators[0]._state == state

    await wait_until(pilot, cond, f"ThinkingIndicator._state == {state!r}")


def _submit_solution_record(
    *,
    seq: int = 1,
    ts: str = "2026-05-28T00:00:00+00:00",
    report: str = "Финальный ответ",
    comment: str = "",
    call_id: str = "call_ss_1",
    mid: str = "ss1",
) -> dict:
    """Минимальный record для submit_solution function_call в формате thread_store."""
    args = {"task_report": report}
    if comment:
        args["comment"] = comment
    return {
        "type": "message",
        "seq": seq,
        "ts_utc": ts,
        "payload": {
            "id": mid,
            "type": "function_call",
            "name": "submit_solution",
            "call_id": call_id,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def _policy_user_record(
    *,
    seq: int = 2,
    ts: str = "2026-05-28T00:00:01+00:00",
    inner: str = "Повторно вызови submit_solution с уточнением.",
    mid: str = "pol1",
) -> dict:
    """[policy] technical user-message от finish_gate feedback'а."""
    return {
        "type": "message",
        "seq": seq,
        "ts_utc": ts,
        "payload": {
            "id": mid,
            "role": "user",
            "content": f"<technical type='technical'>{inner}</technical>",
        },
    }


def _ask_human_record(
    *,
    seq: int = 1,
    ts: str = "2026-05-28T00:00:01+00:00",
    question: str = "Какой вариант выбрать: A, B или C?",
    call_id: str = "call_ah_1",
    mid: str = "ah1",
) -> dict:
    """ask_human function_call от агента — TUI смонтирует AskHumanBanner."""
    return {
        "type": "message",
        "seq": seq,
        "ts_utc": ts,
        "payload": {
            "id": mid,
            "type": "function_call",
            "name": "ask_human",
            "call_id": call_id,
            "arguments": json.dumps(
                {"question": question}, ensure_ascii=False
            ),
        },
    }


def _submit_solution_synthetic_output_record(
    *,
    seq: int = 2,
    ts: str = "2026-05-28T00:00:00.5+00:00",
    call_id: str = "call_ss_1",
    mid: str = "fco_ss_1",
) -> dict:
    """Синтетический tool_output для submit_solution (запись старого формата
    треда, писавшегося до перехода на живое исполнение turn_finish).

    Имеет `"tool": "submit_solution"` и `"synthetic": true` в output-JSON, НО
    НЕ имеет поля `name` на верхнем уровне — поэтому старый фильтр trace_mapper
    по `name == "submit_solution"` его пропускал и sentinel `_finalized` сбрасывался."""
    return {
        "type": "message",
        "seq": seq,
        "ts_utc": ts,
        "payload": {
            "id": mid,
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(
                {
                    "tool": "submit_solution",
                    "ok": True,
                    "status": "submitted",
                    "message": "Dialog turn finished. Waiting for new user questions.",
                    "error": None,
                    "synthetic": True,
                },
                ensure_ascii=False,
            ),
        },
    }


class _BlockingFake:
    """Fake executor: пишет переданные records в .jsonl и блокирует тред."""

    def __init__(self, records: list[dict], threads_dir: Path) -> None:
        self._records = records
        self._threads_dir = threads_dir
        self._evt = threading.Event()

    async def arun(self, messages, *, thread_id, **kw):
        # auto-create: attach + pending mount на UI-потоке — до записи .jsonl.
        time.sleep(0.4)
        data = self._threads_dir / f"data_cli_{thread_id}.jsonl"
        meta = self._threads_dir / f"meta_cli_{thread_id}.json"
        # user-сообщение пишется первым (для consistency с реальным flow)
        lines = [
            json.dumps(
                {
                    "type": "message",
                    "seq": 0,
                    "ts_utc": "2026-05-27T23:59:59+00:00",
                    "payload": {
                        "id": "u0",
                        "role": "user",
                        "content": messages[0],
                    },
                }
            )
            + "\n",
        ]
        for r in self._records:
            lines.append(json.dumps(r) + "\n")
        data.write_text("".join(lines), encoding="utf-8")
        meta.write_text(
            json.dumps({"thread_id": thread_id, "messages_count": len(lines)}),
            encoding="utf-8",
        )
        self._evt.wait(timeout=30.0)

    def stop_run(self) -> None:
        self._evt.set()

    def add_message(self, m) -> None:
        pass


@pytest.mark.asyncio
async def test_submit_solution_event_switches_indicator_to_validating(
    tmp_path,
):
    """submit_solution-event переключает индикатор в режим "validating"
    немедленно + _last_event_kind == '_finalized'. Без 1.5с задержки."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    fake = _BlockingFake(
        records=[_submit_solution_record(report="Готово, вот ответ.")],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "сделай что-нибудь"
        await pilot.press("enter")
        await wait_for_finalized(pilot)

        screen = _screen(pilot)
        # Индикатор НЕ удаляется — переключается в "validating" режим,
        # держится пока runner.is_running() (intent_guard / MCP cleanup).
        indicators = pilot.app.query(ThinkingIndicator)
        assert len(indicators) == 1
        assert indicators[0]._state == "validating"
        assert screen._last_event_kind == "_finalized"


@pytest.mark.asyncio
async def test_poll_active_keeps_validating_state_until_runner_exits(tmp_path):
    """После finalize индикатор остаётся в "validating" режиме на всех тиках
    поллинга, пока runner жив. Не переключается обратно на "thinking"."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    fake = _BlockingFake(
        records=[_submit_solution_record()],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "test"
        await pilot.press("enter")
        await wait_for_finalized(pilot)

        screen = _screen(pilot)
        assert screen._last_event_kind == "_finalized"
        assert pilot.app.active_runner.is_running()

        # Прогон через несколько тиков поллинга — индикатор остаётся validating.
        await pilot.pause(2.0)
        indicators = pilot.app.query(ThinkingIndicator)
        assert len(indicators) == 1
        assert indicators[0]._state == "validating"
        assert screen._last_event_kind == "_finalized"
        assert pilot.app.active_runner.is_running()


@pytest.mark.asyncio
async def test_synthetic_submit_solution_output_does_not_reset_sentinel(
    tmp_path,
):
    """Регрессия (запись старого формата треда, chat_completions):

    После submit_solution function_call в jsonl мог лежать синтетический
    function_call_output без поля `name` (до перехода на живое исполнение
    turn_finish). У record'а нет поля `name`, но output-JSON содержит
    `"tool": "submit_solution"`. Без расширенного фильтра в trace_mapper
    он мапился как kind='result' и затирал sentinel — индикатор re-mount'ился."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    fake = _BlockingFake(
        records=[
            _submit_solution_record(seq=1, call_id="call_ss_42"),
            _submit_solution_synthetic_output_record(
                seq=2, call_id="call_ss_42"
            ),
        ],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "test"
        await pilot.press("enter")
        await wait_for_finalized(pilot)

        screen = _screen(pilot)
        # Sentinel должен пережить синтетический tool_output.
        assert screen._last_event_kind == "_finalized"
        indicators = pilot.app.query(ThinkingIndicator)
        assert len(indicators) == 1
        assert indicators[0]._state == "validating"
        assert pilot.app.active_runner.is_running()

        # Даже через несколько тиков индикатор остаётся в validating.
        await pilot.pause(2.0)
        indicators = pilot.app.query(ThinkingIndicator)
        assert len(indicators) == 1
        assert indicators[0]._state == "validating"
        assert screen._last_event_kind == "_finalized"


@pytest.mark.asyncio
async def test_policy_event_after_finalized_switches_back_to_thinking(
    tmp_path,
):
    """После finalize прилёт [policy]-строки (gate-reject feedback) переключает
    индикатор обратно в "thinking": _last_event_kind переходит в 'system',
    _poll_active меняет state с validating на thinking."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    fake = _BlockingFake(
        records=[
            _submit_solution_record(seq=1),
            _policy_user_record(seq=2),
        ],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "test"
        await pilot.press("enter")
        # submit_solution и policy пишутся одним батчем → sentinel может не
        # «пойматься» между тиками; финальное состояние — system.
        await wait_for_event_kind(pilot, "system")
        await wait_for_indicator_state(pilot, "thinking")

        screen = _screen(pilot)
        assert screen._last_event_kind == "system"
        indicators = pilot.app.query(ThinkingIndicator)
        assert len(indicators) == 1
        assert indicators[0]._state == "thinking"


@pytest.mark.asyncio
async def test_new_user_send_clears_finalized(tmp_path):
    """После finalize отправка нового user-сообщения сбрасывает sentinel.

    Проверяем напрямую через _reset_thinking_timer (вызывается в трёх send-путях)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    fake = _BlockingFake(
        records=[_submit_solution_record()],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "first"
        await pilot.press("enter")
        await wait_for_finalized(pilot)

        screen = _screen(pilot)
        assert screen._last_event_kind == "_finalized"

        # Прямой вызов _reset_thinking_timer (как в _on_command_text_area_submitted).
        screen._reset_thinking_timer()
        assert screen._last_event_kind is None


@pytest.mark.asyncio
async def test_reset_render_state_clears_finalized(tmp_path):
    """_reset_render_state (новая задача / stream_reset) сбрасывает sentinel."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    fake = _BlockingFake(
        records=[_submit_solution_record()],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "first"
        await pilot.press("enter")
        await wait_for_finalized(pilot)

        screen = _screen(pilot)
        assert screen._last_event_kind == "_finalized"

        screen._reset_render_state()
        assert screen._last_event_kind is None


@pytest.mark.asyncio
async def test_ask_human_active_hides_thinking_indicator(tmp_path):
    """При активном ask_human (агент ждёт ответ юзера) индикатор «думаю…»
    обманывает — он не работает, банер ask_human уже говорит «нужен ответ».
    Индикатор должен быть скрыт пока ask_human_state не сброшен."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    fake = _BlockingFake(
        records=[_ask_human_record(question="Выбери: A или B?")],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "обсудим что-нибудь"
        await pilot.press("enter")
        # 1.0с — ask_human-record прочитан, banner смонтирован,
        # tui_state.ask_human_state выставлен.
        await pilot.pause(1.0)

        # Banner смонтирован → активен ask_human → индикатор скрыт.
        assert pilot.app.tui_state.ask_human_state is not None
        indicators = pilot.app.query(ThinkingIndicator)
        # Может быть либо не монтирован (= 0), либо смонтирован но с .hidden.
        if indicators:
            assert indicators[0].has_class("hidden")
        # Runner ещё работает (ждёт ответа на ask_human).
        assert pilot.app.active_runner.is_running()


@pytest.mark.asyncio
async def test_runner_not_running_shows_terminal_state_after_finalized(
    tmp_path,
):
    """Если runner.is_running() == False — индикатор не уничтожается, а
    переходит в состояние «done» (store сообщает status=completed, runner
    не был остановлен напрямую).

    Sentinel не должен мешать (path-coverage / defensive)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    fake = _BlockingFake(
        records=[_submit_solution_record()],
        threads_dir=Path(tmp_path),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "test"
        await pilot.press("enter")
        await wait_for_finalized(pilot)

        screen = _screen(pilot)
        assert screen._last_event_kind == "_finalized"

        # Заменяем store двойником, который возвращает status=completed,
        # чтобы idle-ветка гарантированно выдала state="done".
        class _CompletedStore:
            def get_record(self, tid):
                return record_from_meta({"tui": {"status": "completed"}})

            def update_tui_meta(self, tid, patch):
                pass

        screen._store = _CompletedStore()
        screen._cursor = (
            None  # skip tail_events — стор двойник не реализует его
        )

        # Симулируем выход runner-треда (без _stop_requested → outcome=completed→done).
        class _StoppedRunner:
            def is_running(self):
                return False

            _stop_requested = False
            _thread_kind = "run"

        pilot.app.active_runner = _StoppedRunner()
        pilot.app.last_runner_error = None
        pilot.app.last_stop_requested = False
        screen._was_running = True

        screen._poll_active()
        await pilot.pause(0.3)

        # Индикатор присутствует и в состоянии «done» (store вернул completed,
        # runner._stop_requested=False → не переопределяется).
        inds = pilot.app.query(ThinkingIndicator)
        assert len(inds) == 1
        assert inds[0]._state == "done"
