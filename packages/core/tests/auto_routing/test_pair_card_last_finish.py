"""Итог прошлого хода из trace (turn_finish): характеризация каждой ветки разбора.

Карточка — вход модели-судьи и часть записанных ожиданий durable-реплея, поэтому
текст итога закреплён побайтно для обоих контрактов (текущего и `legacy`)."""

import json
from types import SimpleNamespace

import pytest

from gc_core.auto_routing.pair_judge import build_pair_card
from gc_core.intent_log import append_user_ask, flush

ASK = {"role": "user", "content": "OLD TASK"}
OK = {"ok": True, "status": "submitted"}
NONE_NEW = {"request": "OLD TASK", "outcome_available": False}
NONE_LEGACY = {"request": "OLD TASK"}


def _call(call_id="f", *, raw=None, **args):
    message = {
        "type": "function_call",
        "name": "turn_finish",
        "arguments": json.dumps(args) if raw is None else raw,
    }
    if call_id is not None:
        message["call_id"] = call_id
    return message


def _out(output=OK, call_id="f", *, as_text=True):
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(output) if as_text else output,
    }


def _previous(messages, *, legacy=False, current="continue", start="end"):
    _, log, seq = flush(
        append_user_ask([], "OLD TASK", user_turn_seq=0), [], 0
    )
    state = SimpleNamespace(
        current_user_intent=append_user_ask([], current, user_turn_seq=seq),
        user_intent_log=log,
        user_turn_seq=seq,
        messages=list(messages),
        turn_messages_start=len(messages) if start == "end" else start,
    )
    return json.loads(build_pair_card(state, legacy=legacy))["previous_turn"]


FULL = _call(solution_type="full_solution", task_report="REPORT")

