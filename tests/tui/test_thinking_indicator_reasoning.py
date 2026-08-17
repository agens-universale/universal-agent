"""ThinkingIndicator: хвост reasoning читается из ReasoningClock, токены — из sink."""
from gc_tui.widgets.thinking_indicator import ThinkingIndicator


class _StubClock:
    def __init__(self, live: bool, episode: float) -> None:
        self._live = live
        self._episode = episode

    def live(self) -> bool:
        return self._live

    def episode(self) -> float:
        return self._episode


class _StubSink:
    def __init__(self, units: int = 0) -> None:
        self._u = units

    def progress_units(self) -> int:
        return self._u


def _render(*, clock_live, clock_episode, reasoning_tag=True, units=0) -> str:
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 0.0,
        sink=_StubSink(units), reasoning_tag=reasoning_tag,
        reasoning_clock=_StubClock(clock_live, clock_episode),
    )
    return str(ind.render())


def test_live_reasoning_shows_razmyshlenie():
    out = _render(clock_live=True, clock_episode=0.0)
    assert "размышление" in out
    assert "обдумал" not in out


def test_not_live_with_episode_shows_obdumal():
    out = _render(clock_live=False, clock_episode=3.0)
    assert "обдумал за 3с" in out
    assert "размышление" not in out


def test_not_live_subsecond_episode_shows_bare_obdumal():
    # 0 < episode < 0.5 → round в 0 → «обдумал» без «за 0с»
    out = _render(clock_live=False, clock_episode=0.3)
    assert "· обдумал" in out
    assert "за 0с" not in out
    assert "обдумал за" not in out


def test_reasoning_tag_off_suppresses_live_phrase():
    out = _render(clock_live=True, clock_episode=0.0, reasoning_tag=False)
    assert "размышление" not in out


def test_not_live_zero_episode_shows_nothing_extra():
    out = _render(clock_live=False, clock_episode=0.0)
    assert "размышление" not in out
    assert "обдумал" not in out


def test_tokens_still_from_sink():
    out = _render(clock_live=True, clock_episode=0.0, units=186)
    assert "токенов" in out


def test_no_clock_no_reasoning_tail():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 0.0,
        sink=_StubSink(0), reasoning_tag=True, reasoning_clock=None,
    )
    out = str(ind.render())
    assert "размышление" not in out and "обдумал" not in out
