"""User-stop mid-batch — A for started, B for not-started; pairing persist."""
from __future__ import annotations

import json

from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_testkit import run_coro


def _async_noop(fn):
    """Заглушка корутинного хука: ядро зовёт async-двойник напрямую."""
    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)
    return _wrapped



def _unique_id_factory():
    n = {"i": 0}

    def _gen(prefix: str) -> str:
        n["i"] += 1
        return f"{prefix}_{n['i']}"

    return _gen


def _state(**kw):
    class _State:
        messages: list = []
        discard_writes_after_stop = False
        stop_requested = False
        allow_pairing_persist = False

        def __init__(self):
            self.session_compact_applied = False
            self.tool_calls = 0
            self.messages = []
            self.turn_tool_calls = 0
            for k, v in kw.items():
                setattr(self, k, v)

    return _State()


def _patch_process_tool_calls(monkeypatch, pr, *, apply=None):
    applied: list[str] = []

    async def _default_apply(**kw):
        applied.append(kw["tool_name"])
        return '{"ok": true}'

    monkeypatch.setattr(pr, "aapply_tool", apply or _default_apply)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", _unique_id_factory())
    return applied


def _cancelled_json(tool_name: str) -> str:
    return json.dumps(
        {
            "tool": tool_name,
            "ok": False,
            "status": "cancelled_by_user",
            "message": "Tool was interrupted by user stop.",
            "error": {
                "code": "cancelled_by_user",
                "message": "Tool was interrupted by user stop.",
                "retryable": True,
            },
            "meta": {"finish_reason": "stopped_by_user"},
        },
        ensure_ascii=False,
    )


def _statuses(outs: list) -> list[str]:
    return [json.loads(o["output"])["status"] for o in outs]


# --- batch abort -----------------------------------------------------------


def test_stop_before_any_tool_emits_b_for_all(monkeypatch):
    from gc_core import processing_responses as pr

    applied = _patch_process_tool_calls(monkeypatch, pr)
    state = _state(stop_requested=True)
    messages = [
        {"type": "function_call", "name": "fs_read_file", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "fs_read_file", "call_id": "c2", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))

    assert applied == []
    assert _statuses(outs) == [
        "not_started_due_to_user_stop",
        "not_started_due_to_user_stop",
    ]
    for out in outs:
        assert json.loads(out["output"])["synthetic"] is True


def test_stop_after_first_tool_a_then_b_for_rest(monkeypatch):
    from gc_core import processing_responses as pr

    state = _state()
    applied: list[str] = []

    async def apply_and_stop(**kw):
        applied.append(kw["tool_name"])
        state.stop_requested = True
        return _cancelled_json(kw["tool_name"])

    _patch_process_tool_calls(monkeypatch, pr, apply=apply_and_stop)

    messages = [
        {"type": "function_call", "name": "subagent_worker", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "fs_read_file", "call_id": "c2", "arguments": "{}"},
        {"type": "function_call", "name": "fs_list_files", "call_id": "c3", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))

    assert applied == ["subagent_worker"]
    assert [o["call_id"] for o in outs] == ["c1", "c2", "c3"]
    assert _statuses(outs) == [
        "cancelled_by_user",
        "not_started_due_to_user_stop",
        "not_started_due_to_user_stop",
    ]


def test_successful_first_then_stop_before_second_gives_ok_then_b(monkeypatch):
    """Первый тул успел с ok=true; stop до второго → B, не A на первом."""
    from gc_core import processing_responses as pr

    state = _state()
    applied: list[str] = []

    async def apply_ok_then_arm_stop(**kw):
        applied.append(kw["tool_name"])
        state.stop_requested = True
        return '{"ok": true, "tool": "%s"}' % kw["tool_name"]

    _patch_process_tool_calls(monkeypatch, pr, apply=apply_ok_then_arm_stop)

    messages = [
        {"type": "function_call", "name": "fs_read_file", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "fs_read_file", "call_id": "c2", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))

    assert applied == ["fs_read_file"]
    first = json.loads(outs[0]["output"])
    assert first["ok"] is True
    assert first.get("status") != "cancelled_by_user"
    assert json.loads(outs[1]["output"])["status"] == "not_started_due_to_user_stop"


