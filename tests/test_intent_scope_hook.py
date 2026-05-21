from types import SimpleNamespace

from config_modules.intent_scope import IntentScopeConfig
from intent_scope.hook import maybe_update_intent_scope_from_tool


class _RecordingState:
    def __init__(self):
        self.current_user_intent: list[str] = []
        self.deltas: list[tuple[str, str]] = []

    def append_intent_scope_delta(self, summary_line: str, *, source: str = "") -> None:
        line = str(summary_line or "").strip()
        if not line or line in self.current_user_intent:
            return
        self.current_user_intent.append(line)
        self.deltas.append((line, source))


def _ctx(*, backend: str, tools: list[str], fallback: bool = False):
    return SimpleNamespace(
        config_dict={"backend_api_mode": backend},
        intent_scope=IntentScopeConfig(
            tools=tools,
            fallback_raw_on_llm_error=fallback,
        ),
        logger=None,
    )


def test_hook_skipped_on_responses_backend():
    state = _RecordingState()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"hi"}',
        state=state,
        ctx=_ctx(backend="responses", tools=["ask_human"]),
    )
    assert state.deltas == []
    assert state.current_user_intent == []


def test_hook_skipped_without_intent_scope_on_ctx():
    state = _RecordingState()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"hi"}',
        state=state,
        ctx=SimpleNamespace(
            config_dict={"backend_api_mode": "chat_completions"},
            logger=None,
        ),
    )
    assert state.deltas == []


def test_hook_skipped_when_tools_empty():
    state = _RecordingState()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"hi"}',
        state=state,
        ctx=_ctx(backend="chat_completions", tools=[]),
    )
    assert state.deltas == []


def test_hook_appends_delta_after_llm_summary(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: "User confirmed option B.",
    )
    state = _RecordingState()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"B"}',
        state=state,
        ctx=_ctx(backend="chat_completions", tools=["ask_human"]),
    )
    assert state.current_user_intent == ["User confirmed option B."]
    assert state.deltas == [("User confirmed option B.", "ask_human")]


def test_hook_submit_solution_uses_final_output(monkeypatch):
    captured = {}

    def _fake_summarize(**kwargs):
        captured["tool_name"] = kwargs["tool_name"]
        captured["output"] = kwargs["tool_output_raw"]
        return "Attempted to close with report."

    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line", _fake_summarize
    )
    state = _RecordingState()
    reject_output = (
        '{"ok":false,"rejected_by_finish_gate":true,"finish_gate":{"feedback":"fix tests"}}'
    )
    maybe_update_intent_scope_from_tool(
        tool_name="submit_solution",
        tool_output_raw=reject_output,
        tool_arguments='{"task_report":"done"}',
        state=state,
        ctx=_ctx(backend="chat_completions", tools=["submit_solution"]),
    )
    assert captured["tool_name"] == "submit_solution"
    assert "rejected_by_finish_gate" in captured["output"]
    assert state.current_user_intent == ["Attempted to close with report."]


def test_hook_fallback_raw_when_llm_empty(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: "",
    )
    state = _RecordingState()
    raw = '{"ok":false,"error":"validation"}'
    maybe_update_intent_scope_from_tool(
        tool_name="submit_solution",
        tool_output_raw=raw,
        state=state,
        ctx=_ctx(
            backend="chat_completions",
            tools=["submit_solution"],
            fallback=True,
        ),
    )
    assert len(state.current_user_intent) == 1
    assert state.current_user_intent[0].startswith("[intent_scope:raw] ")


def test_hook_dedup_within_current(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: "same line",
    )
    state = _RecordingState()
    ctx = _ctx(backend="chat_completions", tools=["ask_human"])
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw="a",
        state=state,
        ctx=ctx,
    )
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw="b",
        state=state,
        ctx=ctx,
    )
    assert state.current_user_intent == ["same line"]
    assert len(state.deltas) == 1