CASES = {
    # --- текущий контракт ---
    "accepted": (
        [ASK, FULL, _out()],
        False,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "output_as_dict": (
        [ASK, FULL, _out(as_text=False)],
        False,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "interrupted_no_output": ([ASK, FULL], False, NONE_NEW),
    "output_bad_json": (
        [
            ASK,
            FULL,
            {
                "type": "function_call_output",
                "call_id": "f",
                "output": "{oops",
            },
        ],
        False,
        NONE_NEW,
    ),
    "output_not_dict": ([ASK, FULL, _out([1])], False, NONE_NEW),
    "output_ok_not_true": (
        [ASK, FULL, _out({"ok": 1, "status": "submitted"})],
        False,
        NONE_NEW,
    ),
    "output_other_call_id": ([ASK, FULL, _out(call_id="x")], False, NONE_NEW),
    "last_output_wins": (
        [ASK, FULL, _out(), _out({"ok": False, "status": "submitted"})],
        False,
        NONE_NEW,
    ),
    "last_output_wins_ok": (
        [ASK, FULL, _out({"ok": False}), _out()],
        False,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "no_call_id": (
        [
            ASK,
            _call(None, solution_type="full_solution", task_report="R"),
            _out(),
        ],
        False,
        NONE_NEW,
    ),
    "empty_call_id": (
        [
            ASK,
            _call("", solution_type="full_solution", task_report="R"),
            _out(call_id=""),
        ],
        False,
        NONE_NEW,
    ),
    "gate_reject": (
        [ASK, FULL, _out({**OK, "finish_gate": {"action": "reject"}})],
        False,
        NONE_NEW,
    ),
    "gate_not_dict": (
        [ASK, FULL, _out({**OK, "finish_gate": "accept"})],
        False,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "gate_fail_open": (
        [
            ASK,
            FULL,
            _out(
                {**OK, "finish_gate": {"runtime_outcome": "fail_open_limit"}}
            ),
        ],
        False,
        {
            "request": "OLD TASK",
            "outcome": "full_solution [fail_open_limit]: REPORT",
        },
    ),
    "gate_other_outcome": (
        [
            ASK,
            FULL,
            _out({**OK, "finish_gate": {"runtime_outcome": "accept"}}),
        ],
        False,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "fail_open_without_kind": (
        [
            ASK,
            _call(task_report="R"),
            _out(
                {**OK, "finish_gate": {"runtime_outcome": "fail_open_error"}}
            ),
        ],
        False,
        {"request": "OLD TASK", "outcome": " [fail_open_error]: R"},
    ),
    "kind_only": (
        [ASK, _call(solution_type=" partial_solution "), _out()],
        False,
        {"request": "OLD TASK", "outcome": "partial_solution"},
    ),
    "report_only": (
        [ASK, _call(task_report=" R "), _out()],
        False,
        {"request": "OLD TASK", "outcome": "R"},
    ),
    "empty_finish": ([ASK, _call(), _out()], False, NONE_NEW),
    "args_bad_json": ([ASK, _call(raw="{oops"), _out()], False, NONE_NEW),
    "args_not_dict": ([ASK, _call(raw="[1]"), _out()], False, NONE_NEW),
    "args_none": (
        [
            ASK,
            _call(raw=None, solution_type="s") | {"arguments": None},
            _out(),
        ],
        False,
        NONE_NEW,
    ),
    "preceding_ask_not_closed": (
        [{"role": "user", "content": "OTHER"}, FULL, _out()],
        False,
        NONE_NEW,
    ),
    "no_preceding_ask": ([FULL, _out()], False, NONE_NEW),
    "technical_user_skipped": (
        [
            ASK,
            {
                "role": "user",
                "content": "<current_context>x</current_context>",
            },
            FULL,
            _out(),
        ],
        False,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "only_last_finish_counts": (
        [
            ASK,
            FULL,
            _out(),
            _call("g", solution_type="s", task_report="LATER"),
        ],
        False,
        NONE_NEW,
    ),
    "last_of_two_accepted": (
        [
            ASK,
            _call("e", solution_type="a", task_report="FIRST"),
            _out(call_id="e"),
            _call("g", solution_type="b", task_report="SECOND"),
            _out(call_id="g"),
        ],
        False,
        {"request": "OLD TASK", "outcome": "b: SECOND"},
    ),
    "non_finish_calls_ignored": (
        [
            ASK,
            FULL,
            _out(),
            {"type": "function_call", "name": "fs_read", "call_id": "r"},
        ],
        False,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "long_report_head_tail": (
        [
            ASK,
            _call(solution_type="k", task_report="A" + "x" * 3000 + "Z"),
            _out(),
        ],
        False,
        None,
    ),
    # --- legacy-контракт: выход тула не проверяется ---
    "legacy_no_output": (
        [ASK, FULL],
        True,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "legacy_rejected_output": (
        [ASK, FULL, _out({"ok": False, "finish_gate": {"action": "reject"}})],
        True,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "legacy_no_ask": (
        [FULL],
        True,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "legacy_fail_open_not_marked": (
        [
            ASK,
            FULL,
            _out(
                {**OK, "finish_gate": {"runtime_outcome": "fail_open_error"}}
            ),
        ],
        True,
        {"request": "OLD TASK", "outcome": "full_solution: REPORT"},
    ),
    "legacy_last_finish": (
        [
            _call("e", solution_type="a", task_report="FIRST"),
            _call("g", solution_type="b", task_report="SECOND"),
        ],
        True,
        {"request": "OLD TASK", "outcome": "b: SECOND"},
    ),
    "legacy_bad_args": ([FULL, _call(raw="{oops")], True, NONE_LEGACY),
    "legacy_empty_finish": ([_call()], True, NONE_LEGACY),
    "legacy_long_report_head": (
        [_call(solution_type="k", task_report="A" + "x" * 3000 + "Z")],
        True,
        {"request": "OLD TASK", "outcome": ("k: A" + "x" * 3000)[:1000]},
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_previous_outcome_from_the_trace(name):
    messages, legacy, expected = CASES[name]
    previous = _previous(messages, legacy=legacy)
    if expected is None:
        outcome = previous["outcome"]
        assert (outcome[:4], outcome[-1], len(outcome)) == ("k: A", "Z", 1000)
        return
    assert previous == expected


def test_messages_after_the_turn_start_are_not_the_previous_turn():
    assert _previous([ASK, FULL, _out()], start=1) == NONE_NEW
    assert _previous([ASK, FULL, _out()], start=3) == {
        "request": "OLD TASK",
        "outcome": "full_solution: REPORT",
    }


def test_a_start_beyond_the_history_keeps_the_whole_history():
    assert _previous([ASK, FULL, _out()], start=10) == {
        "request": "OLD TASK",
        "outcome": "full_solution: REPORT",
    }


def test_no_start_keeps_the_whole_history():
    assert _previous([ASK, FULL, _out()], start=None) == {
        "request": "OLD TASK",
        "outcome": "full_solution: REPORT",
    }


def test_an_open_ask_in_the_history_hides_the_outcome_only_in_the_current_contract():
    messages = [ASK, FULL, _out(), {"role": "user", "content": "continue"}]
    assert _previous(messages, start=None) == NONE_NEW
    assert _previous(messages, start=None, legacy=True) == {
        "request": "OLD TASK",
        "outcome": "full_solution: REPORT",
    }
