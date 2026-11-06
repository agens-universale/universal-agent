"""Модель блоков вызовов ленты `gc_tui.services.tool_blocks` (спека P4).

Модель чистая: события на входе, решения на выходе. Одиночные шаги
проверяются точным списком решений; длинные сценарии — свёрткой решений в
итоговое состояние (`_Fold`): последнее решение по каждому ключу.
"""

from __future__ import annotations

import pytest
from gc_core.constants.tool_display import DisplayCategory as C

from gc_tui.services.tool_blocks import (
    AGENT_WINDOW,
    COLLAPSE_PHRASE,
    GROUP_COLLAPSE_PHRASE,
    BlockEdit,
    BlockKind,
    CallTimers,
    MountHeader,
    MountPhrase,
    SetConsequence,
    SetDiffShare,
    SetEditTail,
    SetHeader,
    SetMode,
    SetPhrase,
    SetRanParts,
    SetTimer,
    SetVisible,
    SetWord,
    ToolBlocks,
    block_glyph,
    block_kind,
    command_file_verb,
    counts_text,
    diff_key,
    edit_verb,
    header_word,
    more_lines_above,
    more_lines_phrase,
    more_phrase,
    output_tail,
    shown_when_folded,
)


class _Fold:
    """Итоговое состояние: последнее решение по каждому ключу."""

    def __init__(self) -> None:
        self.state: dict = {}

    def __call__(self, decisions: list) -> list:
        for d in decisions:
            self.state[d.ident] = d
        return decisions

    def parts(self, block: str = "block:0"):
        return self.state.get(("SetRanParts", block))

    def header(self, block: str = "block:0"):
        return self.state.get(("SetHeader", block))

    def phrase(self, block: str, slot: str):
        d = self.state.get(("SetPhrase", block, slot))
        return None if d is None else d.text

    def visible(self, key: str) -> bool:
        d = self.state.get(("SetVisible", key))
        return True if d is None else d.visible

    def mode(self, key: str):
        return self.state.get(("SetMode", key))

    def consequence(self, key: str):
        return self.state.get(("SetConsequence", key))

    def timer(self, key: str):
        return self.state.get(("SetTimer", key))

    def word(self, key: str):
        d = self.state.get(("SetWord", key))
        return None if d is None else d.word

    def tail(self, key: str) -> str:
        d = self.state.get(("SetEditTail", key))
        return "" if d is None else "".join(t for t, _ in d.parts)

    def share(self, key: str):
        return self.state.get(("SetDiffShare", key))

    def head_text(self, block: str = "block:0") -> str:
        head = self.header(block)
        return head.word + "".join(t for t, _ in head.tail)


def _model() -> tuple[ToolBlocks, _Fold, CallTimers]:
    timers = CallTimers()
    return ToolBlocks(timers), _Fold(), timers


def _reads(model, fold, n, *, start=0, done=True):
    for i in range(start, start + n):
        key = f"c{i}"
        fold(model.member_added(key, C.READ, waiting=False))
        fold(model.started(key))
        if done:
            fold(model.result(key, "ok", reason="", executed=None))


def test_block_kind_table():
    table = {
        C.READ: BlockKind.EXPLORED,
        C.SEARCH: BlockKind.EXPLORED,
        C.FIND: BlockKind.EXPLORED,
        C.VIEW: BlockKind.EXPLORED,
        C.EDIT: BlockKind.EDITS,
        C.WRITE: BlockKind.EDITS,
        C.FETCH: BlockKind.FETCHED,
        C.RUN: BlockKind.RAN,
        C.AGENT: BlockKind.AGENT,
        C.MEMORY: BlockKind.MEMORY,
        C.THINK: BlockKind.SINGLE,
        C.SKILL: BlockKind.SINGLE,
        C.CALC: BlockKind.SINGLE,
        C.WAIT: BlockKind.SINGLE,
        C.MCP: BlockKind.SINGLE,
        C.CALLED: BlockKind.SINGLE,
        C.FINISH: BlockKind.FINISH,
        C.HIDDEN: None,
    }
    assert {c: block_kind(c) for c in C} == table


def test_header_words():
    assert header_word(BlockKind.EXPLORED, live=True) == "Exploring"
    assert header_word(BlockKind.EXPLORED, live=False) == "Explored"
    assert header_word(BlockKind.FETCHED, live=True) == "Fetching"
    assert header_word(BlockKind.FETCHED, live=False) == "Fetched"
    assert header_word(BlockKind.RAN, live=True) == "Running"
    assert header_word(BlockKind.RAN, live=False) == "Ran"
    assert header_word(BlockKind.FINISH, live=True) == "Finishing"
    assert header_word(BlockKind.FINISH, live=False) == "Finished"
    assert header_word(BlockKind.AGENT, live=True) == "Agent"
    assert header_word(BlockKind.AGENT, live=False) == "Agent"
    assert header_word(BlockKind.MEMORY, live=True) == "Memory"
    assert header_word(BlockKind.MEMORY, live=False) == "Memory"
    assert header_word(BlockKind.EDITS, live=True) == "Editing"
    assert header_word(BlockKind.EDITS, live=False, verb="Added") == "Added"


def test_phrases_and_plurals():
    assert more_phrase(21) == "… ещё 21"
    assert more_lines_phrase(1) == "+ ещё 1 строка"
    assert more_lines_phrase(3) == "+ ещё 3 строки"
    assert more_lines_phrase(14) == "+ ещё 14 строк"
    assert COLLAPSE_PHRASE == "− свернуть"


def test_block_glyph_rule():
    assert block_glyph(running=True, stopped=False, any_ok=False) == "run"
    assert block_glyph(running=False, stopped=False, any_ok=True) == "ok"
    failed = block_glyph(running=False, stopped=False, any_ok=False)
    assert failed == "error"
    assert block_glyph(running=True, stopped=True, any_ok=True) == "halted"


def test_first_read_opens_block_with_header():
    model, _, _ = _model()
    assert model.member_added("c0", C.READ, waiting=False) == [
        MountHeader("block:0", before="c0"),
        SetHeader("block:0", "Exploring", "run", ()),
        SetMode("c0", "member", lead=True, current=True),
    ]


def test_explored_window_five():
    model, fold, _ = _model()
    _reads(model, fold, 4)
    _reads(model, fold, 1, start=4, done=False)
    assert fold.header() == SetHeader("block:0", "Exploring", "run", ())
    assert [fold.visible(f"c{i}") for i in range(5)] == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert fold.phrase("block:0", "top") == "… ещё 2"
    assert fold.state[("MountPhrase", "block:0", "top")] == MountPhrase(
        "block:0", "top", after="block:0"
    )
    # `└` — у маркера окна, не у первого видимого члена.
    assert fold.mode("c2") == SetMode("c2", "member", lead=False)
    assert fold.mode("c4") == SetMode("c4", "member", current=True)
    fold(model.result("c4", "ok", reason="", executed=None))
    assert fold.header() == SetHeader("block:0", "Explored", "ok", ())
    assert fold.mode("c4") == SetMode("c4", "member")


def test_explored_window_three_no_phrases():
    model, fold, _ = _model()
    _reads(model, fold, 3)
    assert ("MountPhrase", "block:0", "top") not in fold.state
    assert fold.mode("c0") == SetMode("c0", "member", lead=True)
    assert model.toggle("block:0") == []
    assert model.toggle("c1") == []


def test_grouping_rule_two_responses():
    """Чтения из двух ответов модели без прозы между ними — одна группа."""
    model, fold, _ = _model()
    _reads(model, fold, 2)
    _reads(model, fold, 3, start=2)
    assert ("MountHeader", "block:1") not in fold.state
    assert fold.phrase("block:0", "top") == "… ещё 2"


def test_grouping_rule_closing():
    model, fold, _ = _model()
    _reads(model, fold, 2)
    assert model.event_closing() == []
    fold(model.member_added("c2", C.SEARCH, waiting=False))
    assert fold.state[("MountHeader", "block:1")] == MountHeader(
        "block:1", before="c2"
    )


def test_grouping_rule_queued_admitted_closes():
    model, fold, _ = _model()
    _reads(model, fold, 1)
    assert model.queued_admitted() == []
    fold(model.member_added("c1", C.READ, waiting=False))
    assert ("MountHeader", "block:1") in fold.state


def test_grouping_rule_kinds():
    model, fold, _ = _model()
    fold(model.member_added("r0", C.READ, waiting=False))
    assert model.member_added("x0", C.RUN, waiting=False) == [
        SetMode("x0", "single"),
        SetWord("x0", "Running", role="command"),
    ]
    fold(model.member_added("r1", C.READ, waiting=False))
    assert fold.state[("MountHeader", "block:2")] == MountHeader(
        "block:2", before="r1"
    )


def test_single_rows_do_not_group():
    model, fold, _ = _model()
    assert model.member_added("m0", C.THINK, waiting=True) == [
        SetMode("m0", "single"),
    ]
    fold(model.member_added("m1", C.THINK, waiting=False))
    assert fold.mode("m1") == SetMode("m1", "single")
    assert not any(k[0] == "MountHeader" for k in fold.state)


