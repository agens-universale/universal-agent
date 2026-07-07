"""ReasoningClock (Model B): эпизод (episode) + interrupt + debounce (live)."""
from tui.services.reasoning_clock import COGNITIVE_TOOLS, ReasoningClock


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _mk(hold: float = 0.5):
    fc = _FakeClock()
    return ReasoningClock(clock=fc, hold=hold), fc


def test_episode_accumulates():
    c, fc = _mk()
    c.start("stream")
    fc.t = 2.0
    c.stop("stream")
    assert c.episode() == 2.0


def test_episode_includes_current_partial_while_active():
    c, fc = _mk()
    c.start("a")
    fc.t = 1.5
    assert c.episode() == 1.5  # ещё активно — партиал учтён


def test_union_no_double_count_on_overlap():
    c, fc = _mk()
    c.start("a")
    fc.t = 1.0
    c.start("b")      # перекрытие
    fc.t = 2.0
    c.stop("a")
    fc.t = 3.0
    c.stop("b")
    assert c.episode() == 3.0  # union [0..3], не 2+2


def test_live_true_while_active():
    c, fc = _mk()
    c.start("a")
    fc.t = 10.0
    assert c.live() is True


def test_live_held_within_hold_after_stop():
    c, fc = _mk(hold=0.5)
    c.start("a")
    fc.t = 1.0
    c.stop("a")
    fc.t = 1.4            # 0.4 < 0.5
    assert c.live() is True


def test_live_false_after_hold():
    c, fc = _mk(hold=0.5)
    c.start("a")
    fc.t = 1.0
    c.stop("a")
    fc.t = 1.6            # 0.6 >= 0.5
    assert c.live() is False


def test_hold_default_is_05():
    fc = _FakeClock()
    c = ReasoningClock(clock=fc)   # без hold → дефолт
    c.start("a")
    fc.t = 1.0
    c.stop("a")
    fc.t = 1.4                     # 0.4 < 0.5
    assert c.live() is True
    fc.t = 1.5                     # 0.5 >= 0.5
    assert c.live() is False


def test_contiguous_ops_merge_into_one_episode():
    # Подряд идущие когнитивные операции (без interrupt) — ОДИН эпизод, времена суммируются.
    c, fc = _mk(hold=0.5)
    c.start("a")
    fc.t = 1.0
    c.stop("a")          # интервал [0..1] → 1.0
    fc.t = 1.2           # гэп 0.2, БЕЗ interrupt
    c.start("b")
    fc.t = 1.5
    c.stop("b")          # интервал [1.2..1.5] → +0.3
    assert c.episode() == 1.3          # один эпизод: 1.0 + 0.3 (гэп не в счёт)


def test_pause_without_interrupt_stays_one_episode():
    # Долгая пауза (> hold) БЕЗ действия эпизод НЕ рвёт — по-прежнему один эпизод.
    c, fc = _mk(hold=0.5)
    c.start("a")
    fc.t = 1.0
    c.stop("a")          # partial 1.0
    fc.t = 3.0           # пауза 2.0 > hold, live уже погас
    assert c.live() is False
    c.start("b")
    fc.t = 4.0
    c.stop("b")          # ещё 1.0
    assert c.episode() == 2.0          # один эпизод: 1.0 + 1.0 (пауза не в счёт)


def test_interrupt_freezes_and_resets_episode():
    # Действие в мире рвёт эпизод: следующее раздумье считается заново.
    c, fc = _mk()
    c.start("a")
    fc.t = 5.0
    c.stop("a")          # эпизод = 5.0
    assert c.episode() == 5.0
    c.interrupt()        # действие в мире
    assert c.episode() == 5.0          # сразу после — «последний эпизод» = 5.0
    fc.t = 6.0
    c.start("b")
    fc.t = 9.0
    c.stop("b")          # новый эпизод = 3.0
    assert c.episode() == 3.0          # НЕ 8.0


def test_interrupt_flushes_active_interval():
    # interrupt во время активного интервала — дозакрывает его в «последний эпизод».
    c, fc = _mk()
    c.start("a")
    fc.t = 2.0
    c.interrupt()        # активный [0..2] дозакрыт → last=2.0, текущий=0
    assert c.episode() == 2.0
    c.stop("a")          # «a» уже сброшен interrupt'ом → no-op
    assert c.episode() == 2.0


def test_reset_clears_all():
    c, fc = _mk()
    c.start("a")
    fc.t = 2.0
    c.stop("a")
    c.interrupt()        # есть и текущий, и последний
    c.reset()
    assert c.episode() == 0.0
    assert c.live() is False


def test_stop_unknown_source_is_noop():
    c, fc = _mk()
    c.stop("never-started")   # не должно падать / менять состояние
    assert c.episode() == 0.0
    assert c.live() is False


def test_start_idempotent():
    c, fc = _mk()
    c.start("a")
    fc.t = 1.0
    c.start("a")          # повторный start того же источника — no-op
    fc.t = 2.0
    c.stop("a")
    assert c.episode() == 2.0   # интервал с 0, не с 1.0


def test_cognitive_tools_membership():
    from constants.tool_names import (
        SESSION_THINK_TOOL_NAME, SESSION_MEMORY_TOOL_NAME, PROJECT_MEMORY_TOOL_NAME,
        READ_USER_INTENT_LOG_NAME, READ_STORED_TOOL_OUTPUT_NAME,
    )
    assert COGNITIVE_TOOLS == frozenset({
        SESSION_THINK_TOOL_NAME, SESSION_MEMORY_TOOL_NAME, PROJECT_MEMORY_TOOL_NAME,
        READ_USER_INTENT_LOG_NAME, READ_STORED_TOOL_OUTPUT_NAME,
    })
    # fs/exec/mcp — НЕ когнитивные
    assert "fs_write_file" not in COGNITIVE_TOOLS
    assert "exec_command" not in COGNITIVE_TOOLS
