"""Правила runtime над ``RuntimeStatus`` дают ровно то, что прежние
правила над meta develop: оракул — старая функция, применённая к meta и
разложенная обратно ``parts_from_meta`` (значения и явно заданные поля)."""

import copy

import pytest
from gc_core.threads import runtime_rules as rules
from gc_core.threads.meta_map import parts_from_meta
from legacy_runtime_oracle import (
    apply_runtime_flag_to_meta,
    bump_image_seq,
    bump_live_volume_for_messages,
    live_volume_from_messages,
    merge_runtime_into_meta,
    reset_run_flags_in_meta,
    runtime_section,
)

CASES = [
    {},
    {"runtime": {}},
    {"runtime": {"summarizing": True}},
    {
        "runtime": {
            "turn": "running",
            "live_msg_count": 3,
            "live_history_chars": 100,
        }
    },
    {
        "runtime": {
            "image_seq": 2,
            "finalizing": True,
            "finish_gate_checking": True,
            "trace_ids": ["a"],
            "mcp_tools": {"s": ["t"]},
            "custom": 1,
        }
    },
    {"runtime": {"image_seq": "битый", "live_msg_count": 1}},
]
IDS = ["нет", "пустая", "флаг", "ход", "всё", "битое"]
FLAGS = ["summarizing", "finalizing", "finish_gate_checking"]
IMAGE = {
    "role": "user",
    "content": [{"type": "input_text", "text": "[Изображение #5] смотри"}],
}
FRESH = [{"role": "user", "content": "привет"}, IMAGE]


def _runtime(meta):
    return parts_from_meta(meta).runtime


def _same(got, expected):
    assert got.model_dump() == expected.model_dump()
    assert got.model_fields_set == expected.model_fields_set
    assert (got.model_extra or {}) == (expected.model_extra or {})


def _old(meta, change):
    meta = copy.deepcopy(meta)
    change(meta)
    return _runtime(meta)


@pytest.mark.parametrize("meta", CASES, ids=IDS)
@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("active", [True, False])
def test_with_flag(meta, flag, active):
    expected = _old(
        meta, lambda m: apply_runtime_flag_to_meta(m, flag, active=active)
    )
    _same(rules.with_flag(_runtime(meta), flag, active=active), expected)


def test_unknown_flag_is_refused():
    with pytest.raises(ValueError):
        rules.with_flag(_runtime({}), "nope", active=True)


@pytest.mark.parametrize("meta", CASES, ids=IDS)
def test_reset_run_flags(meta):
    expected = _old(meta, reset_run_flags_in_meta)
    _same(rules.reset_run_flags(_runtime(meta)), expected)


def _mark_turn(meta, running):
    """Прежняя отметка хода (SessionPort.amark_turn) над meta: без
    изменения — без записи."""
    runtime = dict(meta.get("runtime") or {})
    before = runtime.get("turn")
    if running:
        runtime["turn"] = "running"
    else:
        runtime.pop("turn", None)
    if runtime.get("turn") == before:
        return
    if runtime:
        meta["runtime"] = runtime
    else:
        meta.pop("runtime", None)


@pytest.mark.parametrize("meta", CASES, ids=IDS)
@pytest.mark.parametrize("running", [True, False])
def test_with_turn(meta, running):
    expected = _old(meta, lambda m: _mark_turn(m, running))
    _same(rules.with_turn(_runtime(meta), running=running), expected)


@pytest.mark.parametrize("meta", CASES, ids=IDS)
def test_bumped_live_volume(meta):
    expected = _old(meta, lambda m: bump_live_volume_for_messages(m, FRESH))
    _same(rules.bumped_live_volume(_runtime(meta), FRESH), expected)


@pytest.mark.parametrize("meta", CASES, ids=IDS)
def test_with_live_volume(meta):
    expected = _old(
        meta,
        lambda m: merge_runtime_into_meta(m, live_volume_from_messages(FRESH)),
    )
    _same(rules.with_live_volume(_runtime(meta), FRESH), expected)


@pytest.mark.parametrize("meta", CASES, ids=IDS)
def test_bumped_image_seq(meta):
    expected = _old(meta, lambda m: bump_image_seq(m, FRESH))
    _same(rules.bumped_image_seq(_runtime(meta), FRESH), expected)


def _old_trace(meta, trace_id):
    existing = runtime_section(meta).get("trace_ids")
    ids = [str(x) for x in existing] if isinstance(existing, list) else []
    if trace_id in ids:
        return
    ids.append(trace_id)
    merge_runtime_into_meta(meta, {"trace_ids": ids})


@pytest.mark.parametrize("meta", CASES, ids=IDS)
@pytest.mark.parametrize("trace_id", ["a", "b"])
def test_with_trace_id(meta, trace_id):
    expected = _old(meta, lambda m: _old_trace(m, trace_id))
    _same(rules.with_trace_id(_runtime(meta), trace_id), expected)


@pytest.mark.parametrize("meta", CASES, ids=IDS)
def test_with_mcp_tools(meta):
    snap = {"server": ["tool"]}
    expected = _old(
        meta, lambda m: merge_runtime_into_meta(m, {"mcp_tools": snap})
    )
    _same(rules.with_mcp_tools(_runtime(meta), snap), expected)
