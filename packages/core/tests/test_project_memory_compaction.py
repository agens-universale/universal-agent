from types import SimpleNamespace
from unittest.mock import patch

from gc_core.project_memory import compaction
from gc_core.project_memory.compaction import CompactionResult, SOFT_APPEND_INPUT_LIMIT_CHARS


def _make_ctx(backend_mode: str = "chat_completions") -> SimpleNamespace:
    return SimpleNamespace(
        config_dict={"backend_api_mode": backend_mode, "model": "test-model"},
        auth_connector=None,
        logger=None,
    )


def test_soft_append_input_limit_constant():
    assert SOFT_APPEND_INPUT_LIMIT_CHARS == 3_000


def test_compact_text_happy_path():
    ctx = _make_ctx()
    with patch(
        "gc_core.summarize.llm.arequest_chat_completion_text",
        return_value="compressed body",
    ) as mocked:
        res = compaction.compact_text("very long original text", ctx)
    assert res == CompactionResult(
        ok=True, text="compressed body", error_type=None, error_message=None
    )
    assert mocked.called


def test_compact_text_strips_response():
    ctx = _make_ctx()
    with patch(
        "gc_core.summarize.llm.arequest_chat_completion_text",
        return_value="  compressed  \n\n",
    ):
        res = compaction.compact_text("text", ctx)
    assert res.ok is True
    assert res.text == "compressed"


def test_compact_text_projects_think_tags():
    ctx = _make_ctx()
    with patch(
        "gc_core.summarize.llm.arequest_chat_completion_text",
        return_value="<think>scratch</think>compressed",
    ):
        res = compaction.compact_text("text", ctx)
    assert res.ok is True
    assert res.text == "compressed"


def test_compact_text_logs_dirty_response():
    """Acceptance 08: aux log keeps dirty; return/store is clean."""
    logs: list[str] = []

    class _Logger:
        def info(self, msg, *args, **kwargs):
            try:
                logs.append(msg % args if args else str(msg))
            except Exception:
                logs.append(str(msg))

    ctx = _make_ctx()
    ctx.logger = _Logger()
    with patch(
        "gc_core.summarize.llm.arequest_chat_completion_text",
        return_value="<think>scratch</think>compressed",
    ):
        res = compaction.compact_text("text", ctx)
    assert res.ok is True
    assert res.text == "compressed"
    joined = "\n".join(logs)
    assert "summary_llm_result" in joined
    assert "<think>scratch</think>compressed" in joined


def test_compact_text_empty_response():
    ctx = _make_ctx()
    with patch(
        "gc_core.summarize.llm.arequest_chat_completion_text",
        return_value="   \n  ",
    ):
        res = compaction.compact_text("text", ctx)
    assert res.ok is False
    assert res.error_type == "empty_response"
    assert res.text == ""


def test_compact_text_backend_exception_caught():
    ctx = _make_ctx()
    with patch(
        "gc_core.project_memory.compaction.arequest_auxiliary_completion_text",
        side_effect=RuntimeError("network down"),
    ):
        res = compaction.compact_text("text", ctx)
    assert res.ok is False
    assert res.error_type == "backend_error"
    assert "network down" in res.error_message
    assert res.text == ""


def test_compact_text_backend_unsupported_mode():
    """Compaction needs chat_completions; in 'responses' mode return backend_unsupported."""
    ctx = _make_ctx(backend_mode="responses")
    res = compaction.compact_text("text", ctx)
    assert res.ok is False
    assert res.error_type == "backend_unsupported"
    assert res.text == ""


def test_compact_text_uses_prompt_template_as_system():
    """The prompt template content should appear in the system message sent to backend."""
    ctx = _make_ctx()
    captured = {}

    def fake_call(*, ctx, state, messages, max_tokens, request_label, **kwargs):
        captured["messages"] = messages
        captured["label"] = request_label
        return "compressed"

    with patch(
        "gc_core.summarize.llm.arequest_chat_completion_text", side_effect=fake_call
    ):
        compaction.compact_text("text body", ctx)

    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert "compaction assistant" in msgs[0]["content"].lower()
    assert msgs[1] == {"role": "user", "content": "text body"}
    assert captured["label"] == "project_memory_compaction"