def test_hidden_category_ignored():
    model, _, _ = _model()
    assert model.member_added("h0", C.HIDDEN, waiting=False) == []
    assert model.member_added("c0", C.READ, waiting=False)[0] == (
        MountHeader("block:0", before="c0")
    )


def test_batch_waiting_hidden():
    model, fold, _ = _model()
    _reads(model, fold, 3)
    for key in ("w0", "w1", "w2"):
        fold(model.member_added(key, C.READ, waiting=True))
    assert fold.visible("w0") is False
    assert fold.visible("w1") is False
    assert fold.phrase("block:0", "top") is None
    assert [fold.visible(f"c{i}") for i in range(3)] == [True] * 3
    fold(model.started("w0"))
    assert fold.visible("w0") is True
    assert fold.visible("w1") is False
    assert fold.phrase("block:0", "top") is None
    fold(model.started("w1"))
    assert fold.visible("w1") is True
    assert fold.visible("w2") is False
    assert fold.phrase("block:0", "top") == "… ещё 2"


def test_waiting_batch_header_waits_for_start():
    model, fold, _ = _model()
    decisions = model.member_added("w0", C.READ, waiting=True)
    assert decisions == [
        SetMode("w0", "member"),
        SetVisible("w0", False),
    ]
    fold(decisions)
    fold(model.started("w0"))
    assert fold.state[("MountHeader", "block:0")] == MountHeader(
        "block:0", before="w0"
    )
    assert fold.visible("w0") is True


def test_failures_stop_not_started():
    model, fold, _ = _model()
    _reads(model, fold, 1)
    fold(model.member_added("w", C.FIND, waiting=True))
    fold(model.interrupted("w", started=False))
    assert fold.visible("w") is True
    assert fold.consequence("w") == SetConsequence(
        "w", "не запущено (стоп)", "dim"
    )
    assert fold.timer("w") is None
    assert fold.header().glyph == "halted"
    assert fold.header().word == "Explored"


def test_failures_stop_result_not_started():
    """Стоп даёт не начатому вызову результат «не запущено (стоп)»."""
    model, fold, _ = _model()
    _reads(model, fold, 1)
    fold(model.member_added("w", C.FIND, waiting=True))
    fold(
        model.result(
            "w", "halted", reason="не запущено (стоп)", executed=False
        )
    )
    assert fold.visible("w") is True
    assert fold.consequence("w") == SetConsequence(
        "w", "не запущено (стоп)", "dim"
    )
    assert fold.timer("w") is None
    # Исход «остановлен» сам `○` не ставит — только остановка хода.
    assert fold.header().glyph == "ok"
    fold(model.turn_stopped())
    assert fold.header().glyph == "halted"


def test_limit_halted_member_keeps_glyph():
    """Член, остановленный лимитом, — `└` серым, заголовок без `○`."""
    model, fold, _ = _model()
    _reads(model, fold, 1)
    fold(model.member_added("h", C.READ, waiting=False))
    fold(model.started("h"))
    fold(model.result("h", "halted", reason="лимит вызовов", executed=None))
    assert fold.consequence("h") == SetConsequence("h", "лимит вызовов", "dim")
    assert fold.header().glyph == "ok"


def test_turn_stopped_only_this_turn():
    """Остановка хода не трогает блоки прошлых ходов."""
    model, fold, _ = _model()
    _reads(model, fold, 1)
    fold(model.member_added("h", C.READ, waiting=False))
    fold(model.result("h", "halted", reason="лимит вызовов", executed=None))
    model.event_closing()
    model.turn_ended()
    _reads(model, fold, 1, start=2, done=False)
    fold(model.turn_stopped())
    fold(model.interrupted("c2", started=True))
    assert fold.header("block:0").glyph == "ok"
    assert fold.header("block:1").glyph == "halted"


def test_turn_stopped_skips_limit_halted_member():
    """`○` ставит только исход стопа: член, остановленный лимитом, в
    законченном блоке остановленного хода глиф заголовка не меняет."""
    model, fold, _ = _model()
    _reads(model, fold, 1)
    fold(model.member_added("h", C.READ, waiting=False))
    fold(model.started("h"))
    fold(model.result("h", "halted", reason="лимит вызовов", executed=None))
    fold(model.turn_stopped())
    assert fold.header().glyph == "ok"


def test_turn_stopped_marks_block_by_stop_outcome():
    """Исходы стопа ядра (`отменено`, `не запущено (стоп)`) ставят `○`."""
    for reason in ("отменено", "не запущено (стоп)"):
        model, fold, _ = _model()
        _reads(model, fold, 1)
        fold(model.member_added("s", C.READ, waiting=False))
        fold(model.result("s", "halted", reason=reason, executed=None))
        fold(model.turn_stopped())
        assert fold.header().glyph == "halted", reason


def test_stopped_turn_interrupted_member_cancelled():
    """Остановка хода: идущий член без исхода — `└ отменено`, в каком бы
    порядке ни пришли стоп и перевод."""
    for stop_first in (True, False):
        model, fold, _ = _model()
        _reads(model, fold, 1)
        _reads(model, fold, 1, start=1, done=False)
        steps = [
            model.turn_stopped,
            lambda: model.interrupted("c1", started=True),
        ]
        for step in steps if stop_first else steps[::-1]:
            fold(step())
        assert fold.consequence("c1") == SetConsequence(
            "c1", "отменено", "dim"
        )
        assert fold.header().glyph == "halted"


def test_failures_turn_stopped():
    model, fold, _ = _model()
    _reads(model, fold, 1)
    _reads(model, fold, 1, start=1, done=False)
    assert fold.header().glyph == "run"
    fold(model.turn_stopped())
    # Член ещё идёт: заголовок ждёт его перевода (§2.2).
    assert fold.header() == SetHeader("block:0", "Exploring", "run", ())
    fold(model.result("c1", "halted", reason="отменено", executed=None))
    assert fold.header() == SetHeader("block:0", "Explored", "halted", ())
    assert fold.consequence("c1") == SetConsequence("c1", "отменено", "dim")
    assert fold.timer("c1") == SetTimer("c1", None)
    assert fold.header().glyph == "halted"


def test_turn_stopped_reaches_closed_block():
    """Стоп переводит и блок, уже закрытый строкой «■ Отменено»."""
    model, fold, _ = _model()
    _reads(model, fold, 1, done=False)
    model.event_closing()
    fold(model.turn_stopped())
    fold(model.interrupted("c0", started=True))
    assert fold.header().glyph == "halted"


def test_turn_stopped_leaves_finished_block():
    model, fold, _ = _model()
    _reads(model, fold, 2)
    assert model.turn_stopped() == []


def test_failures_counts():
    model, fold, _ = _model()
    _reads(model, fold, 2)
    for key, outcome, reason in (
        ("e", "error", "не найден"),
        ("r", "refused", "отказ: защита путей"),
    ):
        fold(model.member_added(key, C.READ, waiting=False))
        fold(model.started(key))
        fold(model.result(key, outcome, reason=reason, executed=None))
    _reads(model, fold, 3, start=2)
    head = fold.header()
    assert head.glyph == "ok"
    assert head.word == "Explored"
    assert "".join(t for t, _ in head.tail) == " · 1 ошибка · 1 отказ"
    assert head.tail == (
        (" · ", "dim"),
        ("1 ошибка", "error"),
        (" · ", "dim"),
        ("1 отказ", "refused"),
    )
    # Сбои ушли под окно, ничего не закреплено.
    assert fold.visible("e") is False
    assert fold.visible("r") is False
    assert fold.phrase("block:0", "top") == "… ещё 4"


def test_failures_all_failed():
    model, fold, _ = _model()
    for i in range(2):
        key = f"e{i}"
        fold(model.member_added(key, C.READ, waiting=False))
        fold(model.started(key))
        fold(model.result(key, "error", reason="не найден", executed=None))
    head = fold.header()
    assert head.glyph == "error"
    assert "".join(t for t, _ in head.tail) == " · 2 ошибки"


def test_failures_reason_look():
    model, fold, _ = _model()
    cases = (
        ("e", "error", "не найден", "error"),
        ("r", "refused", "отказ: защита путей", "refused"),
        ("h", "halted", "лимит", "dim"),
    )
    for key, outcome, reason, _look in cases:
        fold(model.member_added(key, C.READ, waiting=False))
        fold(model.started(key))
        before = fold.mode(key)
        fold(model.result(key, outcome, reason=reason, executed=None))
        # Вид члена не меняется: ни глифа, ни цвета сбоя на самой строке.
        assert fold.mode(key).mode == before.mode == "member"
    for key, _outcome, reason, look in cases:
        assert fold.consequence(key) == SetConsequence(key, reason, look)


def test_single_failure_consequence():
    model, fold, _ = _model()
    fold(model.member_added("x", C.RUN, waiting=False))
    fold(model.started("x"))
    fold(model.result("x", "refused", reason="отказ", executed=None))
    assert fold.consequence("x") == SetConsequence("x", "отказ", "refused")
    assert fold.timer("x") is None


