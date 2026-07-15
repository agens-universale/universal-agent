"""Проводка confirm-канала (T3): is_tui + tui_confirm в tool-ctx.

Тест 1 (unit): _present_confirm вызывает call_from_thread с callable, который
достаёт show_confirm_banner у текущего экрана.

Тест 2 (integration, run_test): полная цепочка
  confirm(…) → call_from_thread → show_confirm_banner → [y] → submit_confirm_reply
              → confirm() возвращает "once".

Тест 3 (re-entrancy): два последовательных confirm в одном раннере:
  decline → approve; канал должен быть re-entrant.

Тест 4 (arrow nav): ↓ + Enter выбирает выделенный вариант (session).
"""
from __future__ import annotations

import threading
import types
import pytest


# ── unit-тест ────────────────────────────────────────────────────────────────

def test_present_confirm_calls_call_from_thread_and_show_banner(monkeypatch):
    """_present_confirm на агент-потоке вызывает call_from_thread;
    тот при синхронном вызове немедленно зовёт show_confirm_banner на TraceScreen."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp()
    calls: list[tuple] = []

    fake_screen = types.SimpleNamespace(
        show_confirm_banner=lambda t, q, f: calls.append((t, q, f)),
    )
    monkeypatch.setattr(app, "_main_screen", lambda: fake_screen, raising=False)

    # call_from_thread подменяем синхронной версией (немедленно выполняет fn).
    captured: list = []

    def sync_call_from_thread(fn):
        result = fn()
        captured.append(fn)
        return result

    monkeypatch.setattr(app, "call_from_thread", sync_call_from_thread, raising=False)

    assert app._present_confirm("Title", "Question?", [("Path", "/x")]) is True

    assert len(calls) == 1
    assert calls[0] == ("Title", "Question?", [("Path", "/x")])  # fields проброшены в баннер
    assert len(captured) == 1


# ── integration-тест через run_test() ────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_full_chain_via_run_test():
    """Полная цепочка: AgentRunner.make_tui_confirm → _present_confirm
    → call_from_thread → show_confirm_banner → клавиша y → submit_confirm_reply
    → confirm() возвращает True."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.agent_runner import AgentRunner
    from tui.widgets.confirm_banner import ConfirmBanner

    app = TuiApp(config={"model": "M"})
    # Фабрика не нужна — executor не строим.
    app._executor_factory = lambda: None

    # Строим реальный AgentRunner с заглушкой-executor.
    stub_executor = types.SimpleNamespace(stop_run=lambda: None, add_message=lambda m: None)
    runner = AgentRunner(executor=stub_executor)
    app.active_runner = runner

    # Замыкание confirm — строим ДО run_test, чтобы использовать app._present_confirm
    # (метод определён на TuiApp и доступен до монтирования).
    confirm_fn = runner.make_tui_confirm(app._present_confirm)

    result: list[bool | BaseException] = []

    async with app.run_test() as pilot:
        # Запускаем confirm на worker-потоке — он блокируется до ответа.
        def _worker():
            try:
                res = confirm_fn(title="Тест", question="Удалить файл?", fields=[])
                result.append(res)
            except BaseException as exc:
                result.append(exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        # Даём call_from_thread + show_confirm_banner время смонтировать баннер.
        await pilot.pause(0.3)

        # Баннер должен быть смонтирован на экране.
        banners = list(pilot.app.query(ConfirmBanner))
        assert len(banners) == 1, f"ожидали 1 ConfirmBanner, получили {len(banners)}"

        # Пользователь нажимает «y» → TraceScreen._answer_confirm → runner.submit_confirm_reply("once").
        await pilot.press("y")
        await pilot.pause(0.2)

        # Ждём завершения worker-потока.
        t.join(timeout=3.0)
        assert not t.is_alive(), "worker-поток не завершился вовремя"
        assert result, "confirm не вернул результат"
        val = result[0]
        if isinstance(val, BaseException):
            raise val
        assert val == "once"

        # Баннер снят.
        banners_after = list(pilot.app.query(ConfirmBanner))
        assert len(banners_after) == 0


# ── re-entrancy: два последовательных confirm ─────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_reentrant_decline_then_approve():
    """Два последовательных confirm в одном AgentRunner: первый отклонён (n),
    второй принят (y). Проверяет отсутствие stale-_confirm_event-бага."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.agent_runner import AgentRunner
    from tui.widgets.confirm_banner import ConfirmBanner

    app = TuiApp(config={"model": "M"})
    app._executor_factory = lambda: None

    stub_executor = types.SimpleNamespace(stop_run=lambda: None, add_message=lambda m: None)
    runner = AgentRunner(executor=stub_executor)
    app.active_runner = runner

    confirm_fn = runner.make_tui_confirm(app._present_confirm)

    results: list[bool | BaseException] = []

    async with app.run_test() as pilot:
        # Confirm #1 — отклоняем (n).
        def _worker1():
            try:
                res = confirm_fn(title="T1", question="Первый?", fields=[])
                results.append(res)
            except BaseException as exc:
                results.append(exc)

        t1 = threading.Thread(target=_worker1, daemon=True)
        t1.start()
        await pilot.pause(0.3)

        banners = list(pilot.app.query(ConfirmBanner))
        assert len(banners) == 1, f"confirm #1: ожидали 1 баннер, получили {len(banners)}"

        await pilot.press("n")
        await pilot.pause(0.2)

        t1.join(timeout=3.0)
        assert not t1.is_alive(), "worker1 не завершился"
        assert len(results) == 1
        assert results[0] == "deny", f"confirm #1 должен вернуть 'deny', вернул {results[0]}"

        banners = list(pilot.app.query(ConfirmBanner))
        assert len(banners) == 0, "баннер должен быть снят после confirm #1"

        # Confirm #2 — принимаем (y).
        def _worker2():
            try:
                res = confirm_fn(title="T2", question="Второй?", fields=[])
                results.append(res)
            except BaseException as exc:
                results.append(exc)

        t2 = threading.Thread(target=_worker2, daemon=True)
        t2.start()
        await pilot.pause(0.3)

        banners = list(pilot.app.query(ConfirmBanner))
        assert len(banners) == 1, f"confirm #2: ожидали 1 баннер, получили {len(banners)}"

        await pilot.press("y")
        await pilot.pause(0.2)

        t2.join(timeout=3.0)
        assert not t2.is_alive(), "worker2 не завершился"
        assert len(results) == 2
        assert results[1] == "once", f"confirm #2 должен вернуть 'once', вернул {results[1]}"

        banners = list(pilot.app.query(ConfirmBanner))
        assert len(banners) == 0, "баннер должен быть снят после confirm #2"


# ── arrow navigation: ↑/↓ + Enter ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_arrow_down_then_enter_selects_session():
    """↓ сдвигает выделение с Y ([Y]) на [S] (session); Enter выбирает выделенный → "session"."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.agent_runner import AgentRunner
    from tui.widgets.confirm_banner import ConfirmBanner

    app = TuiApp(config={"model": "M"})
    app._executor_factory = lambda: None

    stub_executor = types.SimpleNamespace(stop_run=lambda: None, add_message=lambda m: None)
    runner = AgentRunner(executor=stub_executor)
    app.active_runner = runner

    confirm_fn = runner.make_tui_confirm(app._present_confirm)
    result: list[bool | BaseException] = []

    async with app.run_test() as pilot:
        def _worker():
            try:
                res = confirm_fn(title="Навигация", question="Выберите?", fields=[])
                result.append(res)
            except BaseException as exc:
                result.append(exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        await pilot.pause(0.3)

        banners = list(pilot.app.query(ConfirmBanner))
        assert len(banners) == 1

        # Стрелка вниз — переключает на [S] (selected=1).
        await pilot.press("down")
        await pilot.pause(0.05)
        banner = banners[0]
        assert banner.selected == 1, f"ожидали selected=1, получили {banner.selected}"

        # Enter — подтверждает выделенный ([S] → "session").
        await pilot.press("enter")
        await pilot.pause(0.2)

        t.join(timeout=3.0)
        assert not t.is_alive()
        assert result and result[0] == "session", f"ожидали 'session', получили {result}"


@pytest.mark.asyncio
async def test_confirm_enter_default_selects_y():
    """Enter без навигации → выделен Y (selected=0) → True."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.agent_runner import AgentRunner
    from tui.widgets.confirm_banner import ConfirmBanner

    app = TuiApp(config={"model": "M"})
    app._executor_factory = lambda: None

    stub_executor = types.SimpleNamespace(stop_run=lambda: None, add_message=lambda m: None)
    runner = AgentRunner(executor=stub_executor)
    app.active_runner = runner

    confirm_fn = runner.make_tui_confirm(app._present_confirm)
    result: list[bool | BaseException] = []

    async with app.run_test() as pilot:
        def _worker():
            try:
                res = confirm_fn(title="Навигация", question="Выберите?", fields=[])
                result.append(res)
            except BaseException as exc:
                result.append(exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        await pilot.pause(0.3)

        banners = list(pilot.app.query(ConfirmBanner))
        assert len(banners) == 1
        assert banners[0].selected == 0, "Y должен быть выделен по умолчанию"

        # Enter без навигации → Y (True).
        await pilot.press("enter")
        await pilot.pause(0.2)

        t.join(timeout=3.0)
        assert not t.is_alive()
        assert result and result[0] == "once", f"ожидали 'once', получили {result}"
