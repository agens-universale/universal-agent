"""Без человека (``human_input: false``, умолчание под Temporal): отказ охраны —
ответ тулу, а не сбой сессии, и субагент не ждёт ответа, которого не будет.

* запись вне периметра: ``ask`` охраны стал ``deny``, отказ уходит модели, ход
  продолжается (раньше отчёт о пути трогал бэкенд в коде workflow и ронял
  сессию);
* субагент пишет AGENTS.md: класс «инструкции» спрашивает человека всегда;
  ребёнку приходит отказ, и ход родителя завершается.
"""

import asyncio
import json

from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core_temporalio import AgentPlugin, AgentSession
from gc_stores.sqlite import SQLiteThreadStore
from live_support import call, temporal_cli
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_workflow_controls import finish, scripted_http  # noqa: F401


def _settings(url, workspace, *, subagents):
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
                        "max_steps": 6,
                        "use_local_subagents": subagents,
                        "strict_turn_finish_validation": False,
                    },
                    "security": {"command_moderation": {"enabled": False}},
                }
            ),
            str(workspace),
        ),
        runtime=RuntimeEnv(launch_root=str(workspace)),
    )


async def _turn(tmp_path, settings):
    plugin = AgentPlugin(
        {"a": settings},
        thread_store=SQLiteThreadStore(str(tmp_path / "threads.sqlite")),
        workspace=tmp_path / "ws",
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
    return answer


def _report(answer):
    try:
        return json.loads(answer)["task_report"]
    except (TypeError, ValueError, KeyError):
        return str(answer)


def _tool_outputs(requests):
    return [
        str(m.get("content"))
        for request in requests
        for m in request["messages"]
        if m["role"] == "tool"
    ]


async def test_denied_write_is_an_answer_not_a_session_failure(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    target = tmp_path / "outside" / "x.txt"
    url, requests, script = scripted_http
    script.extend(
        [
            [
                call(
                    "w", "fs_write_file", {"path": str(target), "content": "x"}
                )
            ],
            [finish("отказано")],
        ]
    )
    answer = await _turn(tmp_path, _settings(url, ws, subagents=False))
    assert _report(answer) == "отказано"
    assert not target.exists()
    assert any("denied" in out.lower() for out in _tool_outputs(requests))


async def test_child_asking_a_human_gets_a_refusal(
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
                    {"task": "write AGENTS.md", "reason": "delegate"},
                )
            ],
            [
                call(
                    "w",
                    "fs_write_file",
                    {"path": str(ws / "AGENTS.md"), "content": "rules"},
                )
            ],
            [finish("ребёнок")],
            [finish("родитель")],
        ]
    )
    answer = await _turn(tmp_path, _settings(url, ws, subagents=True))
    assert _report(answer) == "родитель"
    assert not (ws / "AGENTS.md").exists()
    child_output = json.dumps(_tool_outputs(requests), ensure_ascii=False)
    assert "AGENTS.md" in child_output


async def test_blocked_command_without_cwd_is_an_answer(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Команда без ``cwd``, которую охрана команд отклонила (защищённый путь —
    база хранилища тредов): отказ уходит модели, ход продолжается. Отчёт
    отказа берёт каталог команды у бэкенда — в коде workflow его нет."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    url, requests, script = scripted_http
    db = tmp_path / "threads.sqlite"
    script.extend(
        [
            [call("c", "exec_command", {"command": f"cat {db}"})],
            [finish("отказано")],
        ]
    )
    answer = await _turn(tmp_path, _settings(url, ws, subagents=False))
    assert _report(answer) == "отказано"
    assert any("protected" in out.lower() for out in _tool_outputs(requests))
