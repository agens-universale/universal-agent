"""Входы режима `auto` в TUI: флаг запуска, набранная команда, смена конфига и
профиля в сессии, открытие треда. Недоступный автовыбор везде называет причину
текстом ядра и работает уровнем default; вернулась доступность — снова auto."""

import pytest

from gc_core.runtime_notices import (
    clear_runtime_notices,
    collect_runtime_notices,
)
from gc_testkit import run_coro

_ONE_MODEL_MESSAGE = "автовыбор моделей недоступен: во всех уровнях одна модель — работает уровень default"
_BACKEND = (
    "backend:\n  api_mode: chat_completions\n"
    '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
)
_ONE = "models:\n  mid: {}\nslots:\n  default: mid\n"
_TWO = (
    "models:\n  fast: {}\n  mid: {}\nslots:\n  light: fast\n  default: mid\n"
)


@pytest.fixture(autouse=True)
def _clean_runtime_notices():
    clear_runtime_notices()
    yield
    clear_runtime_notices()


def _auto_runtime_notices():
    return [
        n.message
        for n in collect_runtime_notices()
        if n.code == "auto_routing_unavailable"
    ]


def _app_on_disk(tmp_path, monkeypatch, body, *, model_override=None):
    """TuiApp на настоящем config.yml с диска — так же, как его собирает `gc-ua`
    (`load_settings` + флаг уровня через `apply_cli_overrides`)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.yml"
    cfg.write_text(body, encoding="utf-8")
    from gc_core.config import apply_cli_overrides, load_settings
    from gc_tui.app import TuiApp

    loaded = load_settings(str(cfg))
    if model_override:
        loaded = apply_cli_overrides(
            loaded, extra_overrides={"model": model_override}
        )
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=loaded,
        model_override=model_override,
    )
    app.set_config_path(str(cfg))
    return app, cfg


def _bare_trace_screen(monkeypatch, app):
    from gc_tui.screens.main import TraceScreen

    scr = TraceScreen()
    monkeypatch.setattr(
        type(scr), "app", property(lambda self: app), raising=False
    )
    return scr


def test_level_flag_on_a_one_model_config_explains_in_the_card_and_runs_default(
    tmp_path, monkeypatch
):
    from gc_tui.toggles import effective_toggle_names, toggle_state

    app, _cfg = _app_on_disk(
        tmp_path, monkeypatch, _BACKEND + _ONE, model_override="auto"
    )

    notices = [
        n.message
        for n in app.collect_notices()
        if n.code == "auto_routing_unavailable"
    ]
    assert notices == [_ONE_MODEL_MESSAGE]
    assert app.auto_mode_active() is False
    assert toggle_state(app, "level") == "default"
    assert "auto" not in effective_toggle_names(app, "level")


def test_typed_level_auto_names_the_reason_when_auto_is_unavailable(
    tmp_path, monkeypatch
):
    app, _cfg = _app_on_disk(tmp_path, monkeypatch, _BACKEND + _ONE)
    hints = []
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)

    run_coro(scr._dispatch_command("level auto"))

    assert hints == [
        "/level auto — автовыбор моделей недоступен: во всех уровнях одна модель"
    ]
    assert (
        app.effective_settings().settings.model is None
    )  # набор ничего не переключает


def test_typed_level_auto_on_max_names_the_reason_without_claiming_default(
    tmp_path, monkeypatch
):
    """Набранное `/level auto` ничего не переключает: на уровне max ответ называет
    причину и не выдаёт default за действующий уровень."""
    app, _cfg = _app_on_disk(
        tmp_path,
        monkeypatch,
        _BACKEND + _TWO + "agent:\n  auto_routing: false\n",
    )
    app.set_session_override("model", "max")
    hints = []
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)

    run_coro(scr._dispatch_command("level auto"))

    assert hints == [
        "/level auto — автовыбор моделей недоступен: выключен настройкой auto_routing"
    ]
    assert app.effective_settings().settings.model == "max"


def test_typed_level_auto_when_available_keeps_the_info_hint(
    tmp_path, monkeypatch
):
    app, _cfg = _app_on_disk(tmp_path, monkeypatch, _BACKEND + _TWO)
    hints = []
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)

    run_coro(scr._dispatch_command("level auto"))

    assert len(hints) == 1 and "/level: default" in hints[0]


@pytest.mark.parametrize(
    "broken, reason",
    [
        (_ONE, "во всех уровнях одна модель"),
        (
            _TWO + "agent:\n  auto_routing: false\n",
            "выключен настройкой auto_routing",
        ),
    ],
    ids=["second_model_removed", "auto_routing_off"],
)
def test_config_change_making_auto_unavailable_notices_once_and_recovers(
    tmp_path, monkeypatch, broken, reason
):
    from gc_tui.toggles import toggle_state

    app, cfg = _app_on_disk(tmp_path, monkeypatch, _BACKEND + _TWO)
    app.set_session_override("model", "auto")  # auto выбран в сессии палитрой
    assert app.auto_mode_active() is True

    cfg.write_text(
        _BACKEND + broken, encoding="utf-8"
    )  # Настройки → Модели / рубильник
    app.reload_config()

    assert (
        app.auto_mode_active() is False
        and toggle_state(app, "level") == "default"
    )
    assert _auto_runtime_notices() == [
        f"автовыбор моделей недоступен: {reason} — работает уровень default"
    ]

    app.reload_config()  # перечитка того же сломанного конфига
    assert len(_auto_runtime_notices()) == 1  # повтор того же текста погашен

    cfg.write_text(_BACKEND + _TWO, encoding="utf-8")  # обратный переход
    app.reload_config()

    assert (
        app.auto_mode_active() is True and toggle_state(app, "level") == "auto"
    )
    assert len(_auto_runtime_notices()) == 1  # обратный переход нотиса не шлёт


def test_saved_level_auto_is_explained_by_the_startup_card_only(
    tmp_path, monkeypatch
):
    """`level: auto` сохранён в самом config.yml — причину уже называет карточка
    стартовых уведомлений; runtime-нотис её не дублирует."""
    app, cfg = _app_on_disk(
        tmp_path, monkeypatch, _BACKEND + _TWO + "model: auto\n"
    )
    cfg.write_text(_BACKEND + _ONE + "model: auto\n", encoding="utf-8")

    app.reload_config()

    assert _auto_runtime_notices() == []
    notices = [
        n.message
        for n in app.collect_notices()
        if n.code == "auto_routing_unavailable"
    ]
    assert notices == [_ONE_MODEL_MESSAGE]


def test_session_profile_with_one_call_kit_notices_the_reason(
    tmp_path, monkeypatch
):
    app, _cfg = _app_on_disk(tmp_path, monkeypatch, _BACKEND + _TWO)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "solo.yml").write_text(
        "slots:\n  light: mid\n  default: mid\n  max: mid\n", encoding="utf-8"
    )
    app.set_session_override("model", "auto")

    app.switch_session_profile("solo")

    assert app.auto_mode_active() is False
    assert _auto_runtime_notices() == [_ONE_MODEL_MESSAGE]


def test_opening_a_thread_saved_in_auto_notices_the_reason(
    tmp_path, monkeypatch
):
    app, _cfg = _app_on_disk(tmp_path, monkeypatch, _BACKEND + _ONE)

    app.load_session_overrides(
        {"model": "auto"}
    )  # мета треда: сессия шла в auto

    assert app.auto_mode_active() is False
    assert _auto_runtime_notices() == [_ONE_MODEL_MESSAGE]


def test_opening_a_thread_with_a_broken_session_profile_off_auto_warns_once(
    tmp_path, monkeypatch
):
    """Мета треда хранит профиль, файла которого нет, уровень не `auto`: одно
    предупреждение о снятом профиле — без второго, о временно недоступном."""
    app, _cfg = _app_on_disk(tmp_path, monkeypatch, _BACKEND + _TWO)

    app.load_session_overrides({"profile": "gone"})  # порядок открытия треда:
    app.sanitize_session_profile()  # набор из меты, затем санация

    codes = [n.code for n in collect_runtime_notices()]
    assert codes.count("session_profile_stale") == 1
    assert "session_profile_remerge_failed" not in codes
