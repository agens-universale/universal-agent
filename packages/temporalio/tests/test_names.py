"""Имена activities и workflow — данные совместимости, а не детали кода.

Переименование ломает histories живых сессий и подписи в Temporal UI. Меняйте
этот список только вместе с осознанным планом выкладки (Continue-As-New на
границе хода снимает старые histories, но ход в полёте доигрывает старый код).
"""

import asyncio

from gc_core_temporalio.provider import AgentProfile, WorkerConfig
from gc_core_temporalio.worker import build_runtime
from live_support import worker_kwargs

ACTIVITIES = {
    "universal_agent.context.prepare",
    "universal_agent.context.close",
    "universal_agent.context.release",
    "universal_agent.state.prepare",
    "universal_agent.prompts.prepare",
    "universal_agent.prompt.read",
    "universal_agent.skills.list",
    "universal_agent.model.request",
    "universal_agent.tool.execute",
    "universal_agent.guard.probe",
    "universal_agent.memory.execute",
    "universal_agent.memory.hash",
    "universal_agent.summary.trace",
    "universal_agent.subagent.prepare",
    "universal_agent.thread.load",
    "universal_agent.thread.commit",
    "universal_agent.thread.read_trace",
}
WORKFLOWS = {"UniversalAgentWorkflow", "UniversalSubagentWorkflow"}


def test_registered_names_are_pinned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = WorkerConfig(
        state_dir=str(tmp_path),
        thread_stores={"files": {"path": str(tmp_path / "threads")}},
        profiles={"p": AgentProfile(workspace=str(tmp_path))},
    )
    provider, native, threads = build_runtime(config)
    try:
        activities, workflows = set(), set()
        for options in [worker_kwargs(config, native, threads)]:
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
