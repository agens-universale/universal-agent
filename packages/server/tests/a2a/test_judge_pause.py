"""Пауза судьи в канале A2A: спросить некого, и снимает её следующее
сообщение пира — каждое сообщение пира начинает новый прогон (спека §7.1)."""

import importlib
import types

from gc_server.a2a.surface import apply_a2a_runtime_defaults
from gc_core.execution.state import SharedStateData
from gc_testkit.session_meta import merge_shared_into_meta
from gc_core.security.confirm_port import confirm_available
from gc_core.thread_store import save_thread_meta
from gc_testkit.settings import make_settings


def _settings(tmp_path):
    return apply_a2a_runtime_defaults(
        make_settings(
            {
                "paths.threads_data_path": str(tmp_path),
                "backend.api_mode": "chat_completions",
                "backend.completions_url": "http://test",
                "agent.model": "x",
                "agent.max_steps": 1,
                "agent.system_prompt_id": "v1",
                "logging.save_logs": False,
            }
        )
    )


async def test_a_peer_message_lifts_the_judge_pause(monkeypatch, tmp_path):
    from gc_core.run_context import acreate_run_context

    save_thread_meta(
        str(tmp_path),
        "a2a",
        "t",
        merge_shared_into_meta(
            {},
            SharedStateData(judge_paused=True, judge_denials_in_a_row=3),
        ),
    )
    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    seen = {}

    async def _loop(messages, state, ctx, **kw):
        seen["paused"] = state.shared.judge_paused
        seen["in_a_row"] = state.shared.judge_denials_in_a_row
        return {"finished": True}

    monkeypatch.setattr(ae, "arun_agent", _loop)
    # Терминал у процесса есть, но канал A2A объявил «человека нет».
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: True)
    )
    ctx = await acreate_run_context(_settings(tmp_path), auth_connector=None)
    assert confirm_available(ctx) is False  # канал A2A: спросить некого
    await ae.AgentExecutor(ctx).arun(
        ["продолжай с другим подходом"], thread_id="t", system="a2a"
    )
    assert seen == {"paused": False, "in_a_row": 0}