def test_reopen_member_without_result():
    """После переоткрытия у члена без результата нет `└` и таймера."""
    model, fold, _ = _model()
    fold(model.member_added("c0", C.READ, waiting=False))
    fold(model.result("c0", "ok", reason="", executed=None))
    fold(model.member_added("c1", C.READ, waiting=False))
    fold(model.interrupted("c1", started=True))
    assert fold.consequence("c1") is None
    assert fold.timer("c1") is None
    assert fold.header() == SetHeader("block:0", "Explored", "halted", ())


def test_toggle_open_close():
    model, fold, _ = _model()
    _reads(model, fold, 5)
    fold(model.toggle("block:0:top"))
    assert all(fold.visible(f"c{i}") for i in range(5))
    assert fold.phrase("block:0", "top") is None
    assert fold.phrase("block:0", "bottom") == GROUP_COLLAPSE_PHRASE
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after="c4"
    )
    assert fold.mode("c0") == SetMode("c0", "member", lead=True)
    # Новый член встаёт и в раскрытый блок; фраза уходит под него.
    _reads(model, fold, 1, start=5)
    assert fold.visible("c5") is True
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after="c5"
    )
    fold(model.toggle("c3"))
    assert [fold.visible(f"c{i}") for i in range(6)] == [
        False,
        False,
        False,
        True,
        True,
        True,
    ]
    assert fold.phrase("block:0", "top") == "… ещё 3"
    assert fold.phrase("block:0", "bottom") is None


def test_expanded_survives_finish():
    model, fold, _ = _model()
    _reads(model, fold, 4)
    _reads(model, fold, 1, start=4, done=False)
    fold(model.toggle("block:0"))
    fold(model.result("c4", "ok", reason="", executed=None))
    assert fold.header().word == "Explored"
    assert fold.phrase("block:0", "bottom") == GROUP_COLLAPSE_PHRASE
    assert all(fold.visible(f"c{i}") for i in range(5))


def test_toggle_unknown_key():
    model, _, _ = _model()
    assert model.toggle("block:9") == []
    assert model.toggle("nope") == []


def test_timer_live_freezes_own_clock():
    model, fold, _ = _model()
    _reads(model, fold, 1)
    assert fold.timer("c0") == SetTimer("c0", None)
    assert ("SetTimer", "block:0") not in fold.state


def test_timers_registry_before_result():
    model, fold, timers = _model()
    timers.update({"c1": 2.0})
    fold(model.member_added("c1", C.READ, waiting=False))
    fold(model.result("c1", "ok", reason="", executed=None))
    assert fold.timer("c1") == SetTimer("c1", 2.0)


def test_timers_registry_after_result():
    """Переоткрытие: запись итога хода приходит после строк хода."""
    model, fold, timers = _model()
    fold(model.member_added("c1", C.READ, waiting=False))
    fold(model.result("c1", "ok", reason="", executed=None))
    assert fold.timer("c1") is None
    calls = {"c1": 3.0, "other": 1.0}
    timers.update(calls)
    assert model.timers_updated(calls) == [SetTimer("c1", 3.0)]
    assert model.timers_updated(calls) == []


def test_timers_updated_renders_only_blocks_of_new_calls():
    """Запись итога хода N перерисовывает только блоки с вызовами из её
    `calls`, не все блоки прошлых ходов (иначе конец хода квадратичен)."""
    model, fold, timers = _model()
    for turn in range(3):
        _reads(model, fold, 2, start=2 * turn)
        model.event_closing()
        model.turn_ended()
    rendered: list[str] = []
    render = model._render

    def counting(block):
        rendered.append(block.key)
        return render(block)

    model._render = counting
    calls = {"c4": 1.0, "c5": 2.0}
    timers.update(calls)
    fold(model.timers_updated(calls))
    assert rendered == ["block:2"]
    assert fold.timer("c5") == SetTimer("c5", 2.0)


def test_timers_refused_never():
    model, fold, timers = _model()
    timers.update({"r": 1.0})
    fold(model.member_added("r", C.READ, waiting=False))
    fold(model.started("r"))
    fold(model.result("r", "refused", reason="отказ", executed=None))
    assert fold.timer("r") is None
    assert model.timers_updated({"r": 1.0}) == []


def test_timers_not_executed_explicit():
    model, fold, _ = _model()
    fold(model.member_added("c", C.READ, waiting=False))
    fold(model.started("c"))
    fold(model.result("c", "error", reason="сбой", executed=False))
    assert fold.timer("c") is None


def test_timers_registry_clear():
    timers = CallTimers()
    timers.update({"a": 1.0})
    timers.update({"b": 2.0})
    assert (timers.get("a"), timers.get("b"), timers.get("c")) == (
        1.0,
        2.0,
        None,
    )
    timers.clear()
    assert timers.get("a") is None


def test_paused_member_stops_header_breathing():
    """Член ждёт человека — глиф заголовка статичный (`wait`), прочее в
    блоке не меняется; продолжение — снова `run`."""
    model, fold, _ = _model()
    _reads(model, fold, 1)
    _reads(model, fold, 1, start=1, done=False)
    assert model.paused("c1") == [
        SetHeader("block:0", "Exploring", "wait", ())
    ]
    assert model.resumed("c1") == [
        SetHeader("block:0", "Exploring", "run", ())
    ]


def test_paused_member_refused_settles_header():
    """Отказ во время ожидания — заголовок по исходу, не `wait`."""
    model, fold, _ = _model()
    _reads(model, fold, 1)
    _reads(model, fold, 1, start=1, done=False)
    fold(model.paused("c1"))
    fold(model.result("c1", "refused", reason="запрет", executed=False))
    assert fold.header().glyph == "ok"


def test_paused_single_row_no_block_change():
    model, fold, _ = _model()
    fold(model.member_added("r", C.RUN, waiting=False))
    fold(model.started("r"))
    assert model.paused("r") == []
    assert model.resumed("r") == []


@pytest.mark.parametrize("cat", [C.FETCH])
def test_fetched_like_explored(cat):
    model, fold, _ = _model()
    for i in range(5):
        key = f"f{i}"
        fold(model.member_added(key, cat, waiting=False))
        fold(model.started(key))
    assert fold.header() == SetHeader("block:0", "Fetching", "run", ())
    assert fold.phrase("block:0", "top") == "… ещё 2"
    for i in range(5):
        fold(model.result(f"f{i}", "ok", reason="", executed=None))
    assert fold.header() == SetHeader("block:0", "Fetched", "ok", ())
    # Fetch и Read — разные виды: Read открывает новый блок.
    fold(model.member_added("r", C.READ, waiting=False))
    assert ("MountHeader", "block:1") in fold.state


# --- память (решение владельца 2026-10-23) ------------------------------


def _memory(model, fold, key, outcome="ok", reason=""):
    fold(model.member_added(key, C.MEMORY, waiting=False))
    fold(model.started(key))
    if outcome is not None:
        fold(model.result(key, outcome, reason=reason, executed=None))


def test_memory_single_call_is_block():
    model, fold, _ = _model()
    assert model.member_added("m0", C.MEMORY, waiting=False) == [
        MountHeader("block:0", before="m0"),
        SetHeader("block:0", "Memory", "run", ()),
        SetMode("m0", "member", lead=True, current=True),
    ]
    fold(model.started("m0"))
    fold(model.result("m0", "ok", reason="", executed=None))
    assert fold.header() == SetHeader("block:0", "Memory", "ok", ())


def test_memory_session_and_project_one_block_window():
    """Подряд идущие вызовы session_memory и project_memory (обе —
    категория Memory) — один блок; окно — 3 последних, над ними
    `… ещё N`; пробел раскрывает и сворачивает."""
    model, fold, _ = _model()
    for i in range(5):
        _memory(model, fold, f"m{i}", outcome=None)
    assert fold.header() == SetHeader("block:0", "Memory", "run", ())
    for i in range(5):
        fold(model.result(f"m{i}", "ok", reason="", executed=None))
    assert ("MountHeader", "block:1") not in fold.state
    assert fold.header() == SetHeader("block:0", "Memory", "ok", ())
    assert fold.phrase("block:0", "top") == "… ещё 2"
    assert [fold.visible(f"m{i}") for i in range(5)] == [
        False,
        False,
        True,
        True,
        True,
    ]
    fold(model.toggle("block:0"))
    assert all(fold.visible(f"m{i}") for i in range(5))
    assert fold.phrase("block:0", "bottom") == GROUP_COLLAPSE_PHRASE
    fold(model.toggle("block:0"))
    assert fold.visible("m0") is False


def test_memory_failure_and_refusal():
    model, fold, _ = _model()
    _memory(model, fold, "m0")
    _memory(model, fold, "e", "error", "регистр переполнен")
    _memory(model, fold, "r", "refused", "отказ")
    assert fold.head_text() == "Memory · 1 ошибка · 1 отказ"
    assert fold.header().glyph == "ok"
    assert fold.consequence("e") == SetConsequence(
        "e", "регистр переполнен", "error"
    )
    assert fold.consequence("r") == SetConsequence("r", "отказ", "refused")
    assert fold.mode("e").mode == fold.mode("r").mode == "member"
    assert fold.timer("r") is None


