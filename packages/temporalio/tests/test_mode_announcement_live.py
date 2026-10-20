"""Объявление режима субагенту под Temporal: тот же код цикла, что локально.

Режим сессии — plan в мете треда (конфиг — edit): ребёнок получает общее
состояние копией на старте, и его первый запрос к модели несёт вход в plan
без тула плана; корень — вход в plan с тулом плана.
"""

import asyncio
import json

from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.plan_messages import (
    PLAN_ENTER_MESSAGE,
    PLAN_RESEARCH_ENTER_MESSAGE,
)
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core_temporalio import AgentPlugin, AgentSession
from gc_stores.sqlite import SQLiteThreadStore
from live_support import call, seed_meta, temporal_cli
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_workflow_controls import finish, scripted_http  # noqa: F401

_TASK = "child-task-marker"


def _settings(url, workspace):
    return LoadedSettings(
        settings=resolve_paths(
            AppConfig.model_validate(
                {
                    "backend": {
                        "api_mode": "chat_completions",
                        "completions_url": url,
                        "chat_completion_kwargs": {"stream": False},
                    },
                    "agent": {
                        "model": "scripted",
                        "max_steps": 10,
                        "use_local_subagents": True,
                        "strict_turn_finish_validation": False,
                    },
                    "security": {
                        "permissions": {"default_mode": "edit"},
                        "command_moderation": {"enabled": False},
                    },
                }
            ),
            str(workspace),
        ),
        runtime=RuntimeEnv(launch_root=str(workspace)),
    )


def _user_text(request_json):
    return "\n".join(
        str(m.get("content"))
        for m in request_json["messages"]
        if m.get("role") == "user"
    )


async def test_child_is_told_the_mode_of_the_session(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    url, requests, script = scripted_http
    script.extend(
        [
            [
                call(
                    "child",
                    "subagent_worker",
                    {"task": _TASK, "reason": "delegate"},
                )
            ],
            [finish("ребёнок")],
            [finish("родитель")],
        ]
    )
    store = SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
    # Режим сессии — в мете треда, не в конфиге.
    await seed_meta(
        store,
        "sdk",
        "t1",
        ThreadMetaMapper.merge_shared_into_meta(
            {}, SharedStateData(permission_mode="plan")
        ),
    )
    plugin = AgentPlugin(
        {"a": _settings(url, ws)}, thread_store=store, workspace=ws
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="q", plugins=[plugin]):
            session = AgentSession(
                env.client, agent="a", thread_id="t1", task_queue="q"
            )
            answer = await asyncio.wait_for(session.ask("сделай"), 60)
            await session.close()
    assert json.loads(answer)["task_report"] == "родитель"
    child = [r for r in requests if _TASK in _user_text(r)]
    assert child, "ребёнок не обращался к модели"
    assert PLAN_RESEARCH_ENTER_MESSAGE in _user_text(child[0])
    assert PLAN_ENTER_MESSAGE not in _user_text(child[0])
    assert PLAN_ENTER_MESSAGE in _user_text(requests[0])  # корень — с тулом
