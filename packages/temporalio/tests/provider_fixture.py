"""A real HTTP endpoint with a deterministic response callback for acceptance."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def scripted_provider(respond):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(data)
            message = respond(data)
            if data.get("stream"):
                self._stream(message)
                return
            body = json.dumps(
                {
                    "id": f"response-{len(requests)}",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls"
                            if message.get("tool_calls")
                            else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, message):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [
                    {"index": i, **value}
                    for i, value in enumerate(delta["tool_calls"])
                ]
            for choice, usage in (
                ({"index": 0, "delta": delta, "finish_reason": None}, None),
                (
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"},
                    {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                ),
            ):
                event = {
                    "id": f"response-{len(requests)}",
                    "choices": [choice],
                }
                if usage:
                    event["usage"] = usage
                self.wfile.write(
                    ("data: " + json.dumps(event) + "\n\n").encode()
                )
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/chat/completions",
            requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def calls(*values):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
            for call_id, name, arguments in values
        ],
    }


def finish(report="done"):
    return calls(
        (
            "finish",
            "turn_finish",
            {
                "solution_type": "full_solution",
                "task": "acceptance",
                "task_report": report,
                "handoff": "{}",
            },
        )
    )
