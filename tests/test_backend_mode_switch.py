import json
import logging
import os
import sys
import tempfile
import unittest
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _stub_tools_modules() -> None:
    # В проекте используется верхнеуровневый пакет `tools`, который может
    # конфликтовать с внешними библиотеками/плагинами. Поэтому подменяем
    # модули принудительно, чтобы тест был детерминированным.
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
    # Важно: оставляем `__path__`, чтобы другие тесты могли импортировать
    # подпакеты (например, `tools.simple_tools`).
    root = Path(__file__).resolve().parents[1]
    tools_dir = root / "src" / "tools"
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg

    skills_pkg = types.ModuleType("tools.skills")
    skills_pkg.__path__ = []
    sys.modules["tools.skills"] = skills_pkg

    registry_mod = types.ModuleType("tools.skills.registry")

    def _get_skills_registry(_config_dict):
        return _SkillsRegistryStub()

    registry_mod.get_skills_registry = _get_skills_registry
    sys.modules["tools.skills.registry"] = registry_mod


_stub_tools_modules()

import agent_executor as agent_executor_module  # noqa: E402
import processing_responses as processing_module  # noqa: E402
from thread_store import load_thread_meta, resolve_threads_dir  # noqa: E402


class _SkillsRegistryStub:
    def list_skills(self):
        return []


class BackendModeSwitchTest(unittest.TestCase):
    def test_switch_responses_completions_responses_same_thread(self):
        responses_calls = []
        completions_calls = []

        def _mock_responses_backend(data, headers, url, verify=True, use_retry=True, logger=None):
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
            data, headers, url, verify=True, use_retry=True, logger=None
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
                "x_cl_id": 12345,
                "no_tool_call_max_count": 5,
            }
            ctx = SimpleNamespace(
                config_dict=config_dict,
                auth_connector=None,
                logger=logging.getLogger("test_backend_mode_switch"),
                logging_path=os.path.join(tmp_dir, "test.log"),
                tools_spec=[],
                tools_description=[],
            )

            with patch.object(
                agent_executor_module, "get_skills_registry", return_value=_SkillsRegistryStub()
            ), patch.object(
                processing_module, "request_backend", side_effect=_mock_responses_backend
            ), patch.object(
                processing_module,
                "request_backend_chat_completions",
                side_effect=_mock_completions_backend,
            ):
                executor = agent_executor_module.AgentExecutor(ctx)
                thread_id = "switch_mode_1"

                result_step_1 = executor.run(
                    "step one",
                    thread_id=thread_id,
                    system="cli",
                    verbose=False,
                )
                self.assertIn("step limits", result_step_1)

                config_dict["backend_api_mode"] = "chat_completions"
                result_step_2 = executor.run(
                    "step two",
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
                    "step three",
                    thread_id=thread_id,
                    system="cli",
                    verbose=False,
                )
                self.assertIn("step limits", result_step_3)

                self.assertEqual(len(responses_calls), 2)
                self.assertEqual(len(completions_calls), 1)

                completions_payload = completions_calls[0]["data"]
                self.assertIn("messages", completions_payload)
                self.assertGreaterEqual(len(completions_payload["messages"]), 2)
                self.assertEqual(completions_payload["messages"][0].get("role"), "system")
                self.assertEqual(
                    completions_payload["messages"][0].get("content"),
                    "SYSTEM PROMPT FOR CHAT COMPLETIONS",
                )
                # На шаге chat/completions должна попасть история из предыдущего responses шага.
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
                    "chat_completions_system_prompt_rendered", meta
                )


if __name__ == "__main__":
    unittest.main()
