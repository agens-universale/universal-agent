"""Судья действий и защиты под Temporal без человека (спека §16).

* судья отвечает 500: на каждый проверяемый вызов — ровно две попытки
  (шаг судьи и его один повтор; ни повторов клиента, ни повторов activity),
  ход идёт дальше, счётчик сбоев растёт — на `FAILURES_BEFORE_STOP`-м
  подряд прогон остановлен;
* стоп во время запроса к судье: вызов получает тело `cancelled_by_user`;
* запись в скилл и в `.envrc` без человека — отказ;
* manual: редирект на другой хост на воркере не выполняется —
  `redirect_to_other_host`.
"""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.permission_denials import (
    OTHER_HOST_REDIRECT_TYPE,
    judge_failures_stop_text,
)
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.security.action_judge import load_action_judge_prompt
from gc_core.security.action_judge_step import FAILURES_BEFORE_STOP
from gc_core.security.url_policy import url_host_key
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core_temporalio import AgentPlugin, AgentSession
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from live_support import call, temporal_cli
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_workflow_controls import finish

_JUDGE_SYSTEM = load_action_judge_prompt("system")


def _is_judge(body):
    messages = body.get("messages") or []
    return bool(messages) and messages[0].get("content") == _JUDGE_SYSTEM


def _tool_reply(tool_calls, n):
    return {
        "id": f"response-{n}",
        "object": "chat.completion",
        "model": "scripted",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 10,
            "total_tokens": 50,
        },
    }


@pytest.fixture
def judged_http():
    """Модель — по сценарию `script`; запрос судьи (его системное сообщение)
    — 500, при `judge.hang` — сначала ждёт `judge.release`. GET `/start`
    отвечает 302 на тот же сервер под другим именем хоста (`localhost`)."""
    env = SimpleNamespace(
        main=[],
        judged=[],
        script=[],
        visited=[],
        hang=False,
        seen=threading.Event(),
        release=threading.Event(),
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status, payload, headers=()):
            body = json.dumps(payload).encode()
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            if _is_judge(body):
                env.judged.append(body)
                env.seen.set()
                if env.hang:
                    env.release.wait(60)
                self._send(500, {"error": {"message": "judge is down"}})
                return
            env.main.append(body)
            calls = env.script.pop(0) if env.script else [finish()]
            self._send(200, _tool_reply(calls, len(env.main)))

        def do_GET(self):
            env.visited.append(self.path)
            if self.path == "/start":
                target = f"http://localhost:{env.port}/page"
                self._send(302, {}, headers=[("Location", target)])
                return
            self._send(200, {"page": "other host"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    env.port = server.server_port
    env.url = f"http://127.0.0.1:{env.port}/v1/chat/completions"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield env
    finally:
        env.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def _settings(url, workspace, *, mode="edit"):
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
                        "max_steps": 30,
                        "use_local_subagents": False,
                        "strict_turn_finish_validation": False,
                    },
                    "security": {
                        "permissions": {"default_mode": mode},
                        "command_moderation": {"enabled": False},
                        "url_policy": {"enabled": False},
                    },
                }
            ),
            str(workspace),
        ),
        runtime=RuntimeEnv(launch_root=str(workspace)),
    )


def _workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    return ws


@asynccontextmanager
async def _session(tmp_path, settings, *, shared=None):
    """Сессия без человека (`human_input` у плагина не включён). `shared` —
    общее состояние в мете треда до первого хода."""
    store = SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
    if shared is not None:
        store.save_meta(
            "sdk",
            "t1",
            ThreadMetaMapper.merge_shared_into_meta({}, shared),
        )
    plugin = AgentPlugin(
        {"a": settings}, thread_store=store, workspace=tmp_path / "ws"
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="q", plugins=[plugin]):
            session = AgentSession(
                env.client, agent="a", thread_id="t1", task_queue="q"
            )
            try:
                yield session, store
            finally:
                await session.close()


def _tool_outputs(requests):
    return [
        str(m.get("content"))
        for request in requests
        for m in request["messages"]
        if m["role"] == "tool"
    ]


def _report(answer):
    try:
        return json.loads(answer)["task_report"]
    except (TypeError, ValueError, KeyError):
        return str(answer)