def test_memory_all_failed_glyph():
    model, fold, _ = _model()
    _memory(model, fold, "e", "error", "сбой")
    assert fold.header() == SetHeader(
        "block:0",
        "Memory",
        "error",
        ((" · ", "dim"), ("1 ошибка", "error")),
    )


def test_memory_closed_by_other_kind():
    model, fold, _ = _model()
    _memory(model, fold, "m0")
    _reads(model, fold, 1)
    _memory(model, fold, "m1")
    assert fold.state[("MountHeader", "block:2")] == MountHeader(
        "block:2", before="m1"
    )


# --- правки (Task 8) -----------------------------------------------------


def _edits_model(preview: int = 3):
    timers = CallTimers()
    return ToolBlocks(timers, diff_preview=preview), _Fold()


def _edit(key, *, verb="Edited", add=1, rem=0, lines=10, size=None):
    """Правка: ``lines`` строк диффа с контекстом."""
    return BlockEdit(
        key=key,
        path=f"{key}.py",
        verb=verb,
        add=add,
        rem=rem,
        diff_lines=lines,
        size_bytes=size,
        has_record=True,
    )


def _apply(model, fold, key, edit=None, *, outcome="ok", reason=""):
    fold(model.member_added(key, C.EDIT, waiting=False))
    fold(model.started(key))
    fold(model.result(key, outcome, reason=reason, executed=None, edit=edit))


@pytest.mark.parametrize(
    "op,failed,verb",
    [
        ("create", False, "Added"),
        ("write", False, "Edited"),
        ("append", False, "Edited"),
        ("edit", False, "Edited"),
        (None, False, "Edited"),
        ("create", True, "Edit"),
        ("edit", True, "Edit"),
    ],
)
def test_edit_verb_table(op, failed, verb):
    assert edit_verb(op, failed=failed) == verb


@pytest.mark.parametrize(
    "op,verb",
    [
        ("create", "Added"),
        ("delete", "Deleted"),
        ("shell", "Edited"),
        ("edit", "Edited"),
    ],
)
def test_command_file_verb(op, verb):
    assert command_file_verb(op) == verb


def test_counts_text():
    assert counts_text(4, 2) == (
        (" (", "ink"),
        ("+4", "add"),
        (" ", "dim"),
        ("−2", "del"),
        (")", "ink"),
    )
    assert counts_text(5, 0) == (
        (" (", "ink"),
        ("+5", "add"),
        (" ", "dim"),
        ("−0", "del"),
        (")", "ink"),
    )
    assert counts_text(0, 0) == (
        (" (", "ink"),
        ("+0", "add"),
        (" ", "dim"),
        ("−0", "del"),
        (")", "ink"),
    )


def test_editing_row_is_single():
    model, _ = _edits_model()
    assert model.member_added("a", C.EDIT, waiting=False) == [
        SetMode("a", "single"),
        SetWord("a", "Editing"),
    ]


def test_single_edit_share():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", verb="Added", add=37, lines=37))
    assert fold.word("a") == "Added"
    assert fold.tail("a") == " (+37 −0)"
    assert fold.share("a") == SetDiffShare("a", 3, "single")
    # Фраза — под диффом своего файла, фразы блока нет (2026-10-26).
    assert fold.phrase("a", "more") == "+ ещё 34 строки"
    assert fold.state[("MountPhrase", "a", "more")] == MountPhrase(
        "a", "more", after=diff_key("a")
    )
    assert ("SetPhrase", "block:0", "bottom") not in fold.state
    assert not any(k[0] == "MountHeader" for k in fold.state)
    fold(model.toggle("a"))
    assert fold.share("a") == SetDiffShare("a", None, "single")
    assert fold.phrase("a", "more") == COLLAPSE_PHRASE
    fold(model.toggle("a:more"))
    assert fold.share("a") == SetDiffShare("a", 3, "single")
    assert fold.phrase("a", "more") == "+ ещё 34 строки"


def test_compact_edit_phrase_counts_all_lines():
    """Ход вне окна раскрытых (§6.3): тело не монтируется, фраза блока
    считает все строки диффа — по ней блок раскрывается."""
    from dataclasses import replace

    model, fold = _edits_model()
    edit = replace(_edit("a", lines=3), compact=True)
    _apply(model, fold, "a", edit)
    assert fold.phrase("a", "more") == "+ ещё 3 строки"
    fold(model.toggle("a"))
    assert fold.share("a") == SetDiffShare("a", None, "single")
    assert fold.phrase("a", "more") == COLLAPSE_PHRASE


def test_compact_ran_file_counts_all_lines():
    from dataclasses import replace

    model, fold = _edits_model()
    fold(model.member_added("x", C.RUN, waiting=False))
    fold(model.result("x", "ok", reason="", executed=True))
    files = [replace(_edit("x:a", lines=2), compact=True)]
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    assert fold.phrase("x:a", "more") == "+ ещё 2 строки"
    assert ("SetPhrase", "block:0", "bottom") not in fold.state


def test_edit_comes_with_result():
    """Правка приходит вместе с исходом — одним событием; отдельного
    позднего события счётчиков нет."""
    model, _ = _edits_model()
    model.member_added("a", C.EDIT, waiting=False)
    out = model.result(
        "a",
        "ok",
        reason="",
        executed=True,
        edit=_edit("a", verb="Added", add=3, lines=3),
    )
    assert SetWord("a", "Added") in out
    assert SetEditTail("a", counts_text(3, 0)) in out
    assert not hasattr(ToolBlocks, "edit_counts")


def test_single_edit_fits_no_phrase():
    """Раскрытие не добавит ни строки — фразы нет, пробел ничего не
    делает."""
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=2))
    assert fold.share("a") == SetDiffShare("a", 3, "single")
    assert ("SetPhrase", "a", "more") not in fold.state
    assert model.toggle("a") == []


def test_one_line_edit_fits_budget_without_phrase():
    """Правка одной строки с контекстом (8 строк) при бюджете 10 — дифф
    целиком, фразы нет, раскрывать нечего (решение 2026-10-26)."""
    model, fold = _edits_model(preview=10)
    _apply(model, fold, "a", _edit("a", lines=8))
    assert fold.share("a") == SetDiffShare("a", 10, "single")
    assert ("SetPhrase", "a", "more") not in fold.state
    assert model.toggle("a") == []


def test_phrase_counts_rows_over_budget():
    """6 изменённых и 6 контекста при бюджете 10: показано 10 строк,
    фраза — оставшиеся 2 (решение 2026-10-26)."""
    model, fold = _edits_model(preview=10)
    _apply(model, fold, "a", _edit("a", lines=12))
    assert fold.phrase("a", "more") == "+ ещё 2 строки"
    fold(model.toggle("a"))
    assert fold.share("a") == SetDiffShare("a", None, "single")
    assert fold.phrase("a", "more") == "− свернуть"


def test_preview_setting_is_rows_per_file():
    """`trace_diff_preview_lines` — строк превью на файл, и в блоке тоже:
    доли нет."""
    model, fold = _edits_model(preview=5)
    for key in "abc":
        _apply(model, fold, key, _edit(key, lines=12))
    assert [fold.share(k).rows for k in "abc"] == [5, 5, 5]
    assert [fold.phrase(k, "more") for k in "abc"] == ["+ ещё 7 строк"] * 3


def test_single_to_block_transition():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", add=4, rem=2, lines=6))
    fold(model.member_added("b", C.EDIT, waiting=False))
    fold(model.started("b"))
    model_out = fold(
        model.result(
            "b",
            "ok",
            reason="",
            executed=None,
            edit=_edit("b", add=1, rem=1, lines=6),
        )
    )
    assert MountHeader("block:0", before="a") in model_out
    assert SetMode("a", "member", lead=True) in model_out
    assert SetMode("b", "member", gap=True) in model_out
    assert fold.share("a") == SetDiffShare("a", 3, "member")
    assert fold.share("b") == SetDiffShare("b", 3, "member")
    assert fold.head_text() == "Edited 2 файла (+5 −3)"
    assert fold.header().glyph == "ok"
    assert fold.tail("a") == " (+4 −2)"


def test_three_files_each_full_preview():
    model, fold = _edits_model()
    for key in "abc":
        _apply(model, fold, key, _edit(key, lines=7))
    assert [fold.share(k).rows for k in "abc"] == [3, 3, 3]
    assert [fold.mode(k).gap for k in "abc"] == [False, True, True]
    assert [fold.mode(k).lead for k in "abc"] == [True, False, False]
    assert fold.head_text() == "Edited 3 файла (+3 −0)"
    # Контекст своего файла (4) — под каждым файлом, фразы блока нет.
    assert [fold.phrase(k, "more") for k in "abc"] == ["+ ещё 4 строки"] * 3
    assert [fold.state[("MountPhrase", k, "more")].after for k in "abc"] == [
        diff_key(k) for k in "abc"
    ]
    assert ("SetPhrase", "block:0", "bottom") not in fold.state


