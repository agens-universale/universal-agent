"""Actual root routing over local HTTP and sandboxed SDK history replay."""

import asyncio
import hashlib
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from live_support import call, temporal_cli, worker_kwargs
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from gc_core_temporalio.contracts import (
    ActivityCall,
    PreparedContext,
    RunRequest,
)
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime
from gc_core_temporalio.workflow import UniversalAgentWorkflow


@pytest.fixture
def root_http():
    requests, cards = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            route = self.path.strip("/")
            if route == "shared":
                route = (self.headers.get("X-Test-Route") or "").removeprefix(
                    "fake-header-"
                ) or self.headers.get("Authorization", "").replace(
                    "Bearer ", ""
                ).replace("fake-", "")
            request["route"] = route
            requests.append(request)
            card = None
            try:
                candidate = json.loads(request["messages"][-1]["content"])
                if (
                    isinstance(candidate, dict)
                    and "earlier_history_compacted" in candidate
                ):
                    card = candidate
            except (ValueError, TypeError, KeyError):
                pass
            if card is not None:
                cards.append(card)
                message = {
                    "role": "assistant",
                    "content": json.dumps(
                        {"score": 2, "reason": "scripted difficulty"}
                    ),
                }
            else:
                count = sum(m["role"] == "tool" for m in request["messages"])
                tool = (
                    call(
                        f"calc-{count}",
                        "exec_calculator",
                        {"expression": f"{count} + 1"},
                    )
                    if count < 2
                    else call(
                        "finish",
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": "test",
                            "task_report": "root done",
                            "handoff": "{}",
                        },
                    )
                )
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool],
                }
            body = json.dumps(
                {
                    "id": f"r-{len(requests)}",
                    "object": "chat.completion",
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls"
                            if "tool_calls" in message
                            else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 10,
                        "total_tokens": 50,
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
        yield f"http://127.0.0.1:{server.server_port}", requests, cards
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "version,difference",
    [
        ("new", "url"),
        ("new", "token"),
        ("new", "headers"),
        ("old_root", "url"),
        ("pre_v2", "url"),
        ("equivalent", "url"),
    ],
)
async def test_root_private_request_routing_and_recorded_replay(
    tmp_path, monkeypatch, root_http, version, difference
):
    binary = temporal_cli()
    monkeypatch.chdir(tmp_path)
    url, requests, cards = root_http
    models = {}
    for alias in ("fast", "mid", "big"):
        entry = {
            "model": "shared-model",
            "url": f"{url}/shared",
            "token": "fake-common",
        }
        if version != "equivalent":
            if difference == "url":
                entry["url"] = f"{url}/{alias}"
            elif difference == "token":
                entry["token"] = f"fake-{alias}"
            else:
                entry["backend"] = {
                    "chat_completion_headers": {
                        "X-Test-Route": f"fake-header-{alias}"
                    }
                }
        models[alias] = entry
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "root-routing",
            "resource_queue": "root-routing",
            "model_queue": "root-routing",
            "state_dir": str(tmp_path),
            "profiles": {
                "test": {
                    "workspace": str(tmp_path),
                    "core": {
                        "level": "auto",
                        "models": models,
                        "slots": {
                            "light": "fast",
                            "default": "mid",
                            "max": "big",
                        },
                        "backend": {
                            "api_mode": "chat_completions",
                            "stream": False,
                            "chat_completion_kwargs": {"stream": False},
                        },
                        "agent": {
                            "max_steps": 8,
                            "use_local_subagents": False,
                            "strict_turn_finish_validation": False,
                            "auto_routing_two_level": {
                                "strong_steps": 2,
                                "weak_steps": 10,
                            },
                        },
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                        "tui": {"text_answer_mode": "deny"},
                    },
                }
            },
        }
    )
    provider, native, threads = build_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            options = worker_kwargs(config, native, threads)
            if version == "old_root":
                options["patch_activation_callback"] = lambda item: (
                    item.patch_id != "root-routing-request-equivalence-v1"
                )
            elif version == "pre_v2":
                options["patch_activation_callback"] = lambda item: (
                    item.patch_id
                    not in {
                        "root-routing-request-equivalence-v1",
                        "routing-request-contract-v2",
                    }
                )
            async with Worker(env.client, **options):
                handle = await env.client.start_workflow(
                    UniversalAgentWorkflow.run,
                    RunRequest(
                        "test",
                        "files",
                        "test",
                        "thread",
                        ["Perform two calculations and finish"],
                        "root-routing",
                        "root-routing",
                    ),
                    id="root-routing",
                    task_queue="root-routing",
                )
                result = await asyncio.wait_for(handle.result(), 55)
                history = await handle.fetch_history()
            (
                tmp_path / f"root-history-{version}-{difference}.json"
            ).write_text(history.to_json())
            (tmp_path / "http-evidence.json").write_text(
                json.dumps(
                    {
                        "routes": [r["route"] for r in requests],
                        "pair_judges": len(cards),
                    }
                )
            )
            assert "root done" in str(result["answer"])
            scheduled = next(
                e
                for e in history.events
                if e.HasField("activity_task_scheduled_event_attributes")
                and e.activity_task_scheduled_event_attributes.activity_type.name
                == "universal_agent.context.prepare"
            )
            attrs = scheduled.activity_task_scheduled_event_attributes
            [prepare_call] = await env.client.data_converter.decode(
                attrs.input.payloads, [ActivityCall]
            )
            completion = next(
                e
                for e in history.events
                if e.HasField("activity_task_completed_event_attributes")
                and e.activity_task_completed_event_attributes.scheduled_event_id
                == scheduled.event_id
            )
            [prepared] = await env.client.data_converter.decode(
                completion.activity_task_completed_event_attributes.result.payloads,
                [PreparedContext],
            )
            new = version in ("new", "equivalent")
            if new:
                assert prepare_call.data["require_request_equivalence"] is True
                assert (
                    prepared.request_equivalence.context_id
                    == prepared.context.run_id
                )
                assert prepared.request_equivalence.groups == (
                    (0, 0, 0) if version == "equivalent" else (0, 1, 2)
                )
            else:
                assert prepare_call.data == {
                    "store_id": "files",
                    "settings_fingerprint": None,
                }
                assert prepared.request_equivalence is None
                # A real historical result has no optional field at all.
                payload = completion.activity_task_completed_event_attributes.result.payloads[
                    0
                ]
                body = json.loads(payload.data)
                body.pop("request_equivalence", None)
                payload.data = json.dumps(body).encode()
            if version in ("new", "pre_v2"):
                assert len(cards) == 1
                assert [r["route"] for r in requests] == [
                    "mid",
                    "mid",
                    "mid",
                    "fast",
                    "mid",
                ]
            else:
                assert cards == []
                assert len(requests) == 3
            recorded = json.dumps(asdict(prepared))
            for entry in models.values():
                headers = entry.get("backend", {}).get(
                    "chat_completion_headers", {}
                )
                for value in (
                    entry["url"],
                    entry["token"],
                    *headers.keys(),
                    *headers.values(),
                ):
                    assert value not in recorded
                    assert (
                        hashlib.sha256(value.encode()).hexdigest()
                        not in recorded
                    )
            before = len(requests)
            await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
                history
            )
            assert len(requests) == before
    finally:
        await provider.aclose()
