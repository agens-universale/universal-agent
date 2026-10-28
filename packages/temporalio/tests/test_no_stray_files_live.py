"""Ход под Temporal не оставляет файлов вне хранилища тредов и рабочей папки.

HOME, TMPDIR и каталог состояния gc подменены на временные. Хранилище —
SQLite: картинка ``fs_view_image`` ложится в него вложением треда
(``put_blob``), а не на диск воркера; лог прогона по умолчанию — не файлом (``save_logs: false``
под Temporal). Любой новый файл вне белого списка роняет тест — так новая
запись мимо хранилища видна сразу.
"""

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

from gc_core.config_modules import env_vars
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core_temporalio import AgentPlugin, AgentSession
from gc_stores.sqlite import SQLiteThreadStore
from live_support import call, temporal_cli, thread_messages
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_workflow_controls import finish, scripted_http  # noqa: F401


def _files(root: Path) -> set[Path]:
    return (
        {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()
    )


async def test_turn_leaves_no_files_outside_store_and_workspace(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    from PIL import Image

    home, tmp = tmp_path / "home", tmp_path / "tmp"
    ws, state = tmp_path / "workspace", tmp_path / "state"
    for path in (home, tmp, ws, state):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setattr(
        env_vars, "env", env_vars.EnvVars(gc_home=home / ".gc")
    )
    monkeypatch.chdir(ws)
    from gc_core.vision import service as vision_service
    from gc_core.vision.probe import ProbeResult, ProbeStatus

    async def _sees(*_args, **_kwargs):
        # Пробу зрения сценарная модель не пройдёт (код на картинке):
        # маршрут «видит» — снимок прикрепляется модели хода.
        return ProbeResult(ProbeStatus.CONFIRMED, "test")

    monkeypatch.setattr(vision_service, "aprobe_route", _sees)
    Image.new("RGB", (32, 32), (200, 10, 10)).save(ws / "pic.png")

    url, _requests, script = scripted_http
    # По вызову на ответ модели: батч из нескольких вызовов ядро режет.
    script.extend(
        [
            [
                call(
                    "w",
                    "fs_write_file",
                    {"path": str(ws / "out.txt"), "content": "x"},
                )
            ],
            [
                call(
                    "m",
                    "session_memory",
                    {
                        "action": "append",
                        "register": "knowledge",
                        "content": "факт",
                    },
                )
            ],
            [call("img", "fs_view_image", {"path": str(ws / "pic.png")})],
            [call("cmd", "exec_command", {"command": "echo hi"})],
            [finish("ok")],
        ]
    )
    settings = LoadedSettings(
        settings=resolve_paths(
            AppConfig.model_validate(
                {
                    "backend": {
                        "api_mode": "chat_completions",
                        "completions_url": url,
                        "chat_completion_kwargs": {"stream": False},
                        "vision": True,
                    },
                    "agent": {
                        "model": "scripted",
                        "max_steps": 10,
                        "use_local_subagents": False,
                        "strict_turn_finish_validation": False,
                    },
                    "security": {"command_moderation": {"enabled": False}},
                }
            ),
            str(ws),
        ),
        runtime=RuntimeEnv(launch_root=str(ws)),
    )
    store = SQLiteThreadStore(str(state / "threads.sqlite"))
    plugin = AgentPlugin({"a": settings}, thread_store=store, workspace=ws)
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        before = _files(home) | _files(tmp)
        async with Worker(env.client, task_queue="q", plugins=[plugin]):
            session = AgentSession(
                env.client, agent="a", thread_id="t1", task_queue="q"
            )
            await asyncio.wait_for(session.ask("сделай"), 90)
            await session.close()
        after = _files(home) | _files(tmp)

    stray = sorted(str(p) for p in after - before)
    assert stray == [], stray
    outputs = [
        m.get("output", "")
        for m in await thread_messages(store, "sdk", "t1")
        if m.get("type") == "function_call_output"
    ]
    assert not any("state_id" in str(o) for o in outputs), outputs
    with sqlite3.connect(state / "threads.sqlite") as db:
        names = [
            row[0]
            for row in db.execute(
                "SELECT name FROM gc_blobs "
                "WHERE system='sdk' AND thread_id='t1'"
            )
        ]
    assert any(name.endswith(".png") for name in names), (names, outputs)
    assert (ws / "out.txt").read_text() == "x"
    assert not os.path.exists(ws / "images")
