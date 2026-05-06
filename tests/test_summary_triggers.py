import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    def _apply_tool(*_args, **_kwargs):
        return "ok"

    def _build_tools_data(_config_dict):
        return {}, [], []

    tools_pkg.apply_tool = _apply_tool
    tools_pkg.build_tools_data = _build_tools_data
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    root = Path(__file__).resolve().parents[1]
    tools_dir = root / "src" / "tools"
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg

    skills_pkg = types.ModuleType("tools.skills")
    skills_pkg.__path__ = []
    sys.modules["tools.skills"] = skills_pkg

    registry_mod = types.ModuleType("tools.skills.registry")
    registry_mod.get_skills_registry = lambda _cfg: _SkillsRegistryStub()
    sys.modules["tools.skills.registry"] = registry_mod


class _SkillsRegistryStub:
    def list_skills(self):
        return []


_stub_tools_modules()

import agent_executor as agent_executor_module  # noqa: E402


class SummaryTriggersTest(unittest.TestCase):
    def _make_ctx(self, tmp_dir: str, backend_api_mode: str) -> SimpleNamespace:
        prompt_path = os.path.join(tmp_dir, "chat_completions_system_prompt.md")
        with open(prompt_path, "w", encoding="utf-8") as fd:
            fd.write("SYSTEM PROMPT")
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
                "dump_storage": False,
                "threads_data_path": tmp_dir,
                "agent_url": "http://backend.local/v1/responses",
                "completions_url": "http://backend.local/v1/chat/completions",
                "backend_api_mode": backend_api_mode,
                "chat_completions_system_prompt_path": prompt_path,
                "use_ssl": True,
                "use_retry": False,
                "x_cl_id": 12345,
                "no_tool_call_max_count": 5,
                "summary_params": {
                    "chat_completions_summary_trigger_chars": 1,
                    "incremental_batch_chars": 2,
                    "max_accumulated_summary_chars": 20000,
                    "original_user_messages_max_chars": 20000,
                    "last_dialog_steps_count": 5,
                    "last_dialog_step_message_max_chars": 5000,
                },
            },
            auth_connector=None,
            logger=logging.getLogger("test_summary_triggers"),
            logging_path=os.path.join(tmp_dir, "test.log"),
            tools_spec=[],
            tools_description=[],
        )

    def test_chat_completions_calls_pre_run_and_pre_backend_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="chat_completions")
            call_order: list[str] = []

            def _fake_run_agent(messages, state, _ctx, **kwargs):
                call_order.append("run_agent")
                pre_backend_hook = kwargs.get("pre_backend_compaction_hook")
                if pre_backend_hook is not None:
                    pre_backend_hook(messages, "user_ask")
                return "ok"

            def _record_summary_call(**kwargs):
                reason = str(kwargs.get("trigger_reason"))
                call_order.append(f"summary:{reason}")

            with patch.object(
                agent_executor_module, "get_skills_registry", return_value=_SkillsRegistryStub()
            ), patch.object(
                agent_executor_module, "run_agent", side_effect=_fake_run_agent
            ), patch.object(
                agent_executor_module,
                "history_exceeds_summary_trigger",
                return_value=True,
            ), patch.object(
                agent_executor_module,
                "run_summary_compaction",
                side_effect=_record_summary_call,
            ):
                executor = agent_executor_module.AgentExecutor(ctx)
                result = executor.run("hello", thread_id="thr1", system="cli", verbose=False)
                self.assertEqual(result, "ok")

            self.assertEqual(call_order[0], "summary:pre_run")
            self.assertIn("run_agent", call_order)
            self.assertIn("summary:pre_backend", call_order)

    def test_chat_completions_in_memory_calls_pre_run_and_pre_backend_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="chat_completions")
            calls: list[str] = []

            def _fake_run_agent(messages, state, _ctx, **kwargs):
                pre_backend_hook = kwargs.get("pre_backend_compaction_hook")
                if pre_backend_hook is not None:
                    pre_backend_hook(messages, "user_ask")
                return "ok"

            def _record(**kwargs):
                calls.append(str(kwargs.get("trigger_reason")))

            with patch.object(
                agent_executor_module, "get_skills_registry", return_value=_SkillsRegistryStub()
            ), patch.object(
                agent_executor_module, "run_agent", side_effect=_fake_run_agent
            ), patch.object(
                agent_executor_module,
                "history_exceeds_summary_trigger",
                return_value=True,
            ), patch.object(
                agent_executor_module, "run_summary_compaction", side_effect=_record
            ):
                executor = agent_executor_module.AgentExecutor(ctx)
                result = executor.run("hello", system="cli", verbose=False)
                self.assertEqual(result, "ok")

            self.assertEqual(calls, ["pre_run", "pre_backend"])

    def test_responses_mode_does_not_call_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="responses")

            with patch.object(
                agent_executor_module, "get_skills_registry", return_value=_SkillsRegistryStub()
            ), patch.object(
                agent_executor_module, "run_agent", return_value="ok"
            ), patch.object(
                agent_executor_module, "run_summary_compaction"
            ) as compaction_mock:
                executor = agent_executor_module.AgentExecutor(ctx)
                result = executor.run("hello", thread_id="thr1", system="cli", verbose=False)
                self.assertEqual(result, "ok")

            compaction_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
