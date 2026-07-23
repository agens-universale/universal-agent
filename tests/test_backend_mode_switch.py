import logging
import os
import sys
import tempfile
import importlib
import unittest
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _restore_tools_and_dependents() -> None:
    for name in list(sys.modules):
        if name in ("agent_executor", "run_summary_coordinator"):
            sys.modules.pop(name, None)
        elif name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)
    real_tools = importlib.import_module("tools")
    proc = sys.modules.get("processing_responses")
    if proc is not None:
        proc.apply_tool = real_tools.apply_tool
        proc.WrongFunctionArgumentsException = (
            real_tools.WrongFunctionArgumentsException
        )
    cycle_stop_mod = sys.modules.get("message_policy.cycle_stop")
    if cycle_stop_mod is not None:
        cycle_stop_mod.apply_tool = real_tools.apply_tool


def _stub_tools_modules() -> None:
    # Верхнеуровневый `tools` конфликтует с другими окружениями — минимальный stub
    # только на время этого теста (см. _restore_tools_and_dependents).
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    def _apply_tool(*_args, **_kwargs):
        raise AssertionError("apply_tool should not be called in this test")

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
    # Point at the real skills dir so genuine submodules (skills_policy) resolve;
    # tools.skills.registry is still stubbed below via sys.modules.
    skills_pkg.__path__ = [str(tools_dir / "skills")]
    sys.modules["tools.skills"] = skills_pkg

    registry_mod = types.ModuleType("tools.skills.registry")

    registry_mod.get_skills_registry = lambda _config_dict, **_kw: _SkillsRegistryStub()

    sys.modules["tools.skills.registry"] = registry_mod


class _SkillsRegistryStub:
    def list_skills(self):
        return []


class BackendModeSwitchTest(unittest.TestCase):
    def test_switch_responses_completions_responses_same_thread(self):
        _stub_tools_modules()
        try:
            import agent_executor as agent_executor_module
            import processing_responses as processing_module
            from thread_store import load_thread_meta, resolve_threads_dir
        except Exception:
            _restore_tools_and_dependents()
            raise

        responses_calls = []
        completions_calls = []

        def _mock_responses_backend(
            data, headers, url, verify=True, cert=None, use_retry=True, logger=None, timeout=None,
        ):
            responses_calls.append(
                {
                    "data": data,
                    "headers": headers,
                    "url": url,
                }
            )
            call_idx = len(responses_calls)
            return (
                {
                    "id": f"resp_{call_idx}",
                    "output": [
                        {
                            "id": f"msg_{call_idx}",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": f"resp step {call_idx}",
                                }
                            ],
                        }
                    ],
                },
                {},
            )

        def _mock_completions_backend(
            data, headers, url, verify=True, cert=None, use_retry=True, logger=None, timeout=None,
        ):
            completions_calls.append(
                {
                    "data": data,
                    "headers": headers,
                    "url": url,
                }
            )
            return {
                "id": "chat_1",
                "created": 1700000000,
                "object": "chat.completion",
                "model": "CodeChat",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "chat step",
                            "tool_calls": [],
                        },
                        "finish_reason": "stop",
                    }
                ],
            }

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                prompt_path = os.path.join(tmp_dir, "chat_completions_system_prompt.md")
                with open(prompt_path, "w", encoding="utf-8") as fd:
                    fd.write("SYSTEM PROMPT FOR CHAT COMPLETIONS")
                config_dict = {
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
                    "backend_api_mode": "responses",
                    "chat_completions_system_prompt_path": prompt_path,
                    "use_ssl": True,
                    "use_retry": False,
                    "backend_request_timeout_sec": 600,
                    "x_cl_id": 12345,
                    "no_tool_call_max_count": 5,
                    "summary_params": {
                        "chat_completions_summary_trigger_chars": 0,
                    },
                }
                ctx = SimpleNamespace(
                    config_dict=config_dict,
                    auth_connector=None,
                    logger=logging.getLogger("test_backend_mode_switch"),
                    logging_path=os.path.join(tmp_dir, "test.log"),
                    tools_spec=[],
                    tools_description=[],
                    project_gc_dir=Path(tmp_dir) / ".gc",
                    skills_registry=_SkillsRegistryStub(),
                )

                def _mock_chat_completions_policy(**kwargs):
                    payload = kwargs["payload_factory"](kwargs["initial_max_tokens"])
                    completions_calls.append(
                        {
                            "data": payload,
                            "headers": kwargs["headers"],
                            "url": kwargs["url"],
                        }
                    )
                    return _mock_completions_backend(
                        payload,
                        kwargs["headers"],
                        kwargs["url"],
                        verify=kwargs["verify"],
                        use_retry=kwargs["use_retry"],
                        logger=kwargs.get("logger"),
                    ), payload

                with patch.object(
                    processing_module, "request_backend", side_effect=_mock_responses_backend
                ), patch.object(
                    processing_module,
                    "request_chat_completions_with_policy",
                    side_effect=_mock_chat_completions_policy,
                ):
                    executor = agent_executor_module.AgentExecutor(ctx)
                    thread_id = "switch_mode_1"

                    result_step_1 = executor.run(
                        ["step one"],
                        thread_id=thread_id,
                        system="cli",
                        verbose=False,
                    )
                    self.assertIn("step limits", result_step_1)

                    config_dict["backend_api_mode"] = "chat_completions"
                    result_step_2 = executor.run(
                        ["step two"],
                        thread_id=thread_id,
                        system="cli",
                        verbose=False,
                    )
                    self.assertIn("step limits", result_step_2)

                    threads_dir = resolve_threads_dir(config_dict)
                    meta_after_chat = load_thread_meta(threads_dir, "cli", thread_id)
                    self.assertEqual(
                        meta_after_chat.get("backend_api_mode"), "chat_completions"
                    )
                    self.assertEqual(
                        meta_after_chat.get("chat_completions_system_prompt_rendered"),
                        "SYSTEM PROMPT FOR CHAT COMPLETIONS",
                    )

                    config_dict["backend_api_mode"] = "responses"
                    result_step_3 = executor.run(
                        ["step three"],
                        thread_id=thread_id,
                        system="cli",
                        verbose=False,
                    )
                    self.assertIn("step limits", result_step_3)

                    self.assertEqual(len(responses_calls), 2)
                    self.assertGreaterEqual(len(completions_calls), 1)

                    completions_payload = completions_calls[0]["data"]
                    self.assertIn("messages", completions_payload)
                    self.assertGreaterEqual(len(completions_payload["messages"]), 2)
                    self.assertEqual(completions_payload["messages"][0].get("role"), "system")
                    self.assertEqual(
                        completions_payload["messages"][0].get("content"),
                        "SYSTEM PROMPT FOR CHAT COMPLETIONS",
                    )
                    self.assertTrue(
                        any(
                            isinstance(m, dict)
                            and m.get("role") == "assistant"
                            and isinstance(m.get("content"), str)
                            and "resp step 1" in m.get("content", "")
                            for m in completions_payload["messages"]
                        )
                    )

                    threads_dir = resolve_threads_dir(config_dict)
                    meta = load_thread_meta(threads_dir, "cli", thread_id)
                    self.assertEqual(meta.get("backend_api_mode"), "responses")
                    self.assertEqual(meta.get("previous_response_id"), "resp_2")
                    self.assertEqual(meta.get("last_backend_response_id"), "resp_2")
                    self.assertNotIn(
                        "chat_completions_system_prompt_rendered",
                        meta,
                    )
        finally:
            _restore_tools_and_dependents()


if __name__ == "__main__":
    unittest.main()
