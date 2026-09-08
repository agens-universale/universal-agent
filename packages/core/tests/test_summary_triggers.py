import importlib
import logging
import os
import sys
import tempfile
import types
import unittest
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from gc_testkit.paths import module_dir
from gc_testkit import run_coro


def teardown_module(_module) -> None:
    # Полные имена: после переезда ключи в sys.modules — "gc_core.agent_executor"
    # и "gc_core.run_summary_coordinator". Кодмод сплита правит узлы import и
    # одиночные строковые литералы, а строки ВНУТРИ кортежа не тронул — сетка
    # module_literals их тоже не ловила (оговорено в её докстринге), и pop стал
    # no-op: модули, загруженные под подменённым gc_core.tools, доживали до конца
    # сессии и доставались следующим тестам.
    for mod in ("gc_core.agent_executor", "gc_core.run_summary_coordinator"):
        sys.modules.pop(mod, None)
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)
    real_tools = importlib.import_module("gc_core.tools")
    proc = sys.modules.get("gc_core.processing_responses")
    if proc is not None:
        proc.aapply_tool = real_tools.aapply_tool
        proc.WrongFunctionArgumentsException = (
            real_tools.WrongFunctionArgumentsException
        )
    cycle_stop_mod = sys.modules.get("gc_core.message_policy.cycle_stop")
    if cycle_stop_mod is not None:
        cycle_stop_mod.aapply_tool = real_tools.aapply_tool


agent_executor_module = None
run_summary_coordinator_module = None


