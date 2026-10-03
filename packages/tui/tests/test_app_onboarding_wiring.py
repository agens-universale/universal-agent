"""Textual-level onboarding wiring tests.

Verify that TuiApp shows OnboardingScreen when _onboarding_mode is set,
and TraceScreen otherwise.
"""

from gc_testkit.tui_support import tui_settings
import pytest

# Marker: these tests require the textual extra (dev-tui).
textual = pytest.importorskip("textual")


# ---------------------------------------------------------------------------
# Test 1: unconfigured + auto mode → OnboardingScreen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_app_shows_onboarding(tmp_path):
    """When _onboarding_mode='auto' and config is unconfigured, the app must
    show OnboardingScreen as the default screen."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "http://your-backend:PORT/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
        "model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "auto"  # выставляется в tui/__init__.py

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen), (
            f"Expected OnboardingScreen, got {type(app.screen).__name__}"
        )


# ---------------------------------------------------------------------------
# Test 2: configured (no _onboarding_mode) → NOT OnboardingScreen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configured_app_no_onboarding(tmp_path):
    """When _onboarding_mode is None (default), the app must not show OnboardingScreen."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    # _onboarding_mode stays None by default

    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, OnboardingScreen), (
            f"Expected TraceScreen (not OnboardingScreen), got {type(app.screen).__name__}"
        )


# ---------------------------------------------------------------------------
# Test 3: review mode → OnboardingScreen with review_all=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_mode_shows_onboarding_review_all(tmp_path):
    """When _onboarding_mode='review', OnboardingScreen must have review_all=True."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "review"
    app._onboarding_config_path = tmp_path / "config.yml"

    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, OnboardingScreen), (
            f"Expected OnboardingScreen, got {type(scr).__name__}"
        )
        assert scr._review_all is True, "review mode must set review_all=True"


# ---------------------------------------------------------------------------
# Test 4: commit (auto) → switch from OnboardingScreen (base) to TraceScreen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_onboarding_auto_switches_to_main(tmp_path, monkeypatch):
    """_finish_onboarding в auto-режиме заменяет базовый OnboardingScreen на TraceScreen
    без краха (switch_screen на базовом экране требует заглушку result-callback)."""
    from gc_core import config as cfg_mod
    from gc_core import thread_store as ts_mod
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.screens.onboarding import OnboardingScreen

    configured = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    from gc_core.config_modules.profiles import ProfileMeta
    from dataclasses import replace

    from gc_testkit.settings import make_settings

    monkeypatch.setattr(
        cfg_mod,
        "load_settings",
        lambda p=None, **kw: replace(
            make_settings(configured),
            profile_meta=ProfileMeta(active_name=None, ref=None, mtime_set={}),
        ),
    )
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d, **kw: d)
    monkeypatch.setattr(
        ts_mod, "resolve_threads_dir", lambda c: str(tmp_path / "t")
    )

    unconf = {
        "completions_url": "http://your-backend:PORT/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
        "model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(unconf)
    )
    app._onboarding_mode = "auto"
    app._onboarding_config_path = str(tmp_path / "config.yml")

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen)
        app._finish_onboarding()  # финал онбординга
        await pilot.pause()
        assert isinstance(
            app.screen, TraceScreen
        )  # перешли на главный без краха


# ---------------------------------------------------------------------------
# Test 5: commit (review / --init-config) → тоже переходит на TraceScreen,
# а не выходит (единообразно с первым запуском — «завершил настройку → работаешь»)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_onboarding_review_switches_to_main(
    tmp_path, monkeypatch
):
    """review-режим (--init-config) на финале ТОЖЕ стартует агента (TraceScreen),
    а не self.exit() — как первый запуск. Esc остаётся путём «выйти не сохраняя»."""
    from gc_core import config as cfg_mod
    from gc_core import thread_store as ts_mod
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.screens.onboarding import OnboardingScreen

    configured = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    from gc_core.config_modules.profiles import ProfileMeta
    from dataclasses import replace

    from gc_testkit.settings import make_settings

    monkeypatch.setattr(
        cfg_mod,
        "load_settings",
        lambda p=None, **kw: replace(
            make_settings(configured),
            profile_meta=ProfileMeta(active_name=None, ref=None, mtime_set={}),
        ),
    )
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d, **kw: d)
    monkeypatch.setattr(
        ts_mod, "resolve_threads_dir", lambda c: str(tmp_path / "t")
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(dict(configured)),
    )
    app._onboarding_mode = "review"
    app._onboarding_config_path = str(tmp_path / "config.yml")

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen)
        exited = []
        app.exit = lambda *a, **k: exited.append(True)
        app._finish_onboarding()  # финал review-онбординга
        await pilot.pause()
        assert isinstance(
            app.screen, TraceScreen
        )  # стартовал агент, а не выход
        assert exited == []  # self.exit() НЕ вызван


# ---------------------------------------------------------------------------
# Test 6: мастер настройки модален — F1/F4 не открывают экраны поверх него
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onboarding_is_modal_for_help_and_notices(tmp_path):
    """Поверх мастера настройки не открываются ни справка (F1), ни /notices (F4).

    Иначе справка рекламирует клавиши, которые в этот момент — тихие no-op'ы
    (F2/F3/F5/Ctrl+N работают только с главного экрана), а /notices собирается по
    ЕЩЁ НЕ ЗАПИСАННОМУ конфигу и предупреждает юзера о том, что он прямо сейчас
    заполняет.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "http://your-backend:PORT/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
        "model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "auto"

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen)
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen), (
            "F1 открыл справку поверх мастера"
        )
        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen), (
            "F4 открыл /notices поверх мастера"
        )


