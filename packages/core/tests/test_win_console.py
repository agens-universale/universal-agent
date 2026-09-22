"""preserve_console_mode: вокруг subprocess сохраняем/восстанавливаем режим
консоли Windows.

Запуск дочернего процесса на Windows гасит ENABLE_MOUSE_INPUT родительской
консоли → Textual-TUI теряет мышь (клик/скролл уходят в выделение терминала,
экран «залипает»). Контекст-менеджер возвращает режим после subprocess.
Кросс-платформенный no-op вне Windows / без консоли.
"""

from gc_core.utils import win_console as wc


def test_restores_saved_mode(monkeypatch):
    calls = {}
    monkeypatch.setattr(wc, "_get_input_mode", lambda: 0x1B7)
    monkeypatch.setattr(wc, "_set_input_mode", lambda m: calls.__setitem__("set", m))
    with wc.preserve_console_mode():
        pass
    assert calls["set"] == 0x1B7  # режим восстановлен после блока


def test_noop_when_no_console(monkeypatch):
    calls = {}
    monkeypatch.setattr(wc, "_get_input_mode", lambda: None)  # консоли нет
    monkeypatch.setattr(wc, "_set_input_mode", lambda m: calls.__setitem__("set", m))
    with wc.preserve_console_mode():
        pass
    assert "set" not in calls  # нечего восстанавливать — не трогаем


def test_restores_even_on_exception(monkeypatch):
    calls = {}
    monkeypatch.setattr(wc, "_get_input_mode", lambda: 0x1B7)
    monkeypatch.setattr(wc, "_set_input_mode", lambda m: calls.__setitem__("set", m))
    try:
        with wc.preserve_console_mode():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert calls["set"] == 0x1B7  # восстановили даже при исключении


def test_get_input_mode_none_off_windows(monkeypatch):
    # На не-Windows нет консольного API → None (no-op путь).
    monkeypatch.setattr(wc.sys, "platform", "linux")
    assert wc._get_input_mode() is None