def test_group_without_hidden_lines_has_no_phrase():
    model, fold = _edits_model()
    for key in "abc":
        _apply(model, fold, key, _edit(key, lines=2))
    assert not any(k[0] == "SetPhrase" for k in fold.state)


def test_added_then_edited_two_blocks():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", verb="Added"))
    _apply(model, fold, "b", _edit("b", verb="Edited"))
    assert not any(k[0] == "MountHeader" for k in fold.state)
    assert fold.word("a") == "Added"
    assert fold.word("b") == "Edited"
    assert fold.mode("b") == SetMode("b", "single")


def test_edits_group_closed_by_other_kind():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a"))
    fold(model.member_added("r", C.READ, waiting=False))
    _apply(model, fold, "b", _edit("b"))
    assert fold.mode("b") == SetMode("b", "single")


def test_failed_edit_joins_single():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", add=4, rem=2))
    _apply(model, fold, "b", outcome="error", reason="не найден фрагмент")
    assert fold.head_text() == "Edited 2 файла (+4 −2) · 1 ошибка"
    assert fold.consequence("b") == SetConsequence(
        "b", "не найден фрагмент", "error"
    )
    assert fold.tail("b") == ""
    assert fold.share("b") is None
    # Таймер — у исполнявшегося упавшего вызова.
    assert fold.timer("b") == SetTimer("b", None)


def test_failed_edit_two_in_row():
    model, fold = _edits_model()
    _apply(model, fold, "a", outcome="error", reason="не найден")
    assert fold.word("a") == "Edit"
    _apply(model, fold, "b", outcome="error", reason="не найден")
    assert fold.head_text() == "Edited 2 файла · 2 ошибки"
    assert fold.header().glyph == "error"


def test_failed_edit_alone_then_success():
    model, fold = _edits_model()
    _apply(model, fold, "a", outcome="error", reason="не найден")
    _apply(model, fold, "b", _edit("b"))
    assert not any(k[0] == "MountHeader" for k in fold.state)
    assert fold.word("a") == "Edit"
    assert fold.word("b") == "Edited"


def test_failed_edit_refused_in_added():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", verb="Added", add=18))
    _apply(model, fold, "b", outcome="refused", reason="отказ: защита путей")
    assert fold.head_text() == "Added 2 файла (+18 −0) · 1 отказ"
    assert fold.consequence("b") == SetConsequence(
        "b", "отказ: защита путей", "refused"
    )
    assert fold.timer("b") is None


def test_ten_files_window_and_details():
    model, fold = _edits_model()
    keys = [f"f{i}" for i in range(10)]
    for key in keys:
        _apply(model, fold, key, _edit(key, add=2, rem=1, lines=5))
    assert fold.head_text() == "Edited 10 файлов (+20 −10)"
    assert fold.phrase("block:0", "top") == "… ещё 7"
    assert [fold.visible(k) for k in keys] == [False] * 7 + [True] * 3
    assert [fold.share(k).rows for k in keys[-3:]] == [3, 3, 3]
    # Фраза — у каждого видимого файла (5 − 3), фразы блока нет.
    assert [fold.phrase(k, "more") for k in keys[-3:]] == [
        "+ ещё 2 строки"
    ] * 3
    assert [fold.phrase(k, "more") for k in keys[:7]] == [None] * 7
    assert ("SetPhrase", "block:0", "bottom") not in fold.state
    assert fold.mode("f7") == SetMode("f7", "member")
    assert fold.mode("f8") == SetMode("f8", "member", gap=True)
    # Маркер окна раскрывает окно файлов: превью файлов прежнее.
    fold(model.toggle("block:0:top"))
    assert all(fold.visible(k) for k in keys)
    assert fold.share("f0") == SetDiffShare("f0", 3, "member")
    assert fold.phrase("f0", "more") == "+ ещё 2 строки"
    assert fold.phrase("block:0", "bottom") == GROUP_COLLAPSE_PHRASE
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after="f9:more"
    )
    assert fold.phrase("block:0", "top") is None
    assert fold.mode("f0") == SetMode("f0", "member", lead=True)
    fold(model.toggle("block:0:bottom"))
    assert [fold.visible(k) for k in keys] == [False] * 7 + [True] * 3
    assert fold.phrase("block:0", "bottom") is None


def test_header_toggles_whole_block_file_toggles_own():
    """Пробел на заголовке — все файлы, окно и диффы целиком; на строке
    файла — только его дифф; клик по фразе файла — тоже только он."""
    model, fold = _edits_model()
    keys = [f"f{i}" for i in range(5)]
    for key in keys:
        _apply(model, fold, key, _edit(key, lines=5))
    fold(model.toggle("f3"))
    assert [fold.share(k).rows for k in keys[2:]] == [3, None, 3]
    assert [fold.phrase(k, "more") for k in keys[2:]] == [
        "+ ещё 2 строки",
        COLLAPSE_PHRASE,
        "+ ещё 2 строки",
    ]
    fold(model.toggle("f4:more"))
    assert fold.share("f4").rows is None
    fold(model.toggle("block:0"))
    assert all(fold.visible(k) for k in keys)
    assert [fold.share(k).rows for k in keys] == [None] * 5
    assert [fold.phrase(k, "more") for k in keys] == [COLLAPSE_PHRASE] * 5
    assert fold.phrase("block:0", "bottom") == GROUP_COLLAPSE_PHRASE
    fold(model.toggle("block:0"))
    assert [fold.visible(k) for k in keys] == [False, False] + [True] * 3
    assert [fold.share(k).rows for k in keys[2:]] == [3, 3, 3]
    assert fold.phrase("block:0", "bottom") is None


def test_file_expansion_survives_live_updates():
    """Раскрытый файл остаётся раскрытым, когда в блок встают новые
    правки (и одиночная становится членом блока)."""
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=8))
    fold(model.toggle("a"))
    _apply(model, fold, "b", _edit("b", lines=8))
    assert fold.share("a") == SetDiffShare("a", None, "member")
    assert fold.phrase("a", "more") == COLLAPSE_PHRASE
    assert fold.share("b") == SetDiffShare("b", 3, "member")
    assert fold.phrase("b", "more") == "+ ещё 5 строк"


def test_space_on_bodiless_file_does_nothing():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=8))
    _apply(model, fold, "b", outcome="error", reason="не найден")
    assert model.toggle("b") == []


def test_binary_size_tail():
    model, fold = _edits_model()
    _apply(
        model,
        fold,
        "img",
        _edit("img", verb="Added", add=0, lines=0, size=12_288),
    )
    assert fold.word("img") == "Added"
    assert fold.tail("img") == " · 12 КБ"
    assert fold.share("img") is None
    assert ("SetPhrase", "block:0", "bottom") not in fold.state


def test_edit_without_record():
    model, fold = _edits_model()
    _apply(model, fold, "a")
    assert fold.word("a") == "Edited"
    assert fold.tail("a") == ""
    assert fold.share("a") is None


# --- части `Ran` -----------------------------------------------------------


def _ran(model, fold, key="x", *, outcome="ok"):
    fold(model.member_added(key, C.RUN, waiting=False))
    fold(model.started(key))
    fold(model.result(key, outcome, reason="", executed=None))


def _files(*specs):
    return [
        BlockEdit(
            key=f"x:f{i}",
            path=f"f{i}.py",
            verb=verb,
            add=0 if verb == "Deleted" else lines,
            rem=lines if verb == "Deleted" else 0,
            diff_lines=lines,
            size_bytes=None,
            has_record=True,
        )
        for i, (verb, lines) in enumerate(specs)
    ]


def test_ran_files_phrase_counts_context():
    """Файл под `Ran` — то же правило: бюджет строк файла, фраза — строки
    сверх него."""
    model, fold = _edits_model()
    _ran(model, fold)
    files = _files(("Edited", 8))
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    assert fold.share("x:f0") == SetDiffShare("x:f0", 3, "member")
    assert fold.phrase("x:f0", "more") == "+ ещё 5 строк"
    assert ("SetPhrase", "block:0", "bottom") not in fold.state


def test_ran_parts_command_only():
    """Скрытые строки команды — фраза под её продолжением, в строке
    `Ran`; раскрыта — `− свернуть` там же, под командой (решение
    2026-10-27); фразы под выводом нет."""
    model, fold = _edits_model()
    _ran(model, fold)
    fold(
        model.ran_parts(
            "x", command_hidden=3, output_hidden=0, files=(), unrecorded=0
        )
    )
    assert fold.parts() == SetRanParts("block:0", command="+ ещё 3 строки")
    assert fold.phrase("block:0", "output") is None
    fold(model.toggle("block:0:command"))
    assert fold.parts() == SetRanParts(
        "block:0", command=COLLAPSE_PHRASE, command_open=True
    )
    assert fold.phrase("block:0", "output") is None
    fold(model.toggle("block:0:command"))
    assert fold.parts() == SetRanParts("block:0", command="+ ещё 3 строки")