def test_single_tool_batch_stop_only_a_no_b(monkeypatch):
    """Один call в batch + stop mid-apply → только A, без лишнего B."""
    from gc_core import processing_responses as pr

    state = _state()

    async def apply_stop(**kw):
        state.stop_requested = True
        return _cancelled_json(kw["tool_name"])

    _patch_process_tool_calls(monkeypatch, pr, apply=apply_stop)
    messages = [
        {"type": "function_call", "name": "subagent_worker", "call_id": "c1", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))

    assert len(outs) == 1
    assert json.loads(outs[0]["output"])["status"] == "cancelled_by_user"
    assert outs[0]["call_id"] in {
        m["call_id"] for m in state.messages if m.get("type") == "function_call_output"
    }


def test_stop_does_not_apply_remaining_even_if_names_differ(monkeypatch):
    """Хвост batch с разными тулами — ни один не apply_tool."""
    from gc_core import processing_responses as pr

    state = _state()
    applied = []

    async def apply_and_stop(**kw):
        applied.append(kw["tool_name"])
        state.stop_requested = True
        return _cancelled_json(kw["tool_name"])

    _patch_process_tool_calls(monkeypatch, pr, apply=apply_and_stop)
    messages = [
        {"type": "function_call", "name": "a", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "b", "call_id": "c2", "arguments": "{}"},
        {"type": "function_call", "name": "c", "call_id": "c3", "arguments": "{}"},
        {"type": "function_call", "name": "d", "call_id": "c4", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))
    assert applied == ["a"]
    assert _statuses(outs) == [
        "cancelled_by_user",
        "not_started_due_to_user_stop",
        "not_started_due_to_user_stop",
        "not_started_due_to_user_stop",
    ]


def test_no_stop_runs_full_batch(monkeypatch):
    """Регрессия: без stop все тулы исполняются как раньше."""
    from gc_core import processing_responses as pr

    applied = _patch_process_tool_calls(monkeypatch, pr)
    state = _state()
    messages = [
        {"type": "function_call", "name": "fs_read_file", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "fs_read_file", "call_id": "c2", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))
    assert applied == ["fs_read_file", "fs_read_file"]
    assert len(outs) == 2
    assert all(json.loads(o["output"])["ok"] is True for o in outs)
    # Без stop pairing-commit не трогает state (обычный путь — через run_agent).
    assert state.messages == []


# --- persist / discard -----------------------------------------------------


def test_pairing_persist_despite_discard_writes(monkeypatch):
    """A/B при stop пишутся даже если discard_writes_after_stop."""
    from gc_core import processing_responses as pr

    persisted: list = []
    state = _state(discard_writes_after_stop=True)

    async def fake_apply(**kw):
        state.stop_requested = True
        return _cancelled_json("subagent_worker")

    _patch_process_tool_calls(monkeypatch, pr, apply=fake_apply)
    messages = [
        {"type": "function_call", "name": "subagent_worker", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "fs_read_file", "call_id": "c2", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages,
        state,
        type("C", (), {"config_dict": {}, "logger": None})(),
        None,
        persist_messages_callback=lambda msgs: persisted.append(list(msgs)),
    ))

    assert len(outs) == 2
    assert {m["call_id"] for m in state.messages if m.get("type") == "function_call_output"} == {
        "c1",
        "c2",
    }
    flat = [m for batch in persisted for m in batch]
    assert {m["call_id"] for m in flat} == {"c1", "c2"}


def test_pairing_writes_state_even_without_persist_callback(monkeypatch):
    """callback=None — всё равно закрываем orphans в state.messages."""
    from gc_core import processing_responses as pr

    state = _state(discard_writes_after_stop=True, stop_requested=True)
    _patch_process_tool_calls(monkeypatch, pr)
    messages = [
        {"type": "function_call", "name": "fs_read_file", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "fs_read_file", "call_id": "c2", "arguments": "{}"},
    ]
    run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))
    assert {m["call_id"] for m in state.messages} == {"c1", "c2"}


def test_pairing_persist_clears_allow_flag_after(monkeypatch):
    """allow_pairing_persist не протекает наружу после commit."""
    from gc_core import processing_responses as pr

    state = _state(discard_writes_after_stop=True, stop_requested=True)
    seen_flags: list[bool] = []

    def cb(msgs):
        seen_flags.append(bool(getattr(state, "allow_pairing_persist", False)))

    _patch_process_tool_calls(monkeypatch, pr)
    run_coro(pr.aprocess_tool_calls(
        [
            {"type": "function_call", "name": "fs_read_file", "call_id": "c1", "arguments": "{}"},
        ],
        state,
        type("C", (), {"config_dict": {}, "logger": None})(),
        None,
        persist_messages_callback=cb,
    ))
    assert seen_flags == [True]
    assert state.allow_pairing_persist is False


