"""Focused emission tests for the jsonl event sites added in this task:

- src/cli.py: final_answer (wrapper-critical: must surface as a top-level jsonl
  "event" field, since the Task2PR action wrapper greps the jsonl stream for
  ``"event": "final_answer"`` to detect run success).
- src/agent_executor.py: run_usage (additive to the existing text
  ``<run_usage>...</run_usage>`` block; payload built via
  ``usage.report.build_run_usage_dict``).

Each test drives real code with a jsonl-mode per-run logger from
``create_run_logger`` and asserts the event line + expected keys appear as a
top-level "event" field (not embedded in "msg").
"""

import json
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# `import cli` (below, inside the final_answer test) parses sys.argv at
# import time (--query is required); pin a valid argv up front so this file
# runs deterministically under any pytest invocation.
sys.argv = ["gc-cli", "-q", "x"]

from gc_core.settings import settings_from_flat
from gc_core.utils.logging_module import create_run_logger
from gc_testkit import run_coro


def _make_logger(tmp_path, level="info"):
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


# ---------------------------------------------------------------------------
# src/usage/report.py: build_run_usage_dict (pure helper backing the
# run_usage event payload; ported from the fork additively, main's rendering
# logic (compute_cost/CostReport) is untouched).
# ---------------------------------------------------------------------------


def test_build_run_usage_dict_shape():
    from gc_core.usage.account import UsageAccount
    from gc_core.usage.report import build_run_usage_dict

    account = UsageAccount()
    account.add("main", input_tokens=100, output_tokens=50, cached_tokens=10)

    out = build_run_usage_dict(account, pricing=None, model_name="CodeChat")
    assert out is not None
    assert out["model"] == "CodeChat"
    assert out["categories"]["main"]["input"] == 100
    assert out["categories"]["main"]["output"] == 50
    assert out["categories"]["main"]["cached"] == 10
    assert out["total"]["input"] == 100
    assert out["total"]["output"] == 50
    # No pricing supplied → no cost breakdown.
    assert "cost" not in out


def test_build_run_usage_dict_empty_account_returns_none():
    from gc_core.usage.account import UsageAccount
    from gc_core.usage.report import build_run_usage_dict

    assert (
        build_run_usage_dict(
            UsageAccount(), pricing=None, model_name="CodeChat"
        )
        is None
    )


# ---------------------------------------------------------------------------
# src/agent_executor.py: run_usage event (reached at end of AgentExecutor.run,
# additive to the existing text <run_usage> block and the log_path event).
# ---------------------------------------------------------------------------


class _SkillsRegistryStub:
    async def list_skills(self):
        return []


def _base_ctx(logger, logging_path, thread_dir):
    return SimpleNamespace(
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
            "pricing": None,
        },
        auth_connector=None,
        logger=logger,
        logging_path=logging_path,
        tools_spec=[],
        tools_description=[],
        skills_registry=_SkillsRegistryStub(),
    )


def test_run_usage_event_emitted_at_end_of_run(tmp_path):
    from gc_core import agent_executor as agent_executor_module

    logger, logging_path = _make_logger(tmp_path)

    with tempfile.TemporaryDirectory() as thread_dir:
        ctx = _base_ctx(logger, logging_path, thread_dir)

        def _fake_run_agent(messages, state, ctx_arg, **kwargs):
            # Simulate the LLM call(s) that would normally accrue usage.
            state.usage.add(
                "main", input_tokens=123, output_tokens=45, cached_tokens=0
            )
            return "ok"

        with patch.object(
            agent_executor_module, "arun_agent", side_effect=_fake_run_agent
        ):
            executor = agent_executor_module.AgentExecutor(ctx)
            result = run_coro(
                executor.arun(
                    ["hello"], thread_id="thr2", system="cli", verbose=False
                )
            )
            assert result == "ok"

    _flush(logger)
    recs = _records(logging_path)

    events = [r for r in recs if r.get("event") == "run_usage"]
    assert events, recs
    usage = events[0]["usage"]
    assert usage["model"] == "CodeChat"
    assert usage["categories"]["main"]["input"] == 123
    assert usage["categories"]["main"]["output"] == 45
    assert usage["total"]["input"] == 123
    assert usage["total"]["output"] == 45

    # Additive: the existing text <run_usage> block and log_path event must
    # still be present.
    text_usage = [r for r in recs if "<run_usage>" in (r.get("msg") or "")]
    assert text_usage, recs
    log_path_events = [r for r in recs if r.get("event") == "log_path"]
    assert log_path_events, recs


def test_run_usage_event_not_emitted_when_no_usage(tmp_path):
    from gc_core import agent_executor as agent_executor_module

    logger, logging_path = _make_logger(tmp_path)

    with tempfile.TemporaryDirectory() as thread_dir:
        ctx = _base_ctx(logger, logging_path, thread_dir)

        with patch.object(
            agent_executor_module, "arun_agent", return_value="ok"
        ):
            executor = agent_executor_module.AgentExecutor(ctx)
            result = run_coro(
                executor.arun(
                    ["hello"], thread_id="thr3", system="cli", verbose=False
                )
            )
            assert result == "ok"

    _flush(logger)
    recs = _records(logging_path)

    events = [r for r in recs if r.get("event") == "run_usage"]
    assert not events, recs


# ---------------------------------------------------------------------------
# src/cli.py: final_answer (must be a top-level jsonl "event" field — the
# Task2PR wrapper greps for `"event": "final_answer"`).
# ---------------------------------------------------------------------------


def test_final_answer_event_is_top_level_event_field(tmp_path):
    from gc_cli import cli as cli_module

    logger, logging_path = _make_logger(tmp_path)
    ctx = SimpleNamespace(logger=logger)

    fake_config_dict = {"console_trace": True}

    with (
        patch.object(cli_module, "_ensure_project_gc_dir"),
        patch.object(cli_module, "_ensure_user_skills_dir"),
        patch.object(
            cli_module.arg_parser,
            "run_args",
            SimpleNamespace(
                init_config=False,
                init=False,
                config=None,
                write_guard=False,
                no_write_guard=False,
                command_moderation=None,
                read_guard=None,
                no_read_guard=False,
                read_guard_mode=None,
                query="hi",
                wrap_technical=False,
                skill=None,
                verbose=False,
                thread_id=None,
                new=False,
                auto_mcp=False,
            ),
        ),
        patch.object(
            cli_module,
            "load_settings",
            lambda *_a, **_k: settings_from_flat(fake_config_dict),
        ),
        patch.object(
            cli_module, "apply_cli_overrides", lambda loaded, **_kw: loaded
        ),
        patch.object(
            cli_module,
            "onboarding",
            SimpleNamespace(
                is_unconfigured=lambda cfg: False,
            ),
        ),
        patch.object(cli_module, "_resolve_forced_skill", return_value=None),
        patch.object(cli_module, "build_auth_connector", return_value=None),
        patch.object(
            cli_module, "acreate_run_context", new=AsyncMock(return_value=ctx)
        ),
        patch.object(cli_module, "AgentExecutor") as mock_executor_cls,
    ):
        # Точка входа CLI асинхронная (Задача 23 async-переезда): прогон идёт
        # через arun, и заглушка обязана быть корутинной.
        async def _arun(*_a, **_kw):
            return "the final answer text"

        mock_executor_cls.return_value.arun = _arun

        rc = cli_module.main()
        assert rc == 0

    _flush(logger)
    recs = _records(logging_path)

    events = [r for r in recs if r.get("event") == "final_answer"]
    assert events, recs
    assert events[0]["answer"]
    assert "the final answer text" in events[0]["answer"]