def test_ran_parts_output_only():
    """Вывод свёрнут к хвосту: фраза `… ещё N строк` — над ним, где вывод
    начинается (решение 2026-10-26); раскрыт — `− свернуть` под строкой,
    внизу вывода."""
    model, fold = _edits_model()
    _ran(model, fold)
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=11, files=(), unrecorded=0
        )
    )
    assert fold.parts() == SetRanParts("block:0", output="… ещё 11 строк")
    assert fold.phrase("block:0", "output") is None
    fold(model.toggle("block:0:output"))
    assert fold.parts() == SetRanParts("block:0", output_open=True)
    assert fold.phrase("block:0", "output") == COLLAPSE_PHRASE
    assert fold.state[("MountPhrase", "block:0", "output")] == MountPhrase(
        "block:0", "output", after="x"
    )


def test_ran_parts_command_and_output_independent():
    """Фраза раскрывает то, рядом с чем стоит (решение 2026-10-27):
    команда и вывод — два состояния строки `Ran`; `− свернуть` команды —
    под командой, вывода — внизу; пробел на строке — оба."""
    model, fold = _edits_model()
    _ran(model, fold)
    fold(
        model.ran_parts(
            "x", command_hidden=3, output_hidden=11, files=(), unrecorded=0
        )
    )
    collapsed = SetRanParts(
        "block:0", command="+ ещё 3 строки", output="… ещё 11 строк"
    )
    assert fold.parts() == collapsed
    fold(model.toggle("block:0:command"))
    assert fold.parts() == SetRanParts(
        "block:0",
        command=COLLAPSE_PHRASE,
        output="… ещё 11 строк",
        command_open=True,
    )
    assert fold.phrase("block:0", "output") is None
    fold(model.toggle("block:0:output"))
    opened = SetRanParts(
        "block:0", command=COLLAPSE_PHRASE, command_open=True, output_open=True
    )
    assert fold.parts() == opened
    assert fold.phrase("block:0", "output") == COLLAPSE_PHRASE
    fold(model.toggle("block:0:command"))
    assert fold.parts() == SetRanParts(
        "block:0", command="+ ещё 3 строки", output_open=True
    )
    # Пробел на строке `Ran`: свёрнуто хоть что-то — раскрыть оба.
    fold(model.toggle("x"))
    assert fold.parts() == opened
    fold(model.toggle("x"))
    assert fold.parts() == collapsed
    assert fold.phrase("block:0", "output") is None


def test_ran_parts_open_survives_live_updates():
    """Раскрытая идущая команда остаётся раскрытой, когда приходит вывод;
    раскрытие вывода переживает новый вывод (стрим)."""
    model, fold = _edits_model()
    _ran(model, fold)
    fold(
        model.ran_parts(
            "x", command_hidden=3, output_hidden=0, files=(), unrecorded=0
        )
    )
    fold(model.toggle("block:0:command"))
    fold(
        model.ran_parts(
            "x", command_hidden=3, output_hidden=11, files=(), unrecorded=0
        )
    )
    assert fold.parts() == SetRanParts(
        "block:0",
        command=COLLAPSE_PHRASE,
        output="… ещё 11 строк",
        command_open=True,
    )
    fold(model.toggle("block:0:output"))
    fold(
        model.ran_parts(
            "x", command_hidden=3, output_hidden=12, files=(), unrecorded=0
        )
    )
    assert fold.parts().output_open
    assert fold.parts().command_open
    assert fold.phrase("block:0", "output") == COLLAPSE_PHRASE


def test_output_tail_last_lines_and_hidden():
    """Свёрнутый вывод — последние строки; вывод растёт — хвост
    сдвигается, скрытых над ним больше."""
    assert output_tail(["a", "b"]) == (("a", "b"), 0)
    lines = [f"l{i}" for i in range(14)]
    assert output_tail(lines) == (("l11", "l12", "l13"), 11)
    assert output_tail([*lines, "l14"]) == (("l12", "l13", "l14"), 12)
    assert output_tail([]) == ((), 0)


def test_more_lines_above_phrase():
    assert more_lines_above(1) == "… ещё 1 строка"
    assert more_lines_above(11) == "… ещё 11 строк"


def test_ran_parts_one_line_no_output():
    model, fold = _edits_model()
    _ran(model, fold)
    assert (
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=(), unrecorded=0
        )
        == []
    )
    assert model.toggle("x") == []


def test_ran_parts_files():
    model, fold = _edits_model()
    _ran(model, fold)
    files = _files(("Added", 5), *[("Edited", 5)] * 4)
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=10, files=files, unrecorded=0
        )
    )
    keys = [f.key for f in files]
    assert fold.phrase("block:0", "files") == "… ещё 2"
    assert fold.state[("MountPhrase", "block:0", "files")] == MountPhrase(
        "block:0", "files", before=keys[0]
    )
    assert [fold.visible(k) for k in keys] == [False, False, True, True, True]
    assert [fold.share(k).rows for k in keys[2:]] == [3, 3, 3]
    assert fold.share(keys[0]) is None
    assert [fold.mode(k).gap for k in keys[2:]] == [False, True, True]
    assert not any(fold.mode(k).lead for k in keys)
    assert fold.tail(keys[2]) == " (+5 −0)"
    # Каждая фраза — где скрытое: вывод — над хвостом, в строке `Ran`;
    # у каждого видимого файла — своя под диффом; фразы блока нет.
    assert fold.parts() == SetRanParts("block:0", output="… ещё 10 строк")
    assert fold.phrase("block:0", "output") is None
    more = [fold.phrase(k, "more") for k in keys[2:]]
    assert more == ["+ ещё 2 строки"] * 3
    assert ("SetPhrase", "block:0", "bottom") not in fold.state
    fold(model.toggle("block:0:files"))
    assert all(fold.visible(k) for k in keys)
    assert fold.share(keys[0]) == SetDiffShare(keys[0], 3, "member")
    assert fold.phrase("block:0", "files") is None
    assert fold.parts().output == "… ещё 10 строк"
    assert fold.phrase("block:0", "bottom") == GROUP_COLLAPSE_PHRASE
    # Пробел на строке файла — только его дифф; на строке команды — всё.
    fold(model.toggle(keys[1]))
    assert fold.share(keys[1]).rows is None
    assert fold.share(keys[2]).rows == 3
    out = fold(model.toggle("x"))
    assert SetRanParts("block:0", output_open=True) in out
    assert fold.phrase("block:0", "output") == COLLAPSE_PHRASE
    assert fold.state[("MountPhrase", "block:0", "output")] == MountPhrase(
        "block:0", "output", before=keys[0]
    )
    assert [fold.share(k).rows for k in keys] == [None] * 5
    out = fold(model.toggle("x"))
    assert SetRanParts("block:0", output="… ещё 10 строк") in out
    assert [fold.share(k).rows for k in keys[2:]] == [3] * 3
    assert [fold.visible(k) for k in keys] == [False, False, True, True, True]


def test_ran_parts_unrecorded():
    model, fold = _edits_model()
    _ran(model, fold)
    files = _files(*[("Edited", 0)] * 4)
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=2
        )
    )
    assert fold.phrase("block:0", "files") == "… ещё 3"
    assert fold.phrase("block:0", "unrecorded") is None
    assert fold.phrase("block:0", "bottom") is None
    fold(model.toggle("block:0:files"))
    assert fold.phrase("block:0", "unrecorded") == "ещё 2 файла не записано"
    assert fold.state[("MountPhrase", "block:0", "unrecorded")] == MountPhrase(
        "block:0", "unrecorded", after=files[-1].key
    )
    assert fold.phrase("block:0", "bottom") == GROUP_COLLAPSE_PHRASE
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after="block:0:unrecorded"
    )


def test_ran_parts_deleted_file_with_lines():
    model, fold = _edits_model()
    _ran(model, fold)
    files = _files(("Deleted", 30))
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    key = files[0].key
    assert fold.tail(key) == " (+0 \u221230)"
    assert fold.share(key) is not None


def test_ran_parts_deleted_file_without_lines():
    model, fold = _edits_model()
    _ran(model, fold)
    files = _files(("Deleted", 0))
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    key = files[0].key
    assert fold.share(key) is None
    assert fold.tail(key) == ""
    assert fold.visible(key) is True
    assert ("SetPhrase", "block:0", "bottom") not in fold.state


def test_ran_parts_bodiless_files_no_gap():
    """Файлы без тела (`Deleted`) идут подряд, без пустых строк."""
    model, fold = _edits_model()
    _ran(model, fold)
    files = _files(("Deleted", 0), ("Deleted", 0), ("Deleted", 0))
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    assert [fold.mode(f.key).gap for f in files] == [False, False, False]


def test_ran_parts_gap_only_after_body():
    """Пустая строка — только после файла с телом (превью диффа)."""
    model, fold = _edits_model()
    _ran(model, fold)
    files = _files(("Edited", 4), ("Deleted", 0), ("Deleted", 0))
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    assert [fold.mode(f.key).gap for f in files] == [False, True, False]


def test_edits_group_gap_only_after_body():
    """Группа правок: бинарь (размер) и правка без записи — без тела,
    следующий за ними файл идёт без пустой строки."""
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=4))
    _apply(model, fold, "b", _edit("b", lines=0, size=12))
    bare = BlockEdit("c", "c.py", "Edited", 0, 0, 0, 0, None, False)
    _apply(model, fold, "c", bare)
    assert [fold.mode(k).gap for k in "abc"] == [False, True, False]


