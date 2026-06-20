"""Проводка confirm-канала (T3): is_tui + tui_confirm в tool-ctx.

Тест 1 (unit): _present_confirm вызывает call_from_thread с callable, который
достаёт show_confirm_banner у текущего экрана.

Тест 2 (integration, run_test): полная цепочка
  confirm(…) → call_from_thread → show_confirm_banner → [y] → submit_confirm_reply
              → confirm() возвращает True.
"""
from __future__ import annotations

import threading
import types
import pytest


# ── unit-тест ────────────────────────────────────────────────────────────────

def test_present_confirm_calls_call_from_thread_and_show_banner(monkeypatch):
    """_present_confirm на агент-потоке вызывает call_from_thread;
    тот при синхронном вызове немедленно зовёт show_confirm_banner."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp()
    calls: list[tuple[str, str]] = []

    # Мок screen с show_confirm_banner — патчим через property на классе.
    fake_screen = types.SimpleNamespace(
        show_confirm_banner=lambda t, q: calls.append((t, q)),
    )
    monkeypatch.setattr(TuiApp, "screen", property(lambda self: fake_screen), raising=False)

    # call_from_thread подменяем синхронной версией (немедленно выполняет fn).
    captured: list = []

    def sync_call_from_thread(fn):
        fn()
        captured.append(fn)

    monkeypatch.setattr(app, "call_from_thread", sync_call_from_thread, raising=False)

    app._present_confirm("Title", "Question?", [])

    assert len(calls) == 1
    assert calls[0] == ("Title", "Question?")
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

        # Пользователь нажимает «y» → TraceScreen._answer_confirm(True) → runner.submit_confirm_reply(True).
        await pilot.press("y")
        await pilot.pause(0.2)

        # Ждём завершения worker-потока.
        t.join(timeout=3.0)
        assert not t.is_alive(), "worker-поток не завершился вовремя"
        assert result, "confirm не вернул результат"
        val = result[0]
        if isinstance(val, BaseException):
            raise val
        assert val is True

        # Баннер снят.
        banners_after = list(pilot.app.query(ConfirmBanner))
        assert len(banners_after) == 0
