"""ThinkingIndicator — таймер анимации тикает, только пока строка видна."""

from gc_tui.widgets.thinking_indicator import ThinkingIndicator
from textual.app import App
from textual.timer import Timer


class _Host(App):
    def __init__(self, indicator: ThinkingIndicator) -> None:
        super().__init__()
        self._indicator = indicator

    def compose(self):
        yield self._indicator


def _spy(monkeypatch, name: str, widget: ThinkingIndicator) -> list:
    """Вызовы Timer.<name> именно для таймера анимации виджета."""
    calls: list = []
    original = getattr(Timer, name)

    def wrapper(self, *args, **kwargs):
        if self is widget._anim_timer:
            calls.append(name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Timer, name, wrapper)
    return calls


def test_set_state_before_mount_safe():
    w = ThinkingIndicator()
    w.set_state("validating")
    assert w._state == "validating"


async def test_live_ticks_coarse():
    w = ThinkingIndicator()
    ticks: list = []
    original = w._tick

    def counting() -> None:
        ticks.append(1)
        original()

    w._tick = counting
    async with _Host(w).run_test() as pilot:
        await pilot.pause(0.5)
        assert len(ticks) >= 1


async def test_hidden_pauses_and_show_resumes():
    w = ThinkingIndicator()
    async with _Host(w).run_test() as pilot:
        await pilot.pause()
        w.hide()
        assert w.has_class("hidden")
        assert w._anim_timer._active.is_set() is False
        w.show("thinking")
        assert not w.has_class("hidden")
        assert w._anim_timer._active.is_set() is True


async def test_live_state_change_while_hidden_keeps_paused():
    w = ThinkingIndicator()
    async with _Host(w).run_test() as pilot:
        await pilot.pause()
        w.hide()
        w.set_state("validating")
        assert w._anim_timer._active.is_set() is False


async def test_hidden_before_mount_starts_paused():
    w = ThinkingIndicator()
    w.hide()
    async with _Host(w).run_test() as pilot:
        await pilot.pause()
        assert w._anim_timer._active.is_set() is False