def test_compact_file_gap_follows_body():
    """Компактный файл (ход вне окна) тела не показывает до раскрытия."""
    from dataclasses import replace

    model, fold = _edits_model()
    _apply(model, fold, "a", replace(_edit("a", lines=4), compact=True))
    _apply(model, fold, "b", _edit("b", lines=4))
    assert fold.mode("b").gap is False
    assert fold.phrase("a", "more") == "+ ещё 4 строки"
    fold(model.toggle("a:more"))
    assert fold.mode("b").gap is True


# --- группа по месту строки (порядок монтирования) ---------------------


def _pending(model, fold, key, category=C.EDIT):
    fold(model.member_added(key, category, waiting=False))
    fold(model.started(key))


def _done(model, fold, key, edit=None, *, outcome="ok"):
    fold(model.result(key, outcome, reason="", executed=None, edit=edit))


def test_place_edit_after_read_not_in_earlier_edit():
    """Батч [e1, r1, e2]: над e2 стоит чтение — в группу e1 она не
    входит."""
    model, fold = _edits_model()
    for key, cat in (("e1", C.EDIT), ("r1", C.READ), ("e2", C.EDIT)):
        _pending(model, fold, key, cat)
    _done(model, fold, "e1", _edit("e1"))
    _done(model, fold, "r1")
    _done(model, fold, "e2", _edit("e2"))
    assert model.block_of("e2") != model.block_of("e1")
    assert fold.mode("e2") == SetMode("e2", "single")
    assert fold.word("e2") == "Edited"
    assert not any(
        k[0] == "MountHeader" and fold.state[k].before == "e1"
        for k in fold.state
    )


def test_place_read_joins_block_right_above():
    """[r1, e1, r2], e1 закончилась, пришёл r3: над r3 стоит r2 — r3 в
    блоке r2."""
    model, fold = _edits_model()
    _pending(model, fold, "r1", C.READ)
    _pending(model, fold, "e1")
    _pending(model, fold, "r2", C.READ)
    _done(model, fold, "e1", _edit("e1"))
    fold(model.member_added("r3", C.READ, waiting=False))
    assert model.block_of("r3") == model.block_of("r2")
    assert model.block_of("r2") != model.block_of("r1")


def test_place_results_out_of_order_keep_mount_order():
    """После группы g результаты пришли не по порядку (e2 раньше e1):
    члены, `gap` и нижняя фраза — по порядку монтирования."""
    model, fold = _edits_model()
    _pending(model, fold, "g1")
    _done(model, fold, "g1", _edit("g1", lines=10))
    _pending(model, fold, "e1")
    _pending(model, fold, "e2")
    _done(model, fold, "e2", _edit("e2", lines=10))
    _done(model, fold, "e1", _edit("e1", lines=10))
    block = model.block_of("g1")
    assert model.block_of("e1") == block
    assert model.block_of("e2") == block
    assert model.last_call(block) == "e2"
    assert fold.mode("g1") == SetMode("g1", "member", lead=True)
    assert fold.mode("e1") == SetMode("e1", "member", gap=True)
    assert fold.mode("e2") == SetMode("e2", "member", gap=True)
    # Фраза — под диффом каждого файла.
    for key in ("g1", "e1", "e2"):
        assert fold.state[("MountPhrase", key, "more")] == MountPhrase(
            key, "more", after=diff_key(key)
        )
        assert fold.phrase(key, "more") == "+ ещё 7 строк"
    assert fold.head_text(block) == "Edited 3 файла (+3 −0)"
    assert ("SetPhrase", block, "bottom") not in fold.state


def test_place_out_of_order_window_by_mount():
    """Окно — последние по месту: результат первого из ждущих пришёл
    последним, он всё равно не последний в блоке."""
    model, fold = _edits_model()
    for key in ("g1", "g2", "g3"):
        _pending(model, fold, key)
        _done(model, fold, key, _edit(key, lines=2))
    _pending(model, fold, "e1")
    _pending(model, fold, "e2")
    _done(model, fold, "e2", _edit("e2", lines=2))
    _done(model, fold, "e1", _edit("e1", lines=2))
    block = model.block_of("g1")
    keys = ["g1", "g2", "g3", "e1", "e2"]
    assert [fold.visible(k) for k in keys] == [False, False, True, True, True]
    assert fold.mode("g3") == SetMode("g3", "member")
    assert fold.mode("e1") == SetMode("e1", "member", gap=True)
    assert ("SetPhrase", block, "bottom") not in fold.state


def test_place_deferred_single_phrase_stays_on_join():
    """Ждавшая правка успела показаться одиночной (фраза под её диффом) —
    при входе в группу фраза остаётся её, под тем же диффом."""
    model, fold = _edits_model()
    _pending(model, fold, "g1")
    _done(model, fold, "g1", _edit("g1", lines=2))
    _pending(model, fold, "e1")
    _pending(model, fold, "e2")
    _done(model, fold, "e2", _edit("e2", lines=40))
    own = ("SetPhrase",)
    assert fold.phrase("e2", "more") == "+ ещё 37 строк"
    _done(model, fold, "e1", _edit("e1", lines=2))
    assert model.block_of("e2") == model.block_of("g1")
    visible = [
        k
        for k in fold.state
        if k[:1] == own and fold.state[k].text is not None
    ]
    assert visible == [("SetPhrase", "e2", "more")]


# --- превью не зависит от числа файлов (решение 2026-10-23) ---------------


def test_preview_ignores_binary_and_failed_neighbours():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=20))
    _apply(model, fold, "img", _edit("img", lines=0, size=512))
    _apply(model, fold, "b", outcome="error", reason="не найден")
    assert fold.share("a") == SetDiffShare("a", 3, "member")


def test_ran_preview_ignores_binary_and_deleted():
    model, fold = _edits_model()
    _ran(model, fold)
    files = [
        *_files(("Edited", 20)),
        BlockEdit(
            key="x:img",
            path="img.png",
            verb="Added",
            add=0,
            rem=0,
            diff_lines=0,
            size_bytes=12,
            has_record=True,
        ),
        BlockEdit(
            key="x:old",
            path="old.py",
            verb="Deleted",
            add=0,
            rem=0,
            diff_lines=0,
            size_bytes=None,
            has_record=True,
        ),
    ]
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    assert fold.share("x:f0") == SetDiffShare("x:f0", 3, "member")


def test_not_started_result_has_no_timer():
    """Исход «не запущено (стоп)» — без таймера, даже если строка успела
    стартовать вживую (очередь батча)."""
    model, fold, _ = _model()
    fold(model.member_added("w", C.READ, waiting=False))
    fold(model.started("w"))
    reason = "не запущено (стоп)"
    fold(model.result("w", "halted", reason=reason, executed=None))
    assert fold.consequence("w") == SetConsequence("w", reason, "dim")
    assert fold.timer("w") is None


# --- окно субагента (спека §7, план Task 15) -------------------------------


def _agent_model() -> tuple[ToolBlocks, _Fold]:
    return ToolBlocks(CallTimers(), agent_window=True), _Fold()


def test_agent_window_counts_events():
    """Блок из 5 чтений + проза + `Ran` + две реплики = 5 событий: окно
    3, `… ещё 2` у окна субагента; члены блока событиями не считаются;
    4 события видны все."""
    model, fold = _agent_model()
    _reads(model, fold, 5)
    fold(model.event_added("prose"))
    fold(model.member_added("x", C.RUN, waiting=False))
    fold(model.started("x"))
    fold(model.result("x", "ok", reason="", executed=None))
    fold(model.event_added("reply"))
    assert fold.phrase(AGENT_WINDOW, "top") is None
    fold(model.event_added("reply2"))
    assert fold.phrase(AGENT_WINDOW, "top") == "… ещё 2"
    assert fold.state[("MountPhrase", AGENT_WINDOW, "top")] == MountPhrase(
        AGENT_WINDOW, "top", after=AGENT_WINDOW
    )
    assert not fold.visible("block:0")
    assert not fold.visible("prose")
    assert not any(fold.visible(f"c{i}") for i in range(5))
    assert all(fold.visible(k) for k in ("x", "reply", "reply2"))


