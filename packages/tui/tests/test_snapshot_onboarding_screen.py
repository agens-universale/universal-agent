"""Онбординг — шаг «Адрес API-сервера» (первый шаг мастера, поле ввода).

Два окна: на обычном карточка влезает целиком (лого, тэглайн, степпер, сводка,
поле), рамка обнимает её, полоса подсказок — сразу под нижней гранью; на низком
— Р19: рамка упирается в потолок (`ScrollFrameMixin._sync_frame_cap`), скроллбар
идёт ВНУТРИ рамки, футер остаётся в кадре, а в подсказках появляется клавиша
прокрутки (её называют, только пока есть что прокручивать).

Экран поднимается тем же путём, что в проде: незаполненный конфиг +
`_onboarding_mode` (его ставит `tui/__init__.py`) → `TuiApp.get_default_screen`
отдаёт `OnboardingScreen`. Шаг адреса ни в сеть, ни в subprocess не ходит
(`fetch`/`mcp` нужны шагам модели и MCP) — подменять на этом шаге нечего.
"""

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_testkit.tui_support import tui_settings, wait_until


def _unconfigured() -> dict:
    """Конфиг из пакетного шаблона — все три поля ещё заглушки (…_SENTINEL):
    ровно то состояние, в котором прод сам поднимает мастер настройки."""
    from gc_core import onboarding

    return {
        "backend.completions_url": onboarding.URL_SENTINEL,
        "backend.chat_completion_headers": {
            "Authorization": onboarding.TOKEN_SENTINEL
        },
        "agent.model": onboarding.MODEL_SENTINEL,
    }


def _app(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    # Стартовый авто-чек обновления — сеть, а его находка ещё и всплывает тостом
    # поверх мастера: снимок ловил бы то с тостом, то без.
    monkeypatch.setattr(TuiApp, "_start_auto_update_check", lambda self: None)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_unconfigured()),
    )
    app._onboarding_mode = "auto"  # ставит tui/__init__.py
    app._onboarding_config_path = str(tmp_path / "config.yml")
    return app


async def _ready(pilot):
    """Дождаться первого шага: тело монтируется в on_mount, а подсказка о
    прокрутке досчитывается уже после раскладки (call_after_refresh)."""
    from textual.widgets import Input
    from gc_tui.screens.onboarding import OnboardingScreen

    await wait_until(
        pilot,
        lambda: (
            isinstance(pilot.app.screen, OnboardingScreen)
            and bool(pilot.app.screen.query(Input))
        ),
        "onboarding step mounted",
    )
    await pilot.pause()
    await pilot.pause()


def test_onboarding_endpoint_step_snapshot(
    snap_compare, tmp_path, monkeypatch
):
    """Обычное окно: карточка целиком, рамка по содержимому, футер под ней."""
    app = _app(tmp_path, monkeypatch)

    async def setup(pilot):
        await _ready(pilot)

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)


def test_onboarding_endpoint_step_low_snapshot(
    snap_compare, tmp_path, monkeypatch
):
    """Низкое окно — Р19: рамка завершена и упёрлась в потолок, скроллбар внутри
    неё, футер в кадре и с подсказкой о прокрутке."""
    app = _app(tmp_path, monkeypatch)

    async def setup(pilot):
        await _ready(pilot)

    assert snap_compare(app, terminal_size=(100, 12), run_before=setup)
