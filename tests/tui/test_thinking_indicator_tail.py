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


def test_processing_state_label():
    """state=processing → «обрабатываю…» (ответ дан, прогон ещё доделывает)."""
    ind = ThinkingIndicator(started_at=0.0, state="processing", clock=lambda: 5.0)
    p = _plain(ind)
    assert "обрабатываю…" in p
    assert "(5с" in p
    assert "думаю" not in p


def test_processing_uses_pulse_glyph_not_morph():
    """«обрабатываю…» дышит пульсом из трёх точек, а не ярким морфом «думаю…»."""
    ind = ThinkingIndicator(started_at=0.0, state="processing", clock=lambda: 1.0)
    seen = set()
    for f in range(40):
        ind._frame = f
        seen.add(_plain(ind)[0])          # первый символ строки = первая точка
    assert seen <= set("·∙•")             # только пульс-кадры
    assert not (seen & set("▴◂▾▸✦✧+"))    # морф-кадры не появляются


def test_processing_pulse_travels_across_three_dots():
    """Акцент `•` бежит по очереди по трём позициям (бегущая волна ·∙•)."""
    ind = ThinkingIndicator(started_at=0.0, state="processing", clock=lambda: 1.0)
    glyphs = set()
    for f in range(40):
        ind._frame = f
        glyphs.add(_plain(ind).split(" ", 1)[0])   # глиф = первый токен (3 ячейки)
    assert glyphs <= {"•∙·", "∙•∙", "·∙•"}
    assert all(len(g) == 3 for g in glyphs)
    # `•` побывал в каждой из трёх позиций
    assert {g.index("•") for g in glyphs if "•" in g} == {0, 1, 2}


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


def test_reasoning_tag_shown_when_enabled():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 8.0,
        sink=_FakeSink(1200, ("thinking", None)), reasoning_tag=True,
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
        sink=_FakeSink(3300, ("done", 6.0)), reasoning_tag=True,
    )
    p = _plain(ind)
    assert "размышление" not in p          # done → no "размышление"
    assert "обдумал за 6с" in p            # done tag still shown
