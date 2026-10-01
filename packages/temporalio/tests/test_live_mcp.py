"""SIGKILL the activity worker after MCP execution, then reuse the same session."""

import asyncio
import json
import shutil

from gc_ua_temporalio.client import TemporalAgentClient
from gc_ua_temporalio.contracts import SessionStart
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.workflow import AgentSessionWorkflow
from provider_fixture import calls, finish, scripted_provider
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_live_controls import Stand, until
from test_mcp_host import HostStand


def respond(data):
    outputs = [m for m in data["messages"] if m["role"] == "tool"]
    if len(outputs) >= 2:
        return finish()
    name = next(
        t["function"]["name"]
        for t in data["tools"]
        if t["function"]["name"].endswith("increment")
    )
    return calls((f"increment-{len(outputs)}", name, {}))


async def test_activity_worker_loss_preserves_mcp_session_and_receipt(
    tmp_path, monkeypatch
):
    with scripted_provider(respond) as (url, requests):
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=shutil.which("temporal")
        ) as env:
            workers = Stand(tmp_path, env, url)
            hosts = HostStand(
                tmp_path,
                env.client.service_client.config.target_host,
                "stream_stdio",
                monkeypatch,
            )
            config = hosts.config.model_dump()
            config["profiles"]["demo"]["completions_url"] = url
            workers.config.write_text(json.dumps(config))
            try:
                await hosts.start()
                process = workers.start("after_mcp")
                handle = await TemporalAgentClient(env.client).start(
                    SessionStart("mcp-restart", "Increment twice")
                )
                async with asyncio.timeout(40):
                    while not (tmp_path / "checkpoint").exists():
                        await asyncio.sleep(0.1)
                process.kill()
                process.wait()
                workers.start()
                await until(
                    handle, lambda s: s["phase"] == "idle" and s.get("turns")
                )
                await handle.signal("close_session")
                await handle.result()
                store = SQLiteThreadStore(config["database"])
                outputs = [
                    m["output"]
                    for m in store.load_messages("temporal", "mcp-restart")
                    if m.get("type") == "function_call_output"
                    and m["call_id"].startswith("increment")
                ]
                values = [
                    json.loads(json.loads(output)["content"][0]["text"])
                    for output in outputs
                ]
                assert [v["count"] for v in values] == [1, 2]
                assert values[0]["session"] == values[1]["session"]
                assert len(requests) == 3
                await Replayer(
                    workflows=[AgentSessionWorkflow]
                ).replay_workflow(await handle.fetch_history())
            finally:
                workers.close()
                hosts.close()
