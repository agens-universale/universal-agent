# tests/tui/test_thinking_indicator_tail.py
from tui.widgets.thinking_indicator import ThinkingIndicator


class _FakeSink:
    def __init__(self, units, state):
        self._units, self._state = units, state
    def progress_units(self):
        return self._units
    def reasoning_state(self):
        return self._state


def _plain(ind):
    return ind._render_text().plain


def test_elapsed_uses_cyrillic_s():
    ind = ThinkingIndicator(started_at=0.0, state="thinking", clock=lambda: 8.0)
    assert "(8с" in _plain(ind)
    assert "(8s" not in _plain(ind)


def test_tail_thinking_no_word_but_counter():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 8.0,
        sink=_FakeSink(1200, ("thinking", None)),
    )
    p = _plain(ind)
    assert "↓ 1.2k токенов" in p
    assert "размышление" not in p   # word dropped; reasoning shown via the StreamBlock line


def test_tail_done_shows_obdumal():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 31.0,
        sink=_FakeSink(3300, ("done", 6.0)),
    )
    p = _plain(ind)
    assert "↓ 3.3k токенов" in p
    assert "обдумал за 6с" in p


def test_no_tail_without_sink():
    ind = ThinkingIndicator(started_at=0.0, state="thinking", clock=lambda: 40.0)
    p = _plain(ind)
    assert "токенов" not in p
    assert "размышление" not in p


def test_no_reasoning_tag_when_state_none():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 40.0,
        sink=_FakeSink(4100, (None, None)),
    )
    p = _plain(ind)
    assert "↓ 4.1k токенов" in p
    assert "размышление" not in p
    assert "обдумал" not in p
