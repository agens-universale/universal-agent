"""select_active_substate — под-состояние TUI-индикатора в режиме active."""

from gc_tui.screens.main import select_active_substate

_SOFT = {"agent", "system"}


def _sub(**kw):
    base = dict(
        summarizing=False,
        finalizing=False,
        finish_gate_checking=False,
        last_event_kind="agent",
        since_last=0.0,
        soft_hide_kinds=_SOFT,
        saw_visible_event=True,
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


def test_finish_gate_checking_beats_finalized_sentinel():
    # Судья turn_finish реально работает (флаг поднят) → «сверяюсь…», даже когда
    # sentinel _finalized уже защёлкнут (регрессия видимости судьи).
    assert (
        _sub(finish_gate_checking=True, last_event_kind="_finalized")
        == "processing"
    )


def test_finish_gate_checking_beats_finalizing_flag():
    assert _sub(finish_gate_checking=True, finalizing=True) == "processing"


def test_finish_gate_checking_off_and_finalized_still_validating():
    # Регресс-гвоздь: флаг опущен (нет судьи/gate=off) → прежнее поведение,
    # ложного «сверяюсь» нет.
    assert (
        _sub(finish_gate_checking=False, last_event_kind="_finalized")
        == "validating"
    )


def test_summarizing_wins_over_finish_gate_checking():
    assert _sub(summarizing=True, finish_gate_checking=True) == "compressing"


def test_soft_hidden_stale_gives_processing():
    assert _sub(last_event_kind="agent", since_last=1.5) == "processing"


def test_soft_hidden_fresh_gives_thinking():
    assert _sub(last_event_kind="agent", since_last=0.5) == "thinking"


def test_default_thinking():
    assert _sub(last_event_kind="tool", since_last=10.0) == "thinking"


def test_hidden_stale_before_any_visible_gives_thinking():
    # Континуация одобрения плана: первое событие скрытое (system).
    assert (
        _sub(
            last_event_kind="system", since_last=13.0, saw_visible_event=False
        )
        == "thinking"
    )


def test_hidden_stale_after_visible_gives_processing():
    assert (
        _sub(last_event_kind="system", since_last=13.0, saw_visible_event=True)
        == "processing"
    )


def test_finish_gate_checking_ignores_visibility_gate():
    assert (
        _sub(finish_gate_checking=True, saw_visible_event=False)
        == "processing"
    )


def test_finalizing_ignores_visibility_gate():
    assert _sub(finalizing=True, saw_visible_event=False) == "validating"
