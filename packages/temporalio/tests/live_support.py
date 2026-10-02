"""Shared scripted HTTP provider and helpers for native live tests."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.fixture
def provider(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(request)
            outputs = [
                m for m in request["messages"] if m.get("role") == "tool"
            ]
            if not outputs:
                calls = [
                    call(
                        "memory",
                        "session_memory",
                        {
                            "action": "append",
                            "register": "knowledge",
                            "content": "durable fact",
                        },
                    ),
                    call(
                        "todo",
                        "session_todo",
                        {
                            "todos": [
                                {
                                    "content": "verify recovery",
                                    "status": "completed",
                                }
                            ]
                        },
                    ),
                    call(
                        "read",
                        "fs_read_file",
                        {
                            "path": str(tmp_path / "example.txt")
                            if any(
                                str(tmp_path) in str(m.get("content", ""))
                                for m in request["messages"]
                                if m.get("role") == "user"
                            )
                            else "example.txt"
                        },
                    ),
                ]
            else:
                calls = [
                    call(
                        "finish",
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": "demo",
                            "task_report": "Recovered successfully",
                            "handoff": "{}",
                        },
                    )
                ]
            response = {
                "id": f"response-{2 if outputs else 1}",
                "object": "chat.completion",
                "model": "scripted",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": calls,
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
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield (
        f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        requests,
    )
    server.shutdown()
    server.server_close()
    thread.join()


async def wait_marker(path, process, handle):
    async with asyncio.timeout(60):
        while not path.exists():
            if process.poll() is not None:
                raise AssertionError("Worker exited; see worker log")
            if (await handle.describe()).status.name != "RUNNING":
                await handle.result()
                raise AssertionError("Workflow ended before checkpoint")
            await asyncio.sleep(0.2)
