"""Focused emission tests for the jsonl event sites added in this task:

- src/security/command_moderation.py: command_moderation_judge_regex_fallback,
  command_moderation_judge_failed
- src/security/path_policy.py: security_policy_decision
- src/agent_executor.py (reached via src/cli.py's run path): log_path

Each test drives real code with a jsonl-mode per-run logger from
``create_run_logger`` and asserts the event line + expected keys appear.
"""
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from gc_core.utils.logging_module import create_run_logger
from gc_testkit import run_coro


def _make_logger(tmp_path, level="debug"):
    cfg = {
        "logging_format": "jsonl",
        "log_level": level,
        "save_logs": True,
        "console_trace": False,
        "logs_dump_dir": str(tmp_path),
    }
    logger, path, _ = create_run_logger(cfg, run_id="t", agent_kind="main")
    return logger, path


def _flush(logger):
    for h in logger.handlers:
        h.flush()


def _records(path):
    with open(path, encoding="utf-8") as fd:
        return [json.loads(line) for line in fd if line.strip()]


def _embedded_events(recs, event_name):
    """command_moderation.py/path_policy.py log via
    ``logger.warning("<event> %s", json.dumps(payload))`` (matching the fork), so the
    jsonl formatter's top-level "event" key is None and the payload is embedded as a
    JSON blob inside "msg". Extract those payloads here.
    """
    out = []
    prefix = f"{event_name} "
    for rec in recs:
        msg = rec.get("msg") or ""
        if msg.startswith(prefix):
            out.append(json.loads(msg[len(prefix):]))
    return out


# ---------------------------------------------------------------------------
# src/security/command_moderation.py
# ---------------------------------------------------------------------------


def test_command_moderation_judge_regex_fallback_event(tmp_path):
    from gc_core.security.command_moderation import _log_command_judge_regex_fallback

    logger, path = _make_logger(tmp_path)
    _log_command_judge_regex_fallback(logger, tool_name="exec_command", verdict="safe")
    _flush(logger)
    recs = _records(path)

    events = _embedded_events(recs, "command_moderation_judge_regex_fallback")
    assert events, recs
    assert events[0]["tool_name"] == "exec_command"
    assert events[0]["matched_verdict"] == "safe"


def test_command_moderation_judge_failed_event(tmp_path):
    from gc_core.security.command_moderation import _log_command_judge_failure
    from gc_core.security.path_policy import ToolSecurityContext

    logger, path = _make_logger(tmp_path)
    context = ToolSecurityContext(launch_root="/tmp/launch", logger=logger)
    _log_command_judge_failure(context, tool_name="exec_python", exc=RuntimeError("judge boom"))
    _flush(logger)
    recs = _records(path)

    events = _embedded_events(recs, "command_moderation_judge_failed")
    assert events, recs
    assert events[0]["tool_name"] == "exec_python"
    assert events[0]["error_type"] == "RuntimeError"
    assert events[0]["error"] == "judge boom"


# ---------------------------------------------------------------------------
# src/security/path_policy.py
# ---------------------------------------------------------------------------


def test_security_policy_decision_event(tmp_path):
    from gc_core.security.path_policy import ToolSecurityContext, audit_policy_decision

    logger, path = _make_logger(tmp_path)
    context = ToolSecurityContext(launch_root="/tmp/launch", logger=logger)
    audit_policy_decision(
        context,
        tool_name="fs_write",
        decision="blocked",
        reason_code="policy_deny",
        risk_flags=["outside_root"],
        error_type="write_guard_violation",
    )
    _flush(logger)
    recs = _records(path)

    events = _embedded_events(recs, "security_policy_decision")
    assert events, recs
    ev = events[0]
    assert ev["tool_name"] == "fs_write"
    assert ev["decision"] == "blocked"
    assert ev["reason_code"] == "policy_deny"
    assert ev["risk_flags"] == ["outside_root"]
    assert ev["error_type"] == "write_guard_violation"


# ---------------------------------------------------------------------------
# src/agent_executor.py: log_path (fork's src/cli.py event; main's console_trace
# print of the log file path lives in AgentExecutor.run, so that's the in-scope
# per-run logger the event is added next to).
# ---------------------------------------------------------------------------


def test_log_path_event_emitted_at_end_of_run(tmp_path):
    from gc_core import agent_executor as agent_executor_module

    logger, logging_path = _make_logger(tmp_path)

    with tempfile.TemporaryDirectory() as thread_dir:
        class _SkillsRegistryStub:
            async def list_skills(self):
                return []

        ctx = SimpleNamespace(
            config_dict={
                "verbose": False,
                "model": "CodeChat",
                "max_steps": 0,
                "system_prompt_id": "v1",
                "use_server_subagents": False,
                "agent_md_path": None,
                "limit_project_metadata": True,
                "console_trace": False,
                "threads_data_path": thread_dir,
                "agent_url": "http://backend.local/v1/responses",
                "backend_api_mode": "responses",
                "use_ssl": True,
                "use_retry": False,
                "x_cl_id": 12345,
                "no_tool_call_max_count": 5,
            },
            auth_connector=None,
            logger=logger,
            logging_path=logging_path,
            tools_spec=[],
            tools_description=[],
            skills_registry=_SkillsRegistryStub(),
        )

        with patch.object(agent_executor_module, "arun_agent", return_value="ok"):
            executor = agent_executor_module.AgentExecutor(ctx)
            result = run_coro(executor.arun(["hello"], thread_id="thr1", system="cli", verbose=False))
            assert result == "ok"

    _flush(logger)
    recs = _records(logging_path)

    events = [r for r in recs if r["event"] == "log_path"]
    assert events, recs
    assert events[0]["path"] == logging_path
