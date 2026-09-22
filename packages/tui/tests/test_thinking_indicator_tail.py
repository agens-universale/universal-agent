# tests/tui/test_thinking_indicator_tail.py
from gc_tui.widgets.thinking_indicator import ThinkingIndicator


class _FakeSink:
    def __init__(self, units, state=None):
        self._units = units
        # state param kept for backward compat but no longer used
    def progress_units(self):
        return self._units
    def reasoning_state(self):
        # legacy method, not used by new implementation
        return (None, None)


class _StubClock:
    def __init__(self, live: bool, episode: float) -> None:
        self._live = live
        self._episode = episode
    def live(self) -> bool:
        return self._live
    def episode(self) -> float:
        return self._episode


def _plain(ind):
    return ind._render_text().plain


def test_elapsed_uses_cyrillic_s():
    ind = ThinkingIndicator(started_at=0.0, state="thinking", clock=lambda: 8.0)
    assert "(8с" in _plain(ind)
    assert "(8s" not in _plain(ind)


def test_processing_state_label():
    """state=processing → «сверяюсь…» (ответ дан, судья сверяет его перед закрытием)."""
    ind = ThinkingIndicator(started_at=0.0, state="processing", clock=lambda: 5.0)
    p = _plain(ind)
    assert "сверяюсь…" in p
    assert "(5с" in p
    assert "думаю" not in p


def test_processing_breathes_dot_brightness():
    """«сверяюсь…»: одна точка • статична по форме, а её ЦВЕТ дышит по кадрам
    (тёмно-серая рампа) — плавно, без морф-спиннера «думаю…» и без смены формы."""
    from gc_tui.widgets.thinking_indicator import _PROCESSING_SHADES
    ind = ThinkingIndicator(started_at=0.0, state="processing", clock=lambda: 1.0)
    glyphs, colors = set(), set()
    for f in range(40):
        ind._frame = f
        t = ind._render_text()
        glyphs.add(t.plain[0])            # первый символ строки = глиф
        colors.add(next(str(s.style) for s in t.spans if t.plain[s.start:s.end] == "•"))
    assert glyphs == {"•"}                            # форма не меняется
    assert not (glyphs & set("·∙▴◂▾▸✦✧+"))            # ни морф, ни старая растущая точка
    assert len(colors) > 1                            # цвет дышит
    assert colors <= set(_PROCESSING_SHADES)          # из тёмно-серой рампы


def test_processing_word_is_dim_not_accent():
    """Слово «сверяюсь…» — серым (dim), чтобы отличаться от accent-«думаю…»."""
    from gc_tui.widgets.thinking_indicator import _ACCENT, _DIM
    t = ThinkingIndicator(started_at=0.0, state="processing", clock=lambda: 1.0)._render_text()
    word_at = t.plain.index("сверяюсь")
    styles = [s.style for s in t.spans if s.start <= word_at < s.end]
    assert _DIM in styles
    assert _ACCENT not in styles


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
        sink=_FakeSink(3300),
        reasoning_clock=_StubClock(live=False, episode=6.0),
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


def test_reasoning_tag_shown_when_enabled():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 8.0,
        sink=_FakeSink(1200), reasoning_tag=True,
        reasoning_clock=_StubClock(live=True, episode=0.0),
    )
    p = _plain(ind)
    assert "· размышление" in p
    assert "↓ 1.2k токенов" in p   # counter still present, tag after it


def test_reasoning_tag_hidden_by_default():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 8.0,
        sink=_FakeSink(1200, ("thinking", None)),  # reasoning_tag defaults False
    )
    assert "размышление" not in _plain(ind)


def test_reasoning_tag_not_shown_in_done_state():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 31.0,
        sink=_FakeSink(3300), reasoning_tag=True,
        reasoning_clock=_StubClock(live=False, episode=6.0),
    )
    p = _plain(ind)
    assert "размышление" not in p          # done → no "размышление"
    assert "обдумал за 6с" in p            # done tag still shown