def setup_module(_module) -> None:
    global agent_executor_module, run_summary_coordinator_module
    _stub_tools_modules()
    tools_pkg = sys.modules["gc_core.tools"]
    from gc_core import processing_responses as pr

    pr.aapply_tool = tools_pkg.aapply_tool
    pr.WrongFunctionArgumentsException = tools_pkg.WrongFunctionArgumentsException
    cycle_stop_mod = sys.modules.get("gc_core.message_policy.cycle_stop")
    if cycle_stop_mod is not None:
        cycle_stop_mod.aapply_tool = tools_pkg.aapply_tool

    for mod in ("gc_core.agent_executor", "gc_core.run_summary_coordinator"):
        sys.modules.pop(mod, None)
    from gc_core import agent_executor as ae
    from gc_core import run_summary_coordinator as rsc

    agent_executor_module = ae
    run_summary_coordinator_module = rsc


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("gc_core.tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    async def _apply_tool(*_args, **_kwargs):
        return "ok"

    def _build_tools_data(_config_dict):
        return {}, [], []

    tools_pkg.aapply_tool = _apply_tool
    tools_pkg.build_tools_data = _build_tools_data
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    tools_dir = module_dir("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg

    skills_pkg = types.ModuleType("gc_core.tools.skills")
    skills_pkg.__path__ = []
    sys.modules["gc_core.tools.skills"] = skills_pkg

    registry_mod = types.ModuleType("gc_core.tools.skills.registry")
    registry_mod.get_skills_registry = lambda _cfg, **_kw: _SkillsRegistryStub()
    sys.modules["gc_core.tools.skills.registry"] = registry_mod


class _SkillsRegistryStub:
    def list_skills(self):
        return []


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
                    "last_dialog_steps_count": 5,
                    "last_dialog_step_message_max_chars": 5000,
                },
            },
            auth_connector=None,
            logger=logging.getLogger("test_summary_triggers"),
            logging_path=os.path.join(tmp_dir, "test.log"),
            tools_spec=[],
            tools_description=[],
            project_gc_dir=Path(tmp_dir) / ".gc",
            skills_registry=_SkillsRegistryStub(),
        )

    def test_chat_completions_calls_pre_run_and_pre_backend_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="chat_completions")
            call_order: list[str] = []
            truth = deque([True, False, True])

            def _hist_exceeds(**_kwargs):
                return truth.popleft() if truth else False

            async def _fake_run_agent(messages, state, _ctx, **kwargs):
                call_order.append("run_agent")
                # Как в processing_responses.run_agent: сначала коммит батча в state.
                state.messages += list(messages)
                pre_backend_hook = kwargs.get("pre_backend_compaction_hook")
                if pre_backend_hook is not None:
                    # Хук корутинный (agent_executor передаёт a-двойник) — ждём.
                    await pre_backend_hook(messages, "user_ask")
                return "ok"

            def _record_summary_call(**kwargs):
                reason = str(kwargs.get("trigger_reason"))
                call_order.append(f"summary:{reason}")
                st = kwargs.get("state")
                if st is None:
                    return
                msgs = list(st.messages)
                sys_m = next(
                    (
                        m
                        for m in msgs
                        if str(m.get("role", "")).strip().lower() == "system"
                    ),
                    None,
                )
                st.messages = [sys_m] if sys_m else []

            with patch.object(
                agent_executor_module, "arun_agent", side_effect=_fake_run_agent
            ), patch.object(
                run_summary_coordinator_module,
                "history_exceeds_summary_trigger",
                side_effect=_hist_exceeds,
            ), patch.object(
                run_summary_coordinator_module,
                "arun_summary_compaction",
                side_effect=_record_summary_call,
            ):
                executor = agent_executor_module.AgentExecutor(ctx)
                result = run_coro(executor.arun(["hello"], thread_id="thr1", system="cli", verbose=False))
                self.assertEqual(result, "ok")

            self.assertEqual(call_order[0], "summary:pre_run")
            self.assertIn("run_agent", call_order)
            self.assertIn("summary:pre_backend", call_order)

    def test_chat_completions_in_memory_calls_pre_run_and_pre_backend_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="chat_completions")
            calls: list[str] = []
            truth = deque([True, False, True])

            def _hist_exceeds(**_kwargs):
                return truth.popleft() if truth else False

            async def _fake_run_agent(messages, state, _ctx, **kwargs):
                state.messages += list(messages)
                pre_backend_hook = kwargs.get("pre_backend_compaction_hook")
                if pre_backend_hook is not None:
                    # Хук корутинный (agent_executor передаёт a-двойник) — ждём.
                    await pre_backend_hook(messages, "user_ask")
                return "ok"

            def _record(**kwargs):
                calls.append(str(kwargs.get("trigger_reason")))
                st = kwargs.get("state")
                if st is None:
                    return
                msgs = list(st.messages)
                sys_m = next(
                    (
                        m
                        for m in msgs
                        if str(m.get("role", "")).strip().lower() == "system"
                    ),
                    None,
                )
                st.messages = [sys_m] if sys_m else []

            with patch.object(
                agent_executor_module, "arun_agent", side_effect=_fake_run_agent
            ), patch.object(
                run_summary_coordinator_module,
                "history_exceeds_summary_trigger",
                side_effect=_hist_exceeds,
            ), patch.object(
                run_summary_coordinator_module,
                "arun_summary_compaction",
                side_effect=_record,
            ):
                executor = agent_executor_module.AgentExecutor(ctx)
                result = run_coro(executor.arun(["hello"], system="cli", verbose=False))
                self.assertEqual(result, "ok")

            self.assertEqual(calls, ["pre_run", "pre_backend"])

    def test_responses_mode_does_not_call_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="responses")

            with patch.object(
                agent_executor_module, "arun_agent", return_value="ok"
            ), patch.object(
                run_summary_coordinator_module, "arun_summary_compaction"
            ) as compaction_mock:
                executor = agent_executor_module.AgentExecutor(ctx)
                result = run_coro(executor.arun(["hello"], thread_id="thr1", system="cli", verbose=False))
                self.assertEqual(result, "ok")

            compaction_mock.assert_not_called()

    def test_chat_completions_passes_pre_backend_hook_to_run_agent(self):
        """Wiring: chat/completions должен передавать pre_backend_compaction_hook в run_agent (spec Step 0)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="chat_completions")
            captured: dict = {}

            async def _fake_run_agent(messages, state, _ctx, **kwargs):
                captured.update(kwargs)
                return "ok"

            with patch.object(
                agent_executor_module, "arun_agent", side_effect=_fake_run_agent
            ), patch.object(
                run_summary_coordinator_module,
                "history_exceeds_summary_trigger",
                return_value=False,
            ):
                executor = agent_executor_module.AgentExecutor(ctx)
                result = run_coro(executor.arun(["hello"], system="cli", verbose=False))
                self.assertEqual(result, "ok")

            hook = captured.get("pre_backend_compaction_hook")
            self.assertTrue(callable(hook))
            turn_end_hook = captured.get("turn_end_compaction_hook")
            self.assertTrue(callable(turn_end_hook))

    def test_thread_history_not_readded_into_current_user_intent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._make_ctx(tmp_dir, backend_api_mode="chat_completions")
            thread_id = "thr_hist"
            data_path = os.path.join(tmp_dir, f"data_cli_{thread_id}.jsonl")
            meta_path = os.path.join(tmp_dir, f"meta_cli_{thread_id}.json")

            old_user_1 = {"role": "user", "content": "старый вопрос 1", "id": "m_old_1"}
            old_user_2 = {"role": "user", "content": "старый вопрос 2", "id": "m_old_2"}
            with open(data_path, "w", encoding="utf-8") as fd:
                for idx, payload in enumerate([old_user_1, old_user_2], start=1):
                    fd.write(
                        json.dumps(
                            {
                                "type": "message",
                                "thread_id": thread_id,
                                "system": "cli",
                                "seq": idx,
                                "ts": "2026-05-06T10:00:00+00:00",
                                "payload": payload,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            with open(meta_path, "w", encoding="utf-8") as fd:
                json.dump(
                    {
                        "thread_id": thread_id,
                        "system": "cli",
                        "schema_version": 1,
                        "created_at": "2026-05-06T10:00:00+00:00",
                        "updated_at": "2026-05-06T10:00:00+00:00",
                        "messages_count": 2,
                        "backend_api_mode": "chat_completions",
                        "previous_response_id": None,
                        "state_schema_version": "v2",
                        "user_intent_log": ["старый вопрос 1", "старый вопрос 2"],
                        "current_user_intent": [],
                    },
                    fd,
                    ensure_ascii=False,
                    indent=2,
                )

            async def _fake_run_agent(messages, _state, _ctx, **kwargs):
                persist_cb = kwargs.get("persist_messages_callback")
                if callable(persist_cb):
                    # Реплика реального run_agent: первый persist в chat_completions
                    # получает весь батч (история + новый user message).
                    persist_cb(messages)
                return "ok"

            with patch.object(
                agent_executor_module, "arun_agent", side_effect=_fake_run_agent
            ), patch.object(
                run_summary_coordinator_module,
                "history_exceeds_summary_trigger",
                return_value=False,
            ):
                executor = agent_executor_module.AgentExecutor(ctx)
                result = run_coro(executor.arun(
                    ["новый вопрос"],
                    thread_id=thread_id,
                    system="cli",
                    verbose=False,
                ))
                self.assertEqual(result, "ok")

            from gc_testkit.intent_fixtures import intent_user_ask_texts
            from gc_core.intent_log import deserialize_list

            with open(meta_path, "r", encoding="utf-8") as fd:
                saved_meta = json.load(fd)
            self.assertEqual(
                intent_user_ask_texts(
                    deserialize_list(saved_meta.get("current_user_intent"))
                ),
                ["новый вопрос"],
            )


if __name__ == "__main__":
    unittest.main()
