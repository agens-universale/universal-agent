import json
from pathlib import Path

from gc_core.run_context import acreate_run_context


def _base_config(tmp_path, **extra):
    cfg = {
        "logs_dump_dir": str(tmp_path),
        "save_logs": True,
        "logging_format": "jsonl",
        "console_trace": False,
        "backend_api_mode": "chat_completions",
        "completions_url": "http://localhost:1/v1/chat/completions",
        "model": "test-model",
        "launch_root": str(tmp_path),
    }
    cfg.update(extra)
    return cfg


def _events(log_path):
    out = []
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


async def test_ctx_trace_id_is_empty_when_absent(tmp_path):
    ctx = await acreate_run_context(_base_config(tmp_path))
    assert ctx.trace_id == ""
    events = _events(ctx.logging_path)
    assert not [e for e in events if e.get("event") == "trace_binding"]


async def test_ctx_trace_id_is_derived_from_config(tmp_path):
    ctx = await acreate_run_context(_base_config(tmp_path, trace_id="wf-42"))
    assert ctx.trace_id == "wf-42"


async def test_trace_binding_event_records_header_names(tmp_path):
    ctx = await acreate_run_context(_base_config(tmp_path, trace_id="wf-42"))
    binding = [
        e
        for e in _events(ctx.logging_path)
        if e.get("event") == "trace_binding"
    ]
    assert len(binding) == 1
    assert binding[0]["trace_id"] == "wf-42"
    assert binding[0]["model_header"] == "x_trace_id"
    assert binding[0]["mcp_header"] == "trace_id"


async def test_trace_binding_reflects_header_override(tmp_path):
    ctx = await acreate_run_context(
        _base_config(
            tmp_path, trace_id="wf-42", trace_id_header_name="X-Trace-Id"
        )
    )
    binding = [
        e
        for e in _events(ctx.logging_path)
        if e.get("event") == "trace_binding"
    ]
    assert binding[0]["model_header"] == "X-Trace-Id"
    assert binding[0]["mcp_header"] == "X-Trace-Id"
