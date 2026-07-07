"""TDD tests for the jsonl `event` instrumentation added to:
- tools/turn/ask_human.py
- tools/turn/intermediate_reply.py
- persistence/state_storage.py
- run_context.py

Each test drives the real code path with a jsonl-mode per-run logger and
asserts the expected event line was written.
"""
import builtins
import json

import pytest

import run_context as rc
from persistence.state_storage import dump_storage
from tools.turn.ask_human import AskHuman
from tools.turn.intermediate_reply import IntermediateReply
from utils.logging_module import create_run_logger


def _jsonl_cfg(tmp_path, **extra):
    cfg = {
        "logs_dump_dir": str(tmp_path),
        "save_logs": True,
        "console_trace": False,
        "logging_format": "jsonl",
    }
    cfg.update(extra)
    return cfg


def _read_events(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class _FakeCtx:
    def __init__(self, logger):
        self.logger = logger


# ---------------------------------------------------------------------------
# ask_human.py
# ---------------------------------------------------------------------------

def test_ask_human_question_and_response_events(tmp_path, monkeypatch):
    logger, path, _ = create_run_logger(_jsonl_cfg(tmp_path), run_id="ah1", agent_kind="main")

    answers = iter(["hello", ""])
    monkeypatch.setattr(builtins, "input", lambda: next(answers))

    tool = AskHuman(ctx=_FakeCtx(logger))
    out = json.loads(tool(question="Q?"))
    assert out["ok"] is True

    for h in logger.handlers:
        h.flush()
    events = {e["event"]: e for e in _read_events(path)}
    assert events["ask_human_question"]["question"] == "Q?"
    assert events["ask_human_response"]["response"] == "hello"


def test_ask_human_interrupted_event(tmp_path, monkeypatch):
    logger, path, _ = create_run_logger(_jsonl_cfg(tmp_path), run_id="ah2", agent_kind="main")

    def _raise():
        raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "input", _raise)

    tool = AskHuman(ctx=_FakeCtx(logger))
    out = json.loads(tool(question="Q?"))
    assert out["ok"] is False

    for h in logger.handlers:
        h.flush()
    events = [e for e in _read_events(path) if e["event"] == "ask_human_interrupted"]
    assert events


def test_ask_human_error_event(tmp_path, monkeypatch):
    logger, path, _ = create_run_logger(_jsonl_cfg(tmp_path), run_id="ah3", agent_kind="main")

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(builtins, "input", _raise)

    tool = AskHuman(ctx=_FakeCtx(logger))
    out = json.loads(tool(question="Q?"))
    assert out["ok"] is False

    for h in logger.handlers:
        h.flush()
    events = [e for e in _read_events(path) if e["event"] == "ask_human_error"]
    assert events and "boom" in events[0]["error"]


# ---------------------------------------------------------------------------
# intermediate_reply.py
# ---------------------------------------------------------------------------

def test_intermediate_reply_event(tmp_path):
    logger, path, _ = create_run_logger(_jsonl_cfg(tmp_path), run_id="ir1", agent_kind="main")

    tool = IntermediateReply(ctx=_FakeCtx(logger))
    out = json.loads(tool(answer="status update", artifact_path="report.md"))
    assert out["ok"] is True

    for h in logger.handlers:
        h.flush()
    events = [e for e in _read_events(path) if e["event"] == "intermediate_reply"]
    assert events
    assert events[0]["answer"] == "status update"
    assert events[0]["artifact_path"] == "report.md"


# ---------------------------------------------------------------------------
# persistence/state_storage.py
# ---------------------------------------------------------------------------

def test_dump_storage_logs_event(tmp_path):
    logger, path, _ = create_run_logger(_jsonl_cfg(tmp_path), run_id="ds1", agent_kind="main")

    dump_storage(str(tmp_path), logger=logger)

    for h in logger.handlers:
        h.flush()
    events = [e for e in _read_events(path) if e["event"] == "storage_dumped"]
    assert events
    assert events[0]["path"].startswith(str(tmp_path))


def test_dump_storage_without_logger_does_not_raise(tmp_path):
    # Additive: no logger passed -> no event, but no crash and human print stays.
    dump_storage(str(tmp_path))


# ---------------------------------------------------------------------------
# run_context.py
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_config_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rc,
        "build_tools_data",
        lambda cfg, on_progress=None: (
            {"tool_a": {}, "tool_b": {}},
            [],
            [],
            {},
        ),
    )
    return _jsonl_cfg(
        tmp_path,
        model="qwen2.5",
        completions_url="http://backend/v1/chat",
        backend_api_mode="chat_completions",
        log_level="debug",
    )


def test_agent_start_and_agent_tools_events(minimal_config_dict):
    ctx = rc.create_run_context(minimal_config_dict, run_id="rc1")

    for h in ctx.logger.handlers:
        h.flush()
    events = {e["event"]: e for e in _read_events(ctx.logging_path)}

    assert events["agent_start"]["model"] == "qwen2.5"
    assert events["agent_start"]["url"] == "http://backend/v1/chat"
    assert events["agent_start"]["backend_api_mode"] == "chat_completions"

    assert events["agent_tools"]["tools"] == ["tool_a", "tool_b"]
    assert events["agent_tools"]["count"] == 2
