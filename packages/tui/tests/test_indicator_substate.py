"""select_active_substate — под-состояние TUI-индикатора в режиме active.

«сверяюсь…» — только по явному флагу ядра (судья проверяет ответ); по
тишине после скрытого события экран его не угадывает."""

from gc_tui.screens.main import select_active_substate


def _sub(**kw):
    base = dict(
        summarizing=False,
        finalizing=False,
        finish_gate_checking=False,
        last_event_kind="agent",
    )
    base.update(kw)
    return select_active_substate(**base)


def test_finalizing_gives_validating():
    # Финальная проза принята → «завершаю…».
    assert _sub(finalizing=True) == "validating"


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


def test_hidden_event_does_not_mean_checking():
    # Техзаметка режима после реплики (скрытое system) и тишина — это
    # обычный вызов модели, а не проверка ответа.
    assert _sub(last_event_kind="system") == "thinking"
    assert _sub(last_event_kind="agent") == "thinking"


def test_default_thinking():
    assert _sub(last_event_kind="tool") == "thinking"
