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
    BlockEdit,
    BlockKind,
    CallTimers,
    MountHeader,
    MountPhrase,
    SetConsequence,
    SetDiffShare,
    SetEditTail,
    SetExpanded,
    SetHeader,
    SetMode,
    SetPhrase,
    SetTimer,
    SetVisible,
    SetWord,
    ToolBlocks,
    block_glyph,
    block_kind,
    command_file_verb,
    counts_text,
    diff_key,
    diff_rows,
    edit_verb,
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


# --- правки (Task 8) -----------------------------------------------------


def _edits_model(preview: int = 12):
    timers = CallTimers()
    return ToolBlocks(timers, diff_preview=preview), _Fold()


def _edit(key, *, verb="Edited", add=1, rem=0, lines=10, size=None):
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


@pytest.mark.parametrize(
    "n,preview,rows",
    [(1, 12, 12), (2, 12, 6), (3, 12, 4), (4, 12, 3), (10, 12, 3)],
)
def test_diff_rows(n, preview, rows):
    assert diff_rows(n, preview) == rows


def test_diff_rows_preview_30():
    assert diff_rows(2, 30) == 15


def test_counts_text():
    assert counts_text(4, 2) == (
        (" (", "dim"),
        ("+4", "ok"),
        (" ", "dim"),
        ("−2", "error"),
        (")", "dim"),
    )
    assert counts_text(5, 0) == ((" (", "dim"), ("+5", "ok"), (")", "dim"))
    assert counts_text(0, 3) == ((" (", "dim"), ("−3", "error"), (")", "dim"))
    assert counts_text(0, 0) == ()


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
    assert fold.tail("a") == " (+37)"
    assert fold.share("a") == SetDiffShare("a", 12, "single")
    assert fold.phrase("block:0", "bottom") == "+ ещё 25 строк"
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after=diff_key("a")
    )
    assert not any(k[0] == "MountHeader" for k in fold.state)
    fold(model.toggle("a"))
    assert fold.share("a") == SetDiffShare("a", None, "single")
    assert fold.phrase("block:0", "bottom") == COLLAPSE_PHRASE


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
    tail = ((" (", "dim"), ("+3", "ok"), (")", "dim"))
    assert SetEditTail("a", tail) in out
    assert not hasattr(ToolBlocks, "edit_counts")


def test_single_edit_fits_no_phrase():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=5))
    assert fold.share("a") == SetDiffShare("a", 12, "single")
    assert ("SetPhrase", "block:0", "bottom") not in fold.state
    assert model.toggle("a") == []


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
    assert fold.share("a") == SetDiffShare("a", 6, "member")
    assert fold.share("b") == SetDiffShare("b", 6, "member")
    assert fold.head_text() == "Edited 2 файла (+5 −3)"
    assert fold.header().glyph == "ok"
    assert fold.tail("a") == " (+4 −2)"


def test_three_files_share_4():
    model, fold = _edits_model()
    for key in "abc":
        _apply(model, fold, key, _edit(key, lines=4))
    assert [fold.share(k).rows for k in "abc"] == [4, 4, 4]
    assert [fold.mode(k).gap for k in "abc"] == [False, True, True]
    assert [fold.mode(k).lead for k in "abc"] == [True, False, False]
    assert fold.head_text() == "Edited 3 файла (+3)"
    assert ("SetPhrase", "block:0", "bottom") not in fold.state


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
    assert fold.timer("b") == SetTimer("b", None, live=False)


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
    assert fold.head_text() == "Added 2 файла (+18) · 1 отказ"
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
    # Скрытые строки видимых файлов (3 × 2) и все строки скрытых (7 × 5).
    assert fold.phrase("block:0", "bottom") == "+ ещё 41 строка"
    assert fold.mode("f7") == SetMode("f7", "member")
    assert fold.mode("f8") == SetMode("f8", "member", gap=True)
    fold(model.toggle("block:0:bottom"))
    assert all(fold.visible(k) for k in keys)
    assert fold.share("f0") == SetDiffShare("f0", None, "member")
    assert fold.phrase("block:0", "bottom") == COLLAPSE_PHRASE
    assert fold.phrase("block:0", "top") is None
    assert fold.mode("f0") == SetMode("f0", "member", lead=True)


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
            add=lines,
            rem=0,
            diff_lines=lines,
            size_bytes=None,
            has_record=True,
        )
        for i, (verb, lines) in enumerate(specs)
    ]


def test_ran_parts_command_only():
    model, fold = _edits_model()
    _ran(model, fold)
    fold(
        model.ran_parts(
            "x", command_hidden=3, output_hidden=0, files=(), unrecorded=0
        )
    )
    assert fold.phrase("block:0", "bottom") == "+ ещё 3 строки"
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after="x"
    )