def test_agent_visibility_conjunction():
    """Член видим, только если виден и в своём блоке, и в окне субагента."""
    model, fold = _agent_model()
    _reads(model, fold, 5)
    # Блок в окне: видны 3 последних члена, старшие — нет.
    assert [fold.visible(f"c{i}") for i in range(5)] == [
        False, False, True, True, True,
    ]  # fmt: skip
    for key in ("e1", "e2", "e3", "e4"):
        fold(model.event_added(key))
    # Блок вне окна субагента: скрыты и видимые в блоке члены.
    assert not any(fold.visible(f"c{i}") for i in range(5))
    # Раскрыли окно субагента — члены снова по окну своего блока.
    fold(model.toggle(AGENT_WINDOW))
    assert [fold.visible(f"c{i}") for i in range(5)] == [
        False, False, True, True, True,
    ]  # fmt: skip
    # `− свернуть` окна — последней строкой блока `Agent`, в конце уровня.
    assert fold.phrase(AGENT_WINDOW, "top") is None
    assert fold.phrase(AGENT_WINDOW, "bottom") == GROUP_COLLAPSE_PHRASE
    assert fold.state[("MountPhrase", AGENT_WINDOW, "bottom")] == (
        MountPhrase(AGENT_WINDOW, "bottom")
    )
    # Раскрыли блок — видны все его члены; свернули окно — снова скрыты.
    fold(model.toggle("block:0"))
    assert all(fold.visible(f"c{i}") for i in range(5))
    fold(model.toggle(AGENT_WINDOW))
    assert not any(fold.visible(f"c{i}") for i in range(5))
    assert fold.phrase(AGENT_WINDOW, "top") == "… ещё 2"
    assert fold.phrase(AGENT_WINDOW, "bottom") is None


def test_agent_window_event_closes_group():
    """Событие субагента закрывает его открытую группу."""
    model, fold = _agent_model()
    _reads(model, fold, 1)
    fold(model.event_added("prose"))
    _reads(model, fold, 1, start=1)
    assert model.block_of("c1") != model.block_of("c0")


def test_top_level_has_no_agent_window():
    model, fold, _ = _model()
    _reads(model, fold, 1)
    for key in ("e1", "e2", "e3", "e4"):
        fold(model.event_added(key))
    assert ("SetPhrase", AGENT_WINDOW, "top") not in fold.state
    assert fold.visible("c0")


def test_agent_row_word_role():
    """Строка `Agent` — своё слово: имя субагента `INK` (§10)."""
    model, fold, _ = _model()
    fold(model.member_added("a", C.AGENT, waiting=False))
    assert fold.state[("SetWord", "a")] == SetWord("a", "Agent", role="agent")


@pytest.mark.parametrize(
    ("outcome", "word"),
    [
        ("ok", "Finished"),
        ("error", "Finish"),
        ("refused", "Finish"),
        ("halted", "Finish"),
    ],
)
def test_finish_word_from_outcome(outcome, word):
    """Сдача: `Finished` — только принятая; со сбоем (отвергнута гейтом,
    плохие аргументы) — `Finish`, как `Edit путь` у правки (§6.1, §7)."""
    model, fold, _ = _model()
    fold(model.member_added("f", C.FINISH, waiting=False))
    assert fold.word("f") == "Finishing"
    fold(model.started("f"))
    fold(model.result("f", outcome, reason="нет", executed=True))
    assert fold.word("f") == word


# --- одну строку не прятать (решение владельца 2026-10-26) -----------------


@pytest.mark.parametrize(
    "total,limit,shown",
    [
        (0, 3, 0),
        (3, 3, 3),
        (4, 3, 4),
        (5, 3, 3),
        (10, 10, 10),
        (11, 10, 11),
        (12, 10, 10),
    ],
)
def test_shown_when_folded_boundaries(total, limit, shown):
    """До `L + 1` — всё без фразы; дальше `L` и скрыто не меньше двух."""
    assert shown_when_folded(total, limit) == shown


@pytest.mark.parametrize("cat", [C.READ, C.FETCH, C.MEMORY])
def test_group_window_four_members_all_shown(cat):
    model, fold, _ = _model()
    for i in range(4):
        fold(model.member_added(f"c{i}", cat, waiting=False))
        fold(model.started(f"c{i}"))
    assert fold.phrase("block:0", "top") is None
    assert [fold.visible(f"c{i}") for i in range(4)] == [True] * 4
    assert fold.mode("c0").lead is True
    assert model.toggle("block:0") == []


@pytest.mark.parametrize("cat", [C.READ, C.FETCH, C.MEMORY])
def test_group_window_five_members_hides_two(cat):
    model, fold, _ = _model()
    for i in range(5):
        fold(model.member_added(f"c{i}", cat, waiting=False))
        fold(model.started(f"c{i}"))
    assert fold.phrase("block:0", "top") == "… ещё 2"
    assert [fold.visible(f"c{i}") for i in range(5)] == [
        False, False, True, True, True,
    ]  # fmt: skip


def test_group_live_growth_three_four_five():
    """Идущая группа: 3 → 4 — видны все четыре; 5 — окно 3 и `… ещё 2`."""
    model, fold, _ = _model()
    _reads(model, fold, 3, done=False)
    assert fold.phrase("block:0", "top") is None
    _reads(model, fold, 1, start=3, done=False)
    assert fold.phrase("block:0", "top") is None
    assert all(fold.visible(f"c{i}") for i in range(4))
    _reads(model, fold, 1, start=4, done=False)
    assert fold.phrase("block:0", "top") == "… ещё 2"
    assert [fold.visible(f"c{i}") for i in range(5)] == [
        False, False, True, True, True,
    ]  # fmt: skip


def _edit_block(model, fold, n):
    keys = [f"e{i}" for i in range(n)]
    for key in keys:
        _apply(model, fold, key, _edit(key, lines=0))
    return keys


def test_edits_file_window_four_all_shown():
    model, fold = _edits_model()
    keys = _edit_block(model, fold, 4)
    assert fold.phrase("block:0", "top") is None
    assert all(fold.visible(k) for k in keys)
    assert model.toggle("block:0:top") == []


def test_edits_file_window_five_hides_two():
    model, fold = _edits_model()
    keys = _edit_block(model, fold, 5)
    assert fold.phrase("block:0", "top") == "… ещё 2"
    assert [fold.visible(k) for k in keys] == [False, False, True, True, True]


def _ran_files(model, fold, n, unrecorded=0):
    _ran(model, fold)
    files = _files(*[("Edited", 0)] * n)
    fold(
        model.ran_parts(
            "x",
            command_hidden=0,
            output_hidden=0,
            files=files,
            unrecorded=unrecorded,
        )
    )
    return [f.key for f in files]


def test_ran_file_window_four_all_shown():
    model, fold = _edits_model()
    keys = _ran_files(model, fold, 4)
    assert fold.phrase("block:0", "files") is None
    assert all(fold.visible(k) for k in keys)
    assert model.toggle("block:0:files") == []


def test_ran_file_window_five_hides_two():
    model, fold = _edits_model()
    keys = _ran_files(model, fold, 5)
    assert fold.phrase("block:0", "files") == "… ещё 2"
    assert [fold.visible(k) for k in keys] == [False, False, True, True, True]


def test_ran_unrecorded_counts_toward_window():
    """Незаписанные файлы — те же элементы окна: 3 файла и 1 незаписанный
    — видно всё, строка «не записано» без раскрытия."""
    model, fold = _edits_model()
    keys = _ran_files(model, fold, 3, unrecorded=1)
    assert fold.phrase("block:0", "files") is None
    assert all(fold.visible(k) for k in keys)
    assert fold.phrase("block:0", "unrecorded") == "ещё 1 файл не записано"
    assert fold.phrase("block:0", "bottom") is None


def test_agent_window_four_events_all_shown():
    model, fold = _agent_model()
    for key in ("e1", "e2", "e3", "e4"):
        fold(model.event_added(key))
    assert fold.phrase(AGENT_WINDOW, "top") is None
    assert all(fold.visible(k) for k in ("e1", "e2", "e3", "e4"))
    assert model.toggle(AGENT_WINDOW) == []


def test_agent_window_five_events_hides_two():
    model, fold = _agent_model()
    for key in ("e1", "e2", "e3", "e4", "e5"):
        fold(model.event_added(key))
    assert fold.phrase(AGENT_WINDOW, "top") == "… ещё 2"
    assert [fold.visible(k) for k in ("e1", "e2", "e3")] == [
        False, False, True,
    ]  # fmt: skip


def test_output_tail_four_lines_all_shown():
    assert output_tail(["a", "b", "c", "d"]) == (("a", "b", "c", "d"), 0)
    lines = ["a", "b", "c", "d", "e"]
    assert output_tail(lines) == (("c", "d", "e"), 2)


def test_diff_budget_limit_plus_one_shown_whole():
    """Дифф файла в `M + 1` строк — целиком, без фразы; `M + 2` — `M`
    строк и `+ ещё 2 строки`."""
    model, fold = _edits_model(preview=10)
    _apply(model, fold, "e0", _edit("e0", lines=11))
    assert fold.share("e0").rows == 11
    assert fold.phrase("e0", "more") is None
    assert model.toggle("e0") == []
    model, fold = _edits_model(preview=10)
    _apply(model, fold, "e0", _edit("e0", lines=12))
    assert fold.share("e0").rows == 10
    assert fold.phrase("e0", "more") == "+ ещё 2 строки"


def test_ran_file_diff_budget_limit_plus_one():
    model, fold = _edits_model(preview=10)
    _ran(model, fold)
    files = _files(("Edited", 11), ("Edited", 12))
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=0, files=files, unrecorded=0
        )
    )
    assert fold.share("x:f0").rows == 11
    assert fold.phrase("x:f0", "more") is None
    assert fold.phrase("x:f1", "more") == "+ ещё 2 строки"
