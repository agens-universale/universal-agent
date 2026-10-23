"""Модель блоков вызовов ленты `gc_tui.services.tool_blocks` (спека P4).

Модель чистая: события на входе, решения на выходе. Одиночные шаги
проверяются точным списком решений; длинные сценарии — свёрткой решений в
итоговое состояние (`_Fold`): последнее решение по каждому ключу.
"""

from __future__ import annotations

import pytest
from gc_core.constants.tool_display import DisplayCategory as C

from gc_tui.services.tool_blocks import (
    COLLAPSE_PHRASE,
    BlockKind,
    CallTimers,
    MountHeader,
    MountPhrase,
    SetConsequence,
    SetHeader,
    SetMode,
    SetPhrase,
    SetTimer,
    SetVisible,
    ToolBlocks,
    block_glyph,
    block_kind,
    failure_tail,
    header_word,
    more_lines_phrase,
    more_phrase,
)


class _Fold:
    """Итоговое состояние: последнее решение по каждому ключу."""

    def __init__(self) -> None:
        self.state: dict = {}

    def __call__(self, decisions: list) -> list:
        for d in decisions:
            self.state[d.ident] = d
        return decisions

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
        C.MEMORY: BlockKind.SINGLE,
        C.THINK: BlockKind.SINGLE,
        C.SKILL: BlockKind.SINGLE,
        C.CALC: BlockKind.SINGLE,
        C.WAIT: BlockKind.SINGLE,
        C.MCP: BlockKind.SINGLE,
        C.CALLED: BlockKind.SINGLE,
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
    assert header_word(BlockKind.AGENT, live=True) == "Agent"
    assert header_word(BlockKind.AGENT, live=False) == "Agent"
    assert header_word(BlockKind.EDITS, live=True) == "Editing"
    assert header_word(BlockKind.EDITS, live=False, verb="Added") == "Added"


def test_phrases_and_plurals():
    assert more_phrase(21) == "… ещё 21"
    assert more_lines_phrase(1) == "+ ещё 1 строка"
    assert more_lines_phrase(3) == "+ ещё 3 строки"
    assert more_lines_phrase(14) == "+ ещё 14 строк"
    assert COLLAPSE_PHRASE == "− свернуть"
    assert failure_tail(1, 0) == " · 1 ошибка"
    assert failure_tail(2, 0) == " · 2 ошибки"
    assert failure_tail(5, 0) == " · 5 ошибок"
    assert failure_tail(0, 1) == " · 1 отказ"
    assert failure_tail(0, 2) == " · 2 отказа"
    assert failure_tail(0, 5) == " · 5 отказов"
    assert failure_tail(1, 1) == " · 1 ошибка · 1 отказ"
    assert failure_tail(0, 0) == ""


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
    _reads(model, fold, 2, start=2)
    assert ("MountHeader", "block:1") not in fold.state
    assert fold.phrase("block:0", "top") == "… ещё 1"


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
    ]
    fold(model.member_added("r1", C.READ, waiting=False))
    assert fold.state[("MountHeader", "block:2")] == MountHeader(
        "block:2", before="r1"
    )


def test_single_rows_do_not_group():
    model, fold, _ = _model()
    assert model.member_added("m0", C.MEMORY, waiting=True) == [
        SetMode("m0", "single"),
    ]
    fold(model.member_added("m1", C.MEMORY, waiting=False))
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
    for key in ("w0", "w1"):
        fold(model.member_added(key, C.READ, waiting=True))
    assert fold.visible("w0") is False
    assert fold.visible("w1") is False
    assert fold.phrase("block:0", "top") is None
    assert [fold.visible(f"c{i}") for i in range(3)] == [True] * 3
    fold(model.started("w0"))
    assert fold.visible("w0") is True
    assert fold.visible("w1") is False
    assert fold.phrase("block:0", "top") == "… ещё 1"


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
    assert fold.header().glyph == "halted"


def test_failures_turn_stopped():
    model, fold, _ = _model()
    _reads(model, fold, 1)
    _reads(model, fold, 1, start=1, done=False)
    assert fold.header().glyph == "run"
    fold(model.turn_stopped())
    assert fold.header() == SetHeader("block:0", "Explored", "halted", ())
    fold(model.result("c1", "halted", reason="отменено", executed=None))
    assert fold.consequence("c1") == SetConsequence("c1", "отменено", "dim")
    assert fold.timer("c1") == SetTimer("c1", None, live=False)
    assert fold.header().glyph == "halted"


def test_turn_stopped_reaches_closed_block():
    """Стоп переводит и блок, уже закрытый строкой «■ Отменено»."""
    model, fold, _ = _model()
    _reads(model, fold, 1, done=False)
    model.event_closing()
    fold(model.turn_stopped())
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
    assert fold.phrase("block:0", "bottom") == COLLAPSE_PHRASE
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
    assert fold.phrase("block:0", "bottom") == COLLAPSE_PHRASE
    assert all(fold.visible(f"c{i}") for i in range(5))


def test_toggle_unknown_key():
    model, _, _ = _model()
    assert model.toggle("block:9") == []
    assert model.toggle("nope") == []


def test_timer_live_freezes_own_clock():
    model, fold, _ = _model()
    _reads(model, fold, 1)
    assert fold.timer("c0") == SetTimer("c0", None, live=False)
    assert ("SetTimer", "block:0") not in fold.state


def test_timers_registry_before_result():
    model, fold, timers = _model()
    timers.update({"c1": 2.0})
    fold(model.member_added("c1", C.READ, waiting=False))
    fold(model.result("c1", "ok", reason="", executed=None))
    assert fold.timer("c1") == SetTimer("c1", 2.0, live=False)


def test_timers_registry_after_result():
    """Переоткрытие: запись итога хода приходит после строк хода."""
    model, fold, timers = _model()
    fold(model.member_added("c1", C.READ, waiting=False))
    fold(model.result("c1", "ok", reason="", executed=None))
    assert fold.timer("c1") is None
    timers.update({"c1": 3.0, "other": 1.0})
    assert model.timers_updated() == [SetTimer("c1", 3.0, live=False)]
    assert model.timers_updated() == []


def test_timers_refused_never():
    model, fold, timers = _model()
    timers.update({"r": 1.0})
    fold(model.member_added("r", C.READ, waiting=False))
    fold(model.started("r"))
    fold(model.result("r", "refused", reason="отказ", executed=None))
    assert fold.timer("r") is None
    assert model.timers_updated() == []


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


def test_paused_resumed_no_block_change():
    model, fold, _ = _model()
    _reads(model, fold, 1, done=False)
    assert model.paused("c0") == []
    assert model.resumed("c0") == []


@pytest.mark.parametrize("cat", [C.FETCH])
def test_fetched_like_explored(cat):
    model, fold, _ = _model()
    for i in range(4):
        key = f"f{i}"
        fold(model.member_added(key, cat, waiting=False))
        fold(model.started(key))
    assert fold.header() == SetHeader("block:0", "Fetching", "run", ())
    assert fold.phrase("block:0", "top") == "… ещё 1"
    for i in range(4):
        fold(model.result(f"f{i}", "ok", reason="", executed=None))
    assert fold.header() == SetHeader("block:0", "Fetched", "ok", ())
    # Fetch и Read — разные виды: Read открывает новый блок.
    fold(model.member_added("r", C.READ, waiting=False))
    assert ("MountHeader", "block:1") in fold.state
