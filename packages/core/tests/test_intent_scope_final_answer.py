import logging
import os

from gc_core.intent_scope import final_answer as fa
from gc_core.intent_scope.append import append_final_answer_to_current
from gc_core.intent_scope.final_answer import amaybe_record_final_answer_intent
from gc_core.intent_scope.summarizer import asummarize_final_answer_line
from gc_testkit import run_coro
from gc_testkit.settings import make_settings


def test_summarize_empty_returns_empty():
    assert (
        run_coro(
            asummarize_final_answer_line(
                ctx=object(), state=object(), assistant_text="  "
            )
        )
        == ""
    )


def test_final_answer_prompt_file_exists_and_nonempty():
    from gc_core.intent_scope.summarizer import (
        _resolve_final_answer_prompt_path,
    )

    class _Ctx:
        intent_scope = None

    path = _resolve_final_answer_prompt_path(_Ctx())
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fd:
        assert fd.read().strip()


class _FakeScope:
    def __init__(self, threshold=2000, max_raw=5000, fallback=True):
        self.max_final_answer_raw_chars = threshold
        self.max_raw_record_chars = max_raw
        self.fallback_raw_on_llm_error = fallback
        self.max_summary_chars = 500
        self.max_tool_output_chars = 8000
        self.intent_scope_final_answer_summary_prompt_path = None


class _FakeState:
    def __init__(self):
        self.current_user_intent = []
        self.user_turn_seq = 0


class _FakeCtx:
    def __init__(self, scope, backend="chat_completions"):
        self.intent_scope = scope
        loaded = make_settings({"backend.api_mode": backend})
        self.settings = loaded.settings
        self.runtime = loaded.runtime
        self.logger = logging.getLogger("test.final_answer")


def test_short_prose_recorded_raw_as_is():
    state = _FakeState()
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text="short answer",
            state=state,
            ctx=_FakeCtx(_FakeScope()),
        )
    )
    assert len(state.current_user_intent) == 1
    rec = state.current_user_intent[0]
    assert rec.action == "final_answer"
    assert rec.content == "short answer"


def test_long_prose_recorded_as_summary(monkeypatch):
    async def _fake_summary(**kw):
        return "SUMMARY LINE"

    monkeypatch.setattr(fa, "asummarize_final_answer_line", _fake_summary)
    state = _FakeState()
    long_text = "x" * 50
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text=long_text,
            state=state,
            ctx=_FakeCtx(_FakeScope(threshold=10)),
        )
    )
    assert len(state.current_user_intent) == 1
    assert state.current_user_intent[0].content == "SUMMARY LINE"


def test_long_prose_summary_fails_fallback_raw_truncated(monkeypatch):
    async def _empty_summary(**kw):
        return ""

    monkeypatch.setattr(fa, "asummarize_final_answer_line", _empty_summary)
    state = _FakeState()
    long_text = "y" * 50
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text=long_text,
            state=state,
            ctx=_FakeCtx(_FakeScope(threshold=10, max_raw=20, fallback=True)),
        )
    )
    assert len(state.current_user_intent) == 1
    assert len(state.current_user_intent[0].content) <= 20


def test_long_prose_summary_fails_no_fallback_skips(monkeypatch):
    async def _empty_summary(**kw):
        return ""

    monkeypatch.setattr(fa, "asummarize_final_answer_line", _empty_summary)
    state = _FakeState()
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text="z" * 50,
            state=state,
            ctx=_FakeCtx(_FakeScope(threshold=10, fallback=False)),
        )
    )
    assert state.current_user_intent == []


def test_responses_backend_skips():
    state = _FakeState()
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text="answer",
            state=state,
            ctx=_FakeCtx(_FakeScope(), backend="responses"),
        )
    )
    assert state.current_user_intent == []


def test_empty_text_skips():
    state = _FakeState()
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text="   ", state=state, ctx=_FakeCtx(_FakeScope())
        )
    )
    assert state.current_user_intent == []


def test_dedup_same_short_prose_once():
    state = _FakeState()
    ctx = _FakeCtx(_FakeScope())
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text="same", state=state, ctx=ctx
        )
    )
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text="same", state=state, ctx=ctx
        )
    )
    assert len(state.current_user_intent) == 1


def test_threshold_zero_routes_to_summary(monkeypatch):
    async def _fake_summary(**kw):
        return "S"

    monkeypatch.setattr(fa, "asummarize_final_answer_line", _fake_summary)
    state = _FakeState()
    run_coro(
        amaybe_record_final_answer_intent(
            assistant_text="hi",
            state=state,
            ctx=_FakeCtx(_FakeScope(threshold=0)),
        )
    )
    assert len(state.current_user_intent) == 1
    assert state.current_user_intent[0].content == "S"


def test_append_final_answer_to_current_empty_returns_false():
    state = _FakeState()
    assert append_final_answer_to_current(state, "   ") is False
    assert state.current_user_intent == []


def test_append_final_answer_to_current_appends_distinct():
    state = _FakeState()
    assert append_final_answer_to_current(state, "one") is True
    assert append_final_answer_to_current(state, "two") is True
    assert len(state.current_user_intent) == 2


def test_append_final_answer_to_current_dedup_same():
    state = _FakeState()
    assert append_final_answer_to_current(state, "dup") is True
    assert append_final_answer_to_current(state, "dup") is False
    assert len(state.current_user_intent) == 1