async def test_judge_failure_is_one_attempt_and_the_turn_goes_on(
    tmp_path, monkeypatch, judged_http
):
    ws = _workspace(tmp_path, monkeypatch)
    http = judged_http
    http.script.extend(
        [
            [call(f"c{i}", "exec_command", {"command": f"printf {i}"})]
            for i in range(FAILURES_BEFORE_STOP)
        ]
    )
    async with _session(tmp_path, _settings(http.url, ws, mode="auto")) as (
        session,
        _store,
    ):
        answer = await asyncio.wait_for(session.ask("сделай"), 120)
    # Каждый проверяемый вызов — ровно две попытки судьи (шаг и его один
    # повтор): повтор клиента или activity дал бы больше, и «не больше 20
    # запросов до остановки» не выполнялось бы.
    assert len(http.judged) == 2 * FAILURES_BEFORE_STOP
    # После каждого сбоя ход шёл дальше: модель получала ответ тула.
    assert len(http.main) == FAILURES_BEFORE_STOP
    # Счётчик рос с каждым сбоем: на пороге прогон остановлен.
    assert judge_failures_stop_text(FAILURES_BEFORE_STOP) in str(answer)


async def test_stop_during_the_judge_request_is_a_stop_body(
    tmp_path, monkeypatch, judged_http
):
    ws = _workspace(tmp_path, monkeypatch)
    http = judged_http
    http.hang = True
    # Команда, а не запись вне проекта: запись вне проекта режим закрывает
    # сам (`write_denied_by_mode`), до судьи она не доходит.
    target = ws / "x.txt"
    http.script.append(
        [call("w", "exec_command", {"command": f"touch {target}"})]
    )
    async with _session(tmp_path, _settings(http.url, ws, mode="auto")) as (
        session,
        store,
    ):
        message_id = await session.send("запиши файл")
        assert await asyncio.to_thread(http.seen.wait, 60)
        await session.stop()
        await asyncio.wait_for(session.wait(message_id, timeout=60), 90)
        http.release.set()
        saved = json.dumps(
            store.load_messages("sdk", "t1"), ensure_ascii=False
        )
    assert not target.exists()
    assert len(http.judged) == 1  # стоп — не сбой: повтора нет
    assert "cancelled_by_user" in saved


async def test_writes_into_a_skill_and_shell_settings_are_refused(
    tmp_path, monkeypatch, judged_http
):
    ws = _workspace(tmp_path, monkeypatch)
    http = judged_http
    skill = ws / ".gc" / "skills" / "x" / "SKILL.md"
    envrc = ws / ".envrc"
    http.script.extend(
        [
            [call("s", "fs_write_file", {"path": str(skill), "content": "x"})],
            [call("e", "fs_write_file", {"path": str(envrc), "content": "x"})],
            [finish("отказано")],
        ]
    )
    async with _session(tmp_path, _settings(http.url, ws)) as (session, _):
        answer = await asyncio.wait_for(session.ask("сделай"), 120)
    assert _report(answer) == "отказано"
    assert not skill.exists() and not envrc.exists()
    # Ответы тулов копятся в каждом следующем запросе — смотрим последний.
    outputs = _tool_outputs(http.main[-1:])
    assert sum("protected_path_denied" in out for out in outputs) == 2


async def test_manual_redirect_to_another_host_is_not_followed_on_the_worker(
    tmp_path, monkeypatch, judged_http
):
    """Режим manual — в мете треда (конфиг — edit), сайт разрешён «в
    сессии»: тело тула исполняет воркер и по редиректу на другой хост не
    идёт (C8 переносит режим сессии на воркер)."""
    ws = _workspace(tmp_path, monkeypatch)
    http = judged_http
    start = f"http://127.0.0.1:{http.port}/start"
    http.script.extend(
        [
            [call("f", "exec_fetch_url", {"url": start})],
            [finish("готово")],
        ]
    )
    shared = SharedStateData(
        permission_mode="manual",
        confirm_rules={url_host_key(start): True},
    )
    async with _session(tmp_path, _settings(http.url, ws), shared=shared) as (
        session,
        _,
    ):
        answer = await asyncio.wait_for(session.ask("открой"), 120)
    assert _report(answer) == "готово"
    assert http.visited == ["/start"]
    outputs = _tool_outputs(http.main)
    assert any(OTHER_HOST_REDIRECT_TYPE in out for out in outputs)
