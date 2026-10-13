"""Судья пары: карточка хода и разбор оценки (спека pair-judge §3)."""
import json
from types import SimpleNamespace

import pytest

from gc_core.auto_routing.pair_judge import (
    PREVIOUS_MAX_CHARS, TASK_MAX_CHARS, build_pair_card, parse_pair_score,
)
from gc_core.intent_log import append_final_answer, append_user_ask, flush

SYS = {"role": "system", "content": "SYS"}


def _closed(ask, *, final=None):
    current = append_user_ask([], ask, user_turn_seq=0)
    if final is not None:
        current = append_final_answer(current, final, user_turn_seq=0)
    _, log, seq = flush(current, [], 0)
    return log, seq


def _state(ask="fix the login page", *, log=(), seq=0, messages=None, start=None):
    return SimpleNamespace(current_user_intent=append_user_ask([], ask, user_turn_seq=seq),
                           user_intent_log=list(log), user_turn_seq=seq,
                           messages=messages or [SYS], turn_messages_start=start)


def _finish(report, kind="partial_solution"):
    return {"type": "function_call", "name": "turn_finish", "call_id": "f1",
            "arguments": json.dumps({"solution_type": kind, "task": "t", "task_report": report,
                                     "handoff": "{}"})}


def test_the_first_turn_card_has_the_request_and_no_previous_turn():
    card = json.loads(build_pair_card(_state()))
    assert card == {"turn": 1, "earlier_history_compacted": False, "request": "fix the login page"}


def test_a_continuation_carries_the_previous_request_and_its_finish_report():
    log, seq = _closed("migrate the storage layer to postgres")
    messages = [SYS, {"role": "user", "content": "migrate the storage layer to postgres"},
                _finish("schema done, data layer left"),
                {"type": "function_call_output", "call_id": "f1", "output": '{"ok": true, "status": "submitted"}'},
                {"role": "user", "content": "go on"}]
    card = json.loads(build_pair_card(_state("go on", log=log, seq=seq, messages=messages, start=4)))
    assert card["turn"] == 2 and card["request"] == "go on"
    assert card["previous_turn"] == {"request": "migrate the storage layer to postgres",
                                     "outcome": "partial_solution: schema done, data layer left"}


def test_the_final_answer_of_the_previous_turn_wins_over_an_older_finish():
    log, seq = _closed("what does this flag do?", final="It switches the cache off.")
    messages = [SYS, _finish("old work", kind="full_solution")]
    card = json.loads(build_pair_card(_state("now remove it", log=log, seq=seq, messages=messages)))
    assert card["previous_turn"]["outcome"] == "It switches the cache off."


def test_without_an_outcome_the_previous_turn_has_only_the_request():
    log, seq = _closed("rename the module")
    card = json.loads(build_pair_card(_state("and the tests", log=log, seq=seq)))
    assert card["previous_turn"] == {"request": "rename the module", "outcome_available": False}


def test_long_texts_are_cut_to_the_card_limits():
    log, seq = _closed("p" * 5000, final="f" * 5000)
    card = json.loads(build_pair_card(_state("r" * 5000, log=log, seq=seq)))
    assert len(card["request"]) == TASK_MAX_CHARS
    assert len(card["previous_turn"]["request"]) == PREVIOUS_MAX_CHARS
    assert len(card["previous_turn"]["outcome"]) == PREVIOUS_MAX_CHARS


def test_a_dialog_summary_in_the_history_marks_the_history_compacted():
    summary = {"role": "user", "content": '<technical type="dialog_summary">earlier work</technical>'}
    card = json.loads(build_pair_card(_state(messages=[SYS, summary])))
    assert card["earlier_history_compacted"] is True


@pytest.mark.parametrize("text, expected", [
    ('{"score": 4, "reason": "several modules"}', (4, "several modules", None)),
    ('```json\n{"score": 2, "reason": "one file"}\n```', (2, "one file", None)),
    ('{"score": "3"}', (3, "", None)),
    ('{"score": 4.0}', (4, "", None)),
    ('{"score": 3.5}', (None, "", "bad_type")),
    ('{"score": true}', (None, "", "bad_type")),
    ('{"score": 0}', (None, "", "out_of_range")),
    ('{"score": 6}', (None, "", "out_of_range")),
    ('{"reason": "hard"}', (None, "", "no_score")),
    ('Here: {"score": 3}', (None, "", "bad_json")),
    ("the task is hard", (None, "", "bad_json")),
    ("", (None, "", "bad_json")),
], ids=["plain", "fenced", "digit_string", "whole_float", "fraction", "bool", "zero", "six",
        "no_score", "prose_around_json", "prose", "empty"])
def test_the_score_parser(text, expected):
    assert parse_pair_score(text) == expected


def _preflight_settings(**updates):
    from gc_testkit.settings import make_settings

    return make_settings({
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
        "backend.api_mode": "chat_completions",
        "level": "auto",
        "paths.chat_completions_routing_pair_judge_prompt_path": "/virtual/pair-prompt.md",
        **updates,
    }).settings


def test_preflight_reads_a_virtual_prompt_from_the_agent_store():
    from gc_core.auto_routing.pair_judge import require_prompt
    from gc_core.auto_routing.policy import RoutingParams
    from gc_testkit.memory_agent_store import MemoryAgentStore

    store = MemoryAgentStore(prompts={"routing_pair_judge": "Choose the initial pair."}, fallback=None)
    require_prompt(_preflight_settings(), RoutingParams(safe_level="auto"), store=store)
    assert store.reads == [("prompt", "routing_pair_judge")]


@pytest.mark.parametrize("updates", [
    {"agent.auto_routing": False},
    {"backend.api_mode": "responses"},
])
def test_preflight_skips_a_bad_prompt_when_auto_is_unavailable(updates):
    from gc_core.auto_routing.pair_judge import require_prompt
    from gc_core.auto_routing.policy import RoutingParams

    require_prompt(_preflight_settings(**updates), RoutingParams(safe_level="auto"))
