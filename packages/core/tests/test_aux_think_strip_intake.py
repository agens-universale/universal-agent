"""TDD спека 08: strip на aux LLM choke; не на tool args/outputs."""

import json
from types import SimpleNamespace

import yaml

from gc_core.config import load_config
from gc_core.intent_guard.validation_result import parse_validator_response
from gc_core.intent_scope.summarizer import build_fallback_aggregated_content
from gc_core.intent_log.user_turn import seed_subagent_current_user_intent
from gc_core.project_memory import compaction
from gc_core.security import command_moderation
from gc_core.summarize import llm as summary_llm
from gc_core.summarize.summary_validation import merge_repaired_summary
from gc_core.summarize.thread_summarizer import summarize_dialog_via_chat_completions
from gc_core.summarize.working_memory_gateway import parse_memory_deltas
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests

_DIRTY_EXTRA = (
    "<think>t</think><redacted_thinking>r</redacted_thinking>"
)


def test_fallback_aggregated_keeps_tool_output_as_is():
    ctx = SimpleNamespace(
        intent_scope=SimpleNamespace(max_summary_chars=0, max_tool_output_chars=10_000)
    )
    raw = "<think>hide</think>visible facts"
    assert build_fallback_aggregated_content(ctx=ctx, tool_output_raw=raw) == raw


def test_request_auxiliary_projects_and_logs_dirty(monkeypatch):
    logged = {}

    def _fake_request(**kwargs):
        return "<think>r</think>one-line summary"

    def _fake_log(*, logger, label, messages, output_text):
        logged["output_text"] = output_text
        logged["label"] = label

    monkeypatch.setattr(summary_llm, "request_chat_completion_text", _fake_request)
    monkeypatch.setattr(summary_llm, "log_llm_prompt_and_result", _fake_log)

    ctx = SimpleNamespace(
        logger=object(),
        auxiliary_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={},
        ),
    )
    out = summary_llm.request_summary_text(
        ctx=ctx,
        state=SimpleNamespace(),
        messages=[{"role": "user", "content": "hi"}],
        request_label="intent_scope:exec_command",
        consumer="intent_scope",
    )
    assert out == "one-line summary"
    assert logged["output_text"] == "<think>r</think>one-line summary"
    assert logged["label"] == "intent_scope:exec_command"


def test_parse_memory_deltas_expects_projected_input():
    # Aux choke projects before parse; parser itself does not strip.
    raw = "<goal>ship 08</goal>\n<state>coding</state>"
    deltas, present = parse_memory_deltas(raw)
    assert "goal" in present
    assert deltas.goal == "ship 08"
    assert deltas.state == "coding"


def test_merge_repaired_summary_on_clean_inputs():
    from gc_core.summarize.summary_spec import assemble_summary_xml, section_tags

    repaired = assemble_summary_xml({t: f"- {t}" for t in section_tags()})
    merged = merge_repaired_summary(invalid_text=repaired, repaired_text=repaired)
    assert "<summary>" in merged
    assert "- task_and_context" in merged


def test_seed_subagent_keeps_assignment_as_is():
    raw = "<think>internal</think>Do the task"
    records = seed_subagent_current_user_intent(raw)
    assert len(records) == 1
    assert records[0].content == raw


def test_command_judge_uses_project_and_config_extra_tag():
    parsed = command_moderation._parse_judge_json(
        _DIRTY_EXTRA
        + '{"verdict":"safe","reasons":["ok"],"risk_flags":[],"confidence":0.9}',
        tags=["redacted_thinking"],
    )
    assert parsed["verdict"] == "safe"


def test_load_config_extras_strip_on_aux_sinks(tmp_path, monkeypatch):
    """YAML extras → load_config → summary / compaction / finish-gate / judge."""
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "backend": {"api_mode": "chat_completions"},
                "strip_tags_projection": {"tags": ["redacted_thinking"]},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_config(str(cfg_path))

    monkeypatch.setattr(
        summary_llm, "log_llm_prompt_and_result", lambda **kwargs: None
    )
    ctx = SimpleNamespace(
        logger=object(),
        config_dict=loaded,
        auxiliary_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={},
        ),
    )
    state = SimpleNamespace()

    monkeypatch.setattr(
        summary_llm,
        "request_chat_completion_text",
        lambda **kwargs: _DIRTY_EXTRA + "one-line summary",
    )
    assert (
        summary_llm.request_summary_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "user", "content": "hi"}],
            request_label="intent_scope:exec_command",
            consumer="intent_scope",
        )
        == "one-line summary"
    )

    monkeypatch.setattr(
        summary_llm,
        "request_chat_completion_text",
        lambda **kwargs: _DIRTY_EXTRA + "compressed",
    )
    compact_res = compaction.compact_text("long memory", ctx, state)
    assert compact_res.ok is True
    assert compact_res.text == "compressed"

    finish_payload = {
        "action": "accept",
        "trace_ok": True,
        "report_ok": True,
        "is_question": False,
        "trace_gaps": [],
        "report_mismatches": [],
        "coverage_score": 1.0,
    }
    monkeypatch.setattr(
        summary_llm,
        "request_chat_completion_text",
        lambda **kwargs: _DIRTY_EXTRA + json.dumps(finish_payload),
    )
    projected_finish = summary_llm.request_auxiliary_completion_text(
        ctx=ctx,
        state=state,
        messages=[{"role": "user", "content": "validate"}],
        max_tokens=100,
        request_label="intent_guard_finish_gate",
        consumer="finish_gate",
        log_prompt=False,
    )
    assert projected_finish == json.dumps(finish_payload)
    parsed_finish = parse_validator_response(projected_finish)
    assert parsed_finish is not None
    assert parsed_finish.action == "accept"

    parsed_judge = command_moderation._parse_judge_json(
        _DIRTY_EXTRA
        + '{"verdict":"safe","reasons":["ok"],"risk_flags":[],"confidence":0.9}',
        tags=loaded["strip_tags_projection"]["tags"],
    )
    assert parsed_judge["verdict"] == "safe"


