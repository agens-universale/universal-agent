"""Имена activities и workflow — данные совместимости, а не детали кода.

Переименование ломает histories живых сессий и подписи в Temporal UI. Меняйте
этот список только вместе с осознанным планом выкладки (Continue-As-New на
границе хода снимает старые histories, но ход в полёте доигрывает старый код).
"""

import asyncio

from gc_ua_temporalio.native_provider import NativeProfile, NativeWorkerConfig
from gc_ua_temporalio.native_worker import build_native_runtime, worker_options

ACTIVITIES = {
    "agent.context.prepare",
    "agent.context.close",
    "agent.context.release",
    "agent.state.prepare",
    "agent.prompts.prepare",
    "agent.prompt.read",
    "agent.skills.list",
    "agent.model.request",
    "agent.tool.execute",
    "agent.guard.probe",
    "agent.memory.execute",
    "agent.memory.hash",
    "agent.summary.trace",
    "agent.subagent.prepare",
    "agent.thread.load",
    "agent.thread.commit",
    "agent.thread.read_trace",
}
WORKFLOWS = {"NativeAgentWorkflow", "NativeSubagentWorkflow"}


def test_registered_names_are_pinned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = NativeWorkerConfig(
        state_dir=str(tmp_path),
        thread_stores={"files": {"path": str(tmp_path / "threads")}},
        profiles={"p": NativeProfile(workspace=str(tmp_path))},
    )
    provider, native, threads = build_native_runtime(config)
    try:
        activities, workflows = set(), set()
        for options in worker_options(config, native, threads):
            activities |= {
                a.__temporal_activity_definition.name
                for a in options["activities"]
            }
            workflows |= {
                w.__temporal_workflow_definition.name
                for w in options["workflows"]
            }
        assert activities == ACTIVITIES
        assert workflows == WORKFLOWS
    finally:
        asyncio.run(provider.aclose())
