"""Треды формата develop открываются и продолжаются (спека хранилищ v2, 9.4).

На этапе 2 меняется только способ открытия (ThreadSession → Threads);
ожидания берутся из manifest.json фикстур и не меняются.
"""

import json

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import ThreadSession
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    parity_config,
)


def _cfg(tmp_path, monkeypatch):
    threads = install_legacy_threads(tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.chdir(workspace)
    cfg = parity_config(
        tmp_path / "conf",
        monkeypatch,
        **{
            "paths.threads_data_path": str(threads),
            "launch_root": str(workspace),
            "agent.strict_turn_finish_validation": False,
            "text_answer_mode": "deny",
            "intent_scope.tools": {},
        },
    )
    return cfg, threads


@pytest.mark.parametrize("name", sorted(LEGACY_THREADS))
async def test_legacy_thread_opens(name, tmp_path, monkeypatch):
    fixture = LEGACY_THREADS[name]
    cfg, _ = _cfg(tmp_path, monkeypatch)
    session = await ThreadSession.afrom_config(
        settings=cfg, system_id=fixture.system, thread_id=fixture.thread_id
    )
    assert len(session.history_messages) == fixture.messages
    texts = [
        m.get("content")
        for m in session.history_messages
        if m.get("role") == "user"
    ]
    assert fixture.first_user_text in texts


async def test_legacy_thread_continues_without_rewriting_history(
    tmp_path, monkeypatch
):
    fixture = LEGACY_THREADS["cli_two_turns"]
    cfg, threads = _cfg(tmp_path, monkeypatch)
    data = threads / f"data_{fixture.system}_{fixture.thread_id}.jsonl"
    before = data.read_bytes()
    finish = assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "full_solution",
            "task": "Продолжение",
            "task_report": "готово",
            "handoff": "{}",
        },
        "finish-new",
    )
    backend = install(monkeypatch, ScriptedBackend([finish]))
    ctx = await acreate_run_context(cfg)
    try:
        await AgentExecutor(ctx).arun(
            ["Продолжим старый тред"],
            thread_id=fixture.thread_id,
            system=fixture.system,
            start_new_thread=False,
            message_ids=["continue-0"],
        )
    finally:
        release_run_logger(ctx.logger)
    backend.assert_clean()
    after = data.read_bytes()
    assert after.startswith(before), "новый ход переписал старую историю"
    assert len(after.splitlines()) > len(before.splitlines())
    sent = json.dumps(backend.calls[0], ensure_ascii=False)
    assert fixture.first_user_text in sent, "старая история не ушла в модель"