# ---------------------------------------------------------------------------
# Test 7: признак ступени «без профиля» доезжает до экрана мастера
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_mode_forwards_the_deactivation_flag(tmp_path):
    """`tui/__init__.py` ставит признак атрибутом приложения — приложение обязано
    донести его до экрана: иначе финал мастера не снимет указатель и упрётся в
    отказ записи (стек следующего старта поднимет сломанный профиль)."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "review"
    app._onboarding_config_path = tmp_path / "config.yml"
    app._onboarding_deactivate_profile = True

    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, OnboardingScreen)
        assert scr._deactivate_profile is True


@pytest.mark.asyncio
async def test_onboarding_does_not_deactivate_by_default(tmp_path):
    """Отрицательный сторож: обычный первый запуск (признак не ставился) — мастер
    к указателю профиля не прикасается."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "http://your-backend:PORT/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
        "model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "auto"

    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, OnboardingScreen)
        assert scr._deactivate_profile is False


@pytest.mark.asyncio
async def test_review_mode_forwards_the_degradation_message(tmp_path):
    """Текст деградации загрузки ставит атрибутом та же точка входа — приложение
    обязано донести его до экрана: под мастером глобальная навигация заблокирована,
    и другого адреса показа у текста нет."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "review"
    app._onboarding_config_path = tmp_path / "config.yml"
    app._onboarding_degraded = 'config.yml не читается с профилем "work"'

    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, OnboardingScreen)
        assert scr._degraded_note == app._onboarding_degraded


@pytest.mark.asyncio
async def test_onboarding_has_no_degradation_message_by_default(tmp_path):
    """Отрицательный сторож: обычный первый запуск — деградации не было, и
    показывать нечего."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "completions_url": "http://your-backend:PORT/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
        "model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "auto"

    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, OnboardingScreen)
        assert scr._degraded_note is None


@pytest.mark.asyncio
async def test_finish_onboarding_survives_a_config_that_stopped_loading(
    tmp_path, monkeypatch
):
    """Финал онбординга перечитывает конфиг — и обязан делать это ЗА ГЕЙТОМ, тем же,
    что держит окно на битом config.yml (`reload_config_if_changed`): причина в
    `/notices`, окно на прежней базе.

    Файл ломается в зазоре между записью визарда и финалом (соседнее окно, редактор
    с автосохранением, оборванная запись) — редко, но цена несоразмерна поводу:
    неперехваченный отказ загрузчика уходит крэшем Textual поверх настройки, которую
    человек только что закончил, и терять её ради диагностики не за что."""
    from gc_core.runtime_notices import collect_runtime_notices
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.screens.onboarding import OnboardingScreen

    monkeypatch.chdir(tmp_path)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend: [this is not, valid: yaml:", encoding="utf-8"
    )
    cfg = {
        "completions_url": "http://your-backend:PORT/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
        "model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._onboarding_mode = "auto"
    app._onboarding_config_path = str(cfg_file)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen)

        app._finish_onboarding()  # финал на сломавшемся файле
        await pilot.pause()

        assert isinstance(
            app.screen, TraceScreen
        )  # человек на главном, а не в трейсбеке
        codes = [n.code for n in collect_runtime_notices()]
        assert "config_reload_failed" in codes, codes