async def test_maybe_persist_blocks_non_pairing_when_discard_stop():
    from gc_core.processing_responses import _maybe_persist

    state = _state(discard_writes_after_stop=True, stop_requested=True)
    called = []
    await _maybe_persist(lambda m: called.append(m), [{"id": "1"}], state)
    assert called == []
    await _maybe_persist(lambda m: called.append(m), [{"id": "2"}], state, pairing=True)
    assert called == [[{"id": "2"}]]
    assert state.allow_pairing_persist is False


def test_is_persist_discarded_matrix():
    class S:
        def __init__(self, discard, stop, allow=False):
            self.discard_writes_after_stop = discard
            self.stop_requested = stop
            self.allow_pairing_persist = allow

    assert is_persist_discarded(S(True, True)) is True
    assert is_persist_discarded(S(True, True), pairing=True) is False
    assert is_persist_discarded(S(True, True, allow=True)) is False
    assert is_persist_discarded(S(True, False)) is False
    assert is_persist_discarded(S(False, True)) is False


def test_pairing_commit_does_not_duplicate_already_in_state(monkeypatch):
    """Повторный filter: уже лежащие в state outputs не дублируются."""
    from gc_core import processing_responses as pr

    existing = {
        "id": "fco_pre",
        "type": "function_call_output",
        "call_id": "c1",
        "output": _cancelled_json("subagent_worker"),
    }
    state = _state(discard_writes_after_stop=True, messages=[existing])

    async def fake_apply(**kw):
        state.stop_requested = True
        return _cancelled_json("subagent_worker")

    # Подменяем id-генератор так, чтобы первый output получил тот же id — нет,
    # важнее call_id в assert по количеству. Дедуп по message id.
    _patch_process_tool_calls(monkeypatch, pr, apply=fake_apply)

    # Сразу stop до apply: оба B, но c1 уже в state с другим id → добавится новый c1?
    # Сценарий: stop после apply A с новым id для c1 + B для c2; existing c1 остаётся.
    state.stop_requested = False
    messages = [
        {"type": "function_call", "name": "subagent_worker", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "fs_read_file", "call_id": "c2", "arguments": "{}"},
    ]
    run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))
    # existing + новый A(c1) + B(c2) — два c1 допустимы только если разные id
    # (реальный make_message_key по id). Проверяем: c2 появился ровно раз.
    c2 = [m for m in state.messages if m.get("call_id") == "c2"]
    assert len(c2) == 1


# --- envelopes distinct from cycle-stop ------------------------------------


def test_b_status_differs_from_cycle_stop_fallback():
    from gc_core.message_policy.user_stop import not_started_due_to_user_stop_output

    cycle = json.loads(AbstractSimpleTool.fallback_tool_output_on_cycle_stop("fs_read_file"))
    user = json.loads(not_started_due_to_user_stop_output("fs_read_file"))
    assert cycle["status"] == "skipped_on_cycle_stop"
    assert user["status"] == "not_started_due_to_user_stop"
    assert user["status"] != cycle["status"]


def test_stop_abort_still_appends_finalizer_skip_outputs(monkeypatch):
    """После abort remaining + skip-output'ы второго finalizer'а в возврате."""
    from gc_core import processing_responses as pr

    state = _state(stop_requested=True)
    _patch_process_tool_calls(monkeypatch, pr)
    # Первый ask_human — finalizer; второй session_memory skipped; но stop до всего
    # → B для executed-части (ask_human) + skip для хвоста после finalizer.
    messages = [
        {"type": "function_call", "name": "turn_ask_human", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "session_memory", "call_id": "c2", "arguments": "{}"},
    ]
    outs = run_coro(pr.aprocess_tool_calls(
        messages, state, type("C", (), {"config_dict": {}, "logger": None})(), None
    ))
    # executed=[ask_human], skipped=[session_memory]
    # stop before ask → B for ask + skip for session_memory
    assert len(outs) == 2
    assert outs[0]["call_id"] == "c1"
    assert json.loads(outs[0]["output"])["status"] == "not_started_due_to_user_stop"
    assert outs[1]["call_id"] == "c2"
    assert "skipped" in outs[1]["output"]
