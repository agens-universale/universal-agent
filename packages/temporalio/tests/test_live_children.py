"""The core loop in child workflows: recovery, explicit memory/usage and stop."""

import asyncio
import json
import shutil

import pytest
from gc_core.usage.account import UsageAccount
from gc_ua_temporalio.client import TemporalAgentClient
from gc_ua_temporalio.contracts import SessionStart
from gc_ua_temporalio.control_contracts import HumanReply
from gc_ua_temporalio.session_contracts import UserMessage
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.workflow import AgentSessionWorkflow
from provider_fixture import calls, finish, scripted_provider
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_live_controls import Stand, until


def child_response(data):
    names = {t["function"]["name"] for t in data["tools"]}
    outputs = [m for m in data["messages"] if m["role"] == "tool"]
    if any(
        m.get("content") == "extra child instruction" for m in data["messages"]
    ):
        return finish("child received extra instruction")
    if "subagent_worker" in names:
        if outputs:
            return finish("parent done")
        return calls(
            (
                "child",
                "subagent_worker",
                {"task": "remember and ask", "reason": "test"},
            )
        )
    if not outputs:
        return calls(
            (
                "remember",
                "session_memory",
                {
                    "action": "append",
                    "register": "knowledge",
                    "content": "child fact",
                },
            ),
            ("ask", "turn_ask_human", {"question": "Continue child?"}),
        )
    return finish("child done")


@pytest.mark.parametrize("stop", [False, True])
async def test_child_worker_restart_controls_and_explicit_delta(
    tmp_path, stop
):
    with scripted_provider(child_response) as (url, requests):
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=shutil.which("temporal")
        ) as env:
            stand = Stand(tmp_path, env, url)
            config = json.loads(stand.config.read_text())
            config["profiles"]["child"] = dict(config["profiles"]["demo"])
            config["profiles"]["demo"]["subagents"] = {
                "subagent_worker": "child"
            }
            stand.config.write_text(json.dumps(config))
            process = stand.start()
            client = TemporalAgentClient(env.client)
            try:
                parent = await client.start(
                    SessionStart("parent", "delegate", auto_close=True)
                )
                status = await until(parent, lambda s: bool(s["active_child"]))
                child = env.client.get_workflow_handle(status["active_child"])
                await until(child, lambda s: s["phase"] == "waiting_human")
                process.kill()
                process.wait()
                stand.start()
                status = await client.status(parent.id)
                assert status["child"]["phase"] == "waiting_human"
                assert len(requests) == 2
                await client.submit_message(
                    parent.id,
                    UserMessage("child-input", "extra child instruction"),
                )
                if stop:
                    await client.stop_turn(parent.id, status["turn_id"])
                else:
                    await client.answer_human(
                        parent.id,
                        HumanReply(
                            status["child"]["pending_human"]["request_id"],
                            "reply",
                            "continue",
                        ),
                    )
                result = await asyncio.wait_for(parent.result(), 40)
                assert result["memory"]["knowledge"].count("child fact") == 1
                assert result["disposition"] == (
                    "STOPPED" if stop else "PROCESSED"
                )
                assert (
                    UsageAccount.from_meta(result["usage"]).totals()["total"]
                    == len(requests) * 15
                )
                store = SQLiteThreadStore(config["database"])
                trace = store.load_meta("temporal", "parent")["child_runs"][0]
                assert trace["outcome"] == result["disposition"]
                assert trace["workflow_id"] == child.id
                for handle in (parent, child):
                    await Replayer(
                        workflows=[AgentSessionWorkflow]
                    ).replay_workflow(await handle.fetch_history())
            finally:
                stand.close()


async def test_unavailable_child_model_preserves_trace_and_parent_can_finish(
    tmp_path,
):
    with scripted_provider(child_response) as (url, requests):
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=shutil.which("temporal")
        ) as env:
            stand = Stand(tmp_path, env, url)
            config = json.loads(stand.config.read_text())
            config["profiles"]["child"] = {
                **config["profiles"]["demo"],
                "completions_url": "http://127.0.0.1:1",
            }
            config["profiles"]["demo"]["subagents"] = {
                "subagent_worker": "child"
            }
            stand.config.write_text(json.dumps(config))
            stand.start()
            try:
                parent = await TemporalAgentClient(env.client).start(
                    SessionStart("failed-child", "delegate", auto_close=True)
                )
                result = await asyncio.wait_for(parent.result(), 40)
                assert result["disposition"] == "PROCESSED"
                store = SQLiteThreadStore(config["database"])
                trace = store.load_meta("temporal", "failed-child")[
                    "child_runs"
                ][0]
                assert trace["outcome"] == "FAILED"
                assert store.load_messages("temporal", trace["thread_id"])
                assert (
                    UsageAccount.from_meta(result["usage"]).totals()["total"]
                    == len(requests) * 15
                )
                await Replayer(
                    workflows=[AgentSessionWorkflow]
                ).replay_workflow(await parent.fetch_history())
            finally:
                stand.close()