def test_aux_always_strips_think_even_when_config_disables_it(tmp_path, monkeypatch):
    """strip_think: false — только основной трейс; aux всегда режет <think>."""
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "backend": {"api_mode": "chat_completions"},
                "strip_tags_projection": {"strip_think": False, "tags": []},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_config(str(cfg_path))
    assert loaded["strip_tags_projection"]["strip_think"] is False

    monkeypatch.setattr(
        summary_llm, "log_llm_prompt_and_result", lambda **kwargs: None
    )
    ctx = SimpleNamespace(
        logger=object(),
        config_dict=loaded,
        auxiliary_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={},
        ),
    )
    state = SimpleNamespace()
    dirty = "<think>secret reasoning</think>"

    monkeypatch.setattr(
        summary_llm,
        "request_chat_completion_text",
        lambda **kwargs: dirty + "one-line summary",
    )
    assert (
        summary_llm.request_summary_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "user", "content": "hi"}],
            request_label="intent_scope:exec_command",
            consumer="intent_scope",
        )
        == "one-line summary"
    )

    finish_payload = {
        "action": "accept",
        "trace_ok": True,
        "report_ok": True,
        "is_question": False,
        "trace_gaps": [],
        "report_mismatches": [],
        "coverage_score": 1.0,
    }
    monkeypatch.setattr(
        summary_llm,
        "request_chat_completion_text",
        lambda **kwargs: dirty + json.dumps(finish_payload),
    )
    projected_finish = summary_llm.request_auxiliary_completion_text(
        ctx=ctx,
        state=state,
        messages=[{"role": "user", "content": "validate"}],
        max_tokens=100,
        request_label="intent_guard_finish_gate",
        consumer="finish_gate",
        log_prompt=False,
    )
    assert projected_finish == json.dumps(finish_payload)
    parsed_finish = parse_validator_response(projected_finish)
    assert parsed_finish is not None
    assert parsed_finish.action == "accept"

    parsed_judge = command_moderation._parse_judge_json(
        dirty + '{"verdict":"safe","reasons":["ok"],"risk_flags":[],"confidence":0.9}'
    )
    assert parsed_judge["verdict"] == "safe"


def test_summarizer_history_strips_think_even_when_config_disables_it(monkeypatch):
    """История в промпте summarizer/patch — без ``<think>``, даже при strip_think: false."""
    seen_user_prompts: list[str] = []

    def _fake_request(**kwargs):
        messages = kwargs.get("messages") or []
        if messages:
            seen_user_prompts.append(str(messages[-1].get("content") or ""))
        return valid_summary_xml_for_tests(seed="clean-history")

    monkeypatch.setattr(
        "gc_core.summarize.thread_summarizer.request_summary_text", _fake_request
    )
    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.request_summary_text", _fake_request
    )
    empty = SimpleNamespace(request_messages=[], raw_response="")
    monkeypatch.setattr(
        "gc_core.summarize.memory_suggestions.request_patch_replace",
        lambda **_k: empty,
    )
    monkeypatch.setattr(
        "gc_core.summarize.prompt_loading.load_prompt_template", lambda _p: "prompt"
    )

    ctx = SimpleNamespace(
        logger=None,
        config_dict={
            "strip_tags_projection": {"strip_think": False, "tags": []},
            "summary_params": {
                "chat_completions_summary_trigger_chars": 999999,
                "incremental_batch_chars": 999999,
                "max_accumulated_summary_chars": 20000,
                "last_dialog_steps_count": 5,
                "last_dialog_step_message_max_chars": 5000,
            },
        },
    )
    result = summarize_dialog_via_chat_completions(
        ctx=ctx,
        state=SimpleNamespace(default_model="test-model"),
        full_history_messages=[
            {"type": "message", "role": "user", "content": "do the task"},
            {
                "type": "message",
                "role": "assistant",
                "content": "<think>secret reasoning</think>visible answer",
            },
        ],
    )
    assert seen_user_prompts
    joined = "\n".join(seen_user_prompts)
    assert "<think" not in joined
    assert "secret reasoning" not in joined
    assert "visible answer" in joined
    assert "<think" not in result["last_dialog_steps"]
    assert "visible answer" in result["last_dialog_steps"]
