from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings
from gc_tui.session_settings import (
    SESSION_KEYS,
    diff_vs_base,
    finish_gate_mode_of,
)


def test_session_keys_cover_toggle_keys():
    """Белый список знает оба тоггл-ключа — по путям через точку."""
    assert (
        SESSION_KEYS["tui.finish_gate_mode"].dotted == "tui.finish_gate_mode"
    )
    assert (
        SESSION_KEYS["tui.text_answer_mode"].dotted == "tui.text_answer_mode"
    )
    assert all(name == key.dotted for name, key in SESSION_KEYS.items())


def test_session_layer_sets_value_and_makes_it_explicit():
    """Переопределение ложится сессионным слоем и делает поле явным — иначе
    правило коллизии сочло бы значение неявным и могло вернуть не его."""
    from gc_core.settings import is_explicit

    base = make_settings({"tui.finish_gate_mode": "on"})
    loaded = base.with_overrides(
        {"tui.finish_gate_mode": "off"}, layer="session"
    )
    assert loaded.settings.tui.finish_gate_mode == "off"
    assert is_explicit(loaded.settings, "tui.finish_gate_mode")
    assert finish_gate_mode_of(loaded) == "off"


def test_session_layer_ignores_unknown_keys():
    """Ключ вне белого списка не применяется (переопределять можно не всё)."""
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"paths.threads_data_path": "/base"}))
    app.set_session_override("paths.threads_data_path", "/hacked")
    assert app.effective_settings().settings.paths.threads_data_path == "/base"


def test_diff_vs_base_returns_only_changed():
    base = make_settings(
        {"tui.finish_gate_mode": "on", "tui.text_answer_mode": "judge"}
    )
    overrides = {
        "tui.finish_gate_mode": "off",
        "tui.text_answer_mode": "judge",
    }
    assert diff_vs_base(base, overrides) == {"tui.finish_gate_mode": "off"}


def test_diff_vs_base_compares_effective_default_not_raw_value():
    """Рукописный (или старый) config.yml: tui.finish_gate_mode не задан, а глобальный
    agent.strict_turn_finish_validation выключен явно — эффективное умолчание файла
    'off'. Включение 'on' в сессии обязано считаться расхождением: иначе строка
    «Сохранить как умолчание» не появится никогда и записать это умолчание из UI
    нечем (по сырому значению поля расхождения не видно)."""
    base = make_settings(**{"agent.strict_turn_finish_validation": False})
    assert diff_vs_base(base, {"tui.finish_gate_mode": "on"}) == {
        "tui.finish_gate_mode": "on"
    }


def test_diff_vs_base_ignores_value_equal_to_effective_default():
    """Обратная сторона того же правила: сессия выставила ровно то, что и так
    действует — сохранять нечего, строки-действия быть не должно."""
    base = make_settings(**{"agent.strict_turn_finish_validation": False})
    assert diff_vs_base(base, {"tui.finish_gate_mode": "off"}) == {}


def test_every_toggle_key_is_session_overridable():
    """Гвоздь расширяемости: тоггл, чей ключ забыли внести в белый список, молча
    не применялся бы — сессионный слой игнорирует неизвестный ключ, исключения нет,
    строка продолжает рендерить старое значение (переключатель выглядит залипшим)."""
    from gc_tui.toggles import TOGGLES

    # Пресет-тогглы (/rigor) не пишут напрямую — dotted="" (спека §6, Task 4);
    # проверка адресует только single-key тогглы.
    assert [
        s.dotted
        for s in TOGGLES.values()
        if s.dotted and s.dotted not in SESSION_KEYS
    ] == []


def test_new_session_key_works_without_code_changes(monkeypatch):
    """Расширяемость: новый ключ в белом списке проходит весь путь наложения."""
    from gc_tui import session_settings as ss
    from gc_tui.app import TuiApp

    monkeypatch.setitem(
        ss.SESSION_KEYS,
        "agent.max_steps",
        ss.SessionKey("agent.max_steps"),
    )
    app = TuiApp(settings=tui_settings({"agent.max_steps": 3}))
    app.set_session_override("agent.max_steps", 5)
    assert app.effective_settings().settings.agent.max_steps == 5
    assert ss.diff_vs_base(
        make_settings({"agent.max_steps": 3}), {"agent.max_steps": 5}
    ) == {"agent.max_steps": 5}
