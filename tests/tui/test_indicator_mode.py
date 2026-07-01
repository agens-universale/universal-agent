"""Выбор режима строки-индикатора в _poll_active.

Краш раннера (err) должен иметь приоритет → терминальный ✗ Ошибка сразу,
не дожидаясь reaping воркера / falling-edge (иначе при быстром «продолжай»
статус теряется, а индикатор уничтожается в idle-ветке)."""
from tui.screens.main import select_indicator_mode as M


def test_err_takes_precedence_over_running():
    assert M(err="boom", is_running=True, ask_human=False, confirm=False) == "failed"
    assert M(err="boom", is_running=False, ask_human=False, confirm=False) == "failed"


def test_active_when_running_without_human_wait():
    assert M(err=None, is_running=True, ask_human=False, confirm=False) == "active"


def test_hide_on_human_wait():
    assert M(err=None, is_running=True, ask_human=True, confirm=False) == "hide"
    assert M(err=None, is_running=False, ask_human=False, confirm=True) == "hide"


def test_terminal_when_idle():
    assert M(err=None, is_running=False, ask_human=False, confirm=False) == "terminal"