def test_ran_parts_output_only():
    model, fold = _edits_model()
    _ran(model, fold)
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=11, files=(), unrecorded=0
        )
    )
    assert fold.phrase("block:0", "bottom") == "+ ещё 11 строк"


def test_ran_parts_command_and_output():
    model, fold = _edits_model()
    _ran(model, fold)
    fold(
        model.ran_parts(
            "x", command_hidden=3, output_hidden=11, files=(), unrecorded=0
        )
    )
    assert fold.phrase("block:0", "bottom") == "+ ещё 14 строк"
    out = fold(model.toggle("block:0:bottom"))
    assert SetExpanded("block:0", True) in out
    assert fold.phrase("block:0", "bottom") == COLLAPSE_PHRASE
    out = fold(model.toggle("x"))
    assert SetExpanded("block:0", False) in out
    assert fold.phrase("block:0", "bottom") == "+ ещё 14 строк"


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
    files = _files(("Added", 4), ("Edited", 4), ("Edited", 4), ("Edited", 4))
    fold(
        model.ran_parts(
            "x", command_hidden=0, output_hidden=10, files=files, unrecorded=0
        )
    )
    keys = [f.key for f in files]
    assert fold.phrase("block:0", "files") == "… ещё 1"
    assert fold.state[("MountPhrase", "block:0", "files")] == MountPhrase(
        "block:0", "files", before=keys[0]
    )
    assert [fold.visible(k) for k in keys] == [False, True, True, True]
    assert [fold.share(k).rows for k in keys[1:]] == [3, 3, 3]
    assert fold.share(keys[0]) is None
    assert [fold.mode(k).gap for k in keys[1:]] == [False, True, True]
    assert not any(fold.mode(k).lead for k in keys)
    assert fold.tail(keys[1]) == " (+4)"
    # Одна фраза внизу: 10 строк вывода + 3 × 1 видимых + 4 скрытого.
    assert fold.phrase("block:0", "bottom") == "+ ещё 17 строк"
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after=diff_key(keys[-1])
    )
    fold(model.toggle("block:0:files"))
    assert all(fold.visible(k) for k in keys)
    assert fold.share(keys[0]) == SetDiffShare(keys[0], None, "member")
    assert fold.phrase("block:0", "files") is None


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
    assert fold.phrase("block:0", "bottom") == COLLAPSE_PHRASE
    assert fold.state[("MountPhrase", "block:0", "bottom")] == MountPhrase(
        "block:0", "bottom", after="block:0:unrecorded"
    )


def test_ran_parts_deleted_file():
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
    # Фраза — под последним по месту файлом, не между файлами.
    assert fold.state[("MountPhrase", block, "bottom")] == MountPhrase(
        block, "bottom", after=diff_key("e2")
    )
    assert fold.phrase(block, "bottom") == "+ ещё 18 строк"
    assert fold.head_text(block) == "Edited 3 файла (+3)"


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
    assert fold.state[("MountPhrase", block, "bottom")] == MountPhrase(
        block, "bottom", after=diff_key("e2")
    )


def test_place_deferred_single_phrase_hidden_on_join():
    """Ждавшая правка успела показаться одиночной (своя фраза) — при
    входе в группу её фраза прячется."""
    model, fold = _edits_model()
    _pending(model, fold, "g1")
    _done(model, fold, "g1", _edit("g1", lines=2))
    _pending(model, fold, "e1")
    _pending(model, fold, "e2")
    _done(model, fold, "e2", _edit("e2", lines=40))
    own = ("SetPhrase",)
    phrases = [k for k in fold.state if k[:1] == own and k[2] == "bottom"]
    assert any(fold.state[k].text for k in phrases)
    _done(model, fold, "e1", _edit("e1", lines=2))
    block = model.block_of("g1")
    visible = [
        k
        for k in fold.state
        if k[:1] == own and fold.state[k].text is not None
    ]
    assert visible == [("SetPhrase", block, "bottom")]


# --- доля диффа: бинарь применился (макет p4.py, исключён только Deleted)


def test_share_counts_binary_as_applied():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=20))
    _apply(model, fold, "img", _edit("img", lines=0, size=512))
    assert fold.share("a") == SetDiffShare("a", 6, "member")


def test_share_skips_failed_edit():
    model, fold = _edits_model()
    _apply(model, fold, "a", _edit("a", lines=20))
    _apply(model, fold, "b", outcome="error", reason="не найден")
    assert fold.share("a") == SetDiffShare("a", 12, "member")


def test_ran_share_counts_binary_not_deleted():
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
    assert fold.share("x:f0") == SetDiffShare("x:f0", 6, "member")
