from gc_tui.session_settings import SESSION_KEYS, apply_overrides, diff_vs_base


def test_session_keys_cover_toggle_keys():
    """Белый список знает оба тоггл-ключа и их плоские имена."""
    assert SESSION_KEYS["tui.finish_gate_mode"].flat == "tui_finish_gate_mode"
    assert SESSION_KEYS["tui.text_answer_mode"].flat == "tui_text_answer_mode"


def test_apply_overrides_sets_flat_key_and_explicit_flag():
    """Переопределение кладётся в плоский ключ; для finish_gate_mode взводится
    флаг явности — иначе правило коллизии сочтёт значение неявным."""
    cfg = {
        "tui_finish_gate_mode": "on",
        "tui_finish_gate_mode_explicit": False,
    }
    apply_overrides(cfg, {"tui.finish_gate_mode": "off"})
    assert cfg["tui_finish_gate_mode"] == "off"
    assert cfg["tui_finish_gate_mode_explicit"] is True


def test_apply_overrides_ignores_unknown_keys():
    """Ключ вне белого списка не применяется (переопределять можно не всё)."""
    cfg = {"threads_data_path": "/base"}
    apply_overrides(cfg, {"agent.threads_data_path": "/hacked"})
    assert cfg["threads_data_path"] == "/base"


def test_diff_vs_base_returns_only_changed():
    base = {"tui_finish_gate_mode": "on", "tui_text_answer_mode": "judge"}
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
    нечем (схемный дефолт плоского ключа тоже 'on', сравнение по сырому значению
    расхождения не видит)."""
    base = {
        "tui_finish_gate_mode": "on",
        "tui_finish_gate_mode_explicit": False,
        "strict_turn_finish_explicit_false": True,
    }
    assert diff_vs_base(base, {"tui.finish_gate_mode": "on"}) == {
        "tui.finish_gate_mode": "on"
    }


def test_diff_vs_base_ignores_value_equal_to_effective_default():
    """Обратная сторона того же правила: сессия выставила ровно то, что и так
    действует — сохранять нечего, строки-действия быть не должно."""
    base = {
        "tui_finish_gate_mode": "on",
        "tui_finish_gate_mode_explicit": False,
        "strict_turn_finish_explicit_false": True,
    }
    assert diff_vs_base(base, {"tui.finish_gate_mode": "off"}) == {}


def test_every_toggle_key_is_session_overridable():
    """Гвоздь расширяемости: тоггл, чей ключ забыли внести в белый список, молча
    не применялся бы — apply_overrides игнорирует неизвестный ключ, исключения нет,
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

    monkeypatch.setitem(
        ss.SESSION_KEYS,
        "tui.fake_mode",
        ss.SessionKey("tui.fake_mode", "tui_fake_mode", None),
    )
    cfg = {"tui_fake_mode": "a"}
    ss.apply_overrides(cfg, {"tui.fake_mode": "b"})
    assert cfg["tui_fake_mode"] == "b"
    assert ss.diff_vs_base({"tui_fake_mode": "a"}, {"tui.fake_mode": "b"}) == {
        "tui.fake_mode": "b"
    }
