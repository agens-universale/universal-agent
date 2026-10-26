"""Сбой провайдера модели повторяет Temporal: ход ждёт и доходит до конца."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from temporalio.api.enums.v1 import EventType
from test_workflow_controls import finish, request, runtime


@pytest.fixture
def flaky_provider():
    """Первые два запроса — обрыв соединения без ответа (как ReadError у
    провайдера в приёмке), дальше — завершение хода."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            calls.append(1)
            if len(calls) <= 2:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            else:
                body = json.dumps(
                    {
                        "id": "r",
                        "object": "chat.completion",
                        "model": "scripted",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [finish("done")],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                ).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            calls,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_transient_provider_failure_is_retried_by_temporal(
    tmp_path, monkeypatch, flaky_provider
):
    monkeypatch.chdir(tmp_path)
    url, calls = flaky_provider
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(),
            id="controls",
            task_queue="native-controls",
        )
        result = await handle.result()
        history = await handle.fetch_history()
    assert result["turns"] == 1 and result["terminal_outcome"] is None
    assert len(calls) >= 3
    failed_attempts = [
        event
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
        and event.activity_task_started_event_attributes.attempt > 1
    ]
    assert failed_attempts  # повтор сделал Temporal, а не ядро
