"""select_active_substate — под-состояние TUI-индикатора в режиме active."""

from tui.screens.main import select_active_substate

_SOFT = {"agent", "system"}


def _sub(**kw):
    base = dict(
        summarizing=False,
        finalizing=False,
        last_event_kind="agent",
        since_last=0.0,
        soft_hide_kinds=_SOFT,
    )
    base.update(kw)
    return select_active_substate(**base)


def test_finalizing_gives_validating():
    # Финальная проза принята → «завершаю…», даже если событие висит >1.5с.
    assert _sub(finalizing=True, since_last=5.0) == "validating"


def test_finalized_sentinel_gives_validating():
    assert _sub(last_event_kind="_finalized") == "validating"


def test_summarizing_wins_over_finalizing():
    assert _sub(summarizing=True, finalizing=True) == "compressing"


def test_soft_hidden_stale_gives_processing():
    assert _sub(last_event_kind="agent", since_last=1.5) == "processing"


def test_soft_hidden_fresh_gives_thinking():
    assert _sub(last_event_kind="agent", since_last=0.5) == "thinking"


def test_default_thinking():
    assert _sub(last_event_kind="tool", since_last=10.0) == "thinking"
