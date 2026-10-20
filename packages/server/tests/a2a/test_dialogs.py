"""Диалог: один ход, корень, идемпотентность границ и потолок агентов."""

import asyncio
import os

from harness import AnswerAgent, SlowAgent, message, rpc, session
from gc_server.a2a.facade import MAX_LIVE_AGENTS


async def test_rejects_bad_parts_role_and_output_mode(tmp_path):
    async with session(tmp_path) as (client, _f, created):
        raw = await rpc(
            client,
            "SendMessage",
            message(parts=[{"raw": "x"}]),
        )
        html = await rpc(
            client,
            "SendMessage",
            message(
                message_id="m-html",
                parts=[{"text": "a", "mediaType": "text/html"}],
            ),
        )
        role = await rpc(
            client,
            "SendMessage",
            message(message_id="m-role", role="ROLE_AGENT"),
        )
        modes = await rpc(
            client,
            "SendMessage",
            {
                **message(message_id="m-mode"),
                "configuration": {"acceptedOutputModes": ["application/json"]},
            },
        )
        listed = await rpc(client, "ListTasks")
    assert raw.json()["error"]["code"] == -32005
    assert html.json()["error"]["code"] == -32005
    assert role.json()["error"]["code"] == -32602
    assert modes.json()["error"]["code"] == -32005
    assert listed.json()["result"]["totalSize"] == 0
    assert created == []


async def test_dialog_rules(tmp_path):
    async with session(tmp_path, agents=[SlowAgent()]) as (client, _f, _c):
        working = await rpc(
            client,
            "SendMessage",
            {
                **message(message_id="live"),
                "configuration": {"returnImmediately": True},
            },
        )
        task = working.json()["result"]["task"]
        second = await rpc(
            client,
            "SendMessage",
            message("ещё", message_id="queued", contextId=task["contextId"]),
        )
        unknown_ctx = await rpc(
            client,
            "SendMessage",
            message("x", message_id="ghost", contextId="missing-context"),
        )
        unknown_task = await rpc(
            client,
            "SendMessage",
            message("x", message_id="ghost-task", taskId="missing-task"),
        )
        await rpc(client, "CancelTask", {"id": task["id"]})
        terminal = await rpc(
            client,
            "SendMessage",
            message("снова", message_id="after", taskId=task["id"]),
        )
        nxt = await rpc(
            client,
            "SendMessage",
            message(
                "новый ход", message_id="next", contextId=task["contextId"]
            ),
        )
    assert second.json()["error"]["code"] == -32004
    assert unknown_ctx.json()["error"]["code"] == -32602
    assert unknown_task.json()["error"]["code"] == -32001
    assert terminal.json()["error"]["code"] == -32004
    assert nxt.json()["result"]["task"]["contextId"] == task["contextId"]
    assert nxt.json()["result"]["task"]["id"] != task["id"]


async def test_input_required_without_task_id_is_rejected(tmp_path):
    from harness import AskAgent

    async with session(tmp_path, agents=[AskAgent()]) as (client, _f, _c):
        asked = await rpc(client, "SendMessage", message())
        task = asked.json()["result"]["task"]
        bare = await rpc(
            client,
            "SendMessage",
            message("мимо", message_id="m-bare", contextId=task["contextId"]),
        )
        mismatch = await rpc(
            client,
            "SendMessage",
            message(
                "мимо",
                message_id="m-mis",
                taskId=task["id"],
                contextId="other-context",
            ),
        )
    assert bare.json()["error"]["code"] == -32602
    assert mismatch.json()["error"]["code"] == -32602


async def test_cwd_is_absolute_launch_root_or_rejected(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    async with session(tmp_path, **{"a2a.launch_root": str(root)}) as (
        client,
        _f,
        created,
    ):
        first = await rpc(client, "SendMessage", message())
        context = first.json()["result"]["task"]["contextId"]
        assert created[0].launch_root == os.path.normpath(str(root))
        relative = await rpc(
            client,
            "SendMessage",
            message(
                "опечатка",
                message_id="m-rel",
                metadata={"cwd": "work"},
            ),
        )
        foreign = await rpc(
            client,
            "SendMessage",
            message(
                "дальше",
                message_id="m-cwd",
                contextId=context,
                metadata={"cwd": str(other)},
            ),
        )
        explicit = await rpc(
            client,
            "SendMessage",
            message(
                "свой корень",
                message_id="m-new",
                metadata={"cwd": str(other)},
            ),
        )
    assert relative.json()["error"]["code"] == -32602
    assert foreign.json()["error"]["code"] == -32602
    assert explicit.status_code == 200
    assert created[1].launch_root == os.path.normpath(str(other))


async def test_idle_agents_evict_to_the_cap_and_resume(tmp_path):
    async with session(tmp_path) as (client, _f, created):
        contexts = []
        for index in range(MAX_LIVE_AGENTS + 1):
            response = await rpc(
                client, "SendMessage", message(message_id=f"m-{index}")
            )
            assert response.status_code == 200
            contexts.append(response.json()["result"]["task"]["contextId"])
        resumed = await rpc(
            client,
            "SendMessage",
            message("снова", message_id="m-back", contextId=contexts[0]),
        )
        assert created[0].closed is True
        assert created[MAX_LIVE_AGENTS].closed is False
        assert resumed.json()["result"]["task"]["contextId"] == contexts[0]
        assert created[-1].calls[0]["thread_id"] == contexts[0]
        assert created[-1].calls[0]["system"] == "a2a"


async def test_busy_agent_is_not_evicted_and_the_extra_dialog_starts(tmp_path):
    async with session(tmp_path, agents=[SlowAgent()]) as (
        client,
        _f,
        created,
    ):
        await rpc(
            client,
            "SendMessage",
            {
                **message(message_id="busy"),
                "configuration": {"returnImmediately": True},
            },
        )
        for index in range(MAX_LIVE_AGENTS):
            response = await rpc(
                client, "SendMessage", message(message_id=f"idle-{index}")
            )
            assert response.status_code == 200
            assert "error" not in response.json()
        assert created[0].closed is False
        assert created[1].closed is True
        assert len(created) == MAX_LIVE_AGENTS + 1


async def test_canceled_setup_does_not_free_the_next_task(
    tmp_path, monkeypatch
):
    """Отмена подготовки не снимает резерв задачи, которая уже заняла диалог."""
    monkeypatch.setattr("gc_server.a2a.facade.MAX_LIVE_AGENTS", 1)
    ready = asyncio.Event()
    release = asyncio.Event()
    first = True

    async with session(tmp_path) as (client, facade, created):

        async def slow_factory(agent_settings, **kwargs):
            nonlocal first
            del kwargs
            if first:
                first = False
                agent = SlowAgent()
                agent.launch_root = agent_settings.runtime.launch_root
                created.append(agent)
                ready.set()
                await release.wait()
                return agent
            agent = AnswerAgent("лишний")
            agent.launch_root = agent_settings.runtime.launch_root
            created.append(agent)
            return agent

        facade._agent_factory = slow_factory
        send = asyncio.create_task(
            rpc(
                client,
                "SendMessage",
                {
                    **message(message_id="m-1"),
                    "configuration": {"returnImmediately": True},
                },
            )
        )
        await ready.wait()
        listed = await rpc(client, "ListTasks", {})
        task = listed.json()["result"]["tasks"][0]
        canceled = await rpc(client, "CancelTask", {"id": task["id"]})
        assert canceled.json()["result"]["status"]["state"] == (
            "TASK_STATE_CANCELED"
        )
        second = asyncio.create_task(
            rpc(
                client,
                "SendMessage",
                {
                    **message(
                        "дальше",
                        message_id="m-2",
                        contextId=task["contextId"],
                    ),
                    "configuration": {"returnImmediately": True},
                },
            )
        )
        await asyncio.sleep(0.05)
        release.set()
        await send
        started = await second
        assert started.json()["result"]["task"]["status"]["state"] == (
            "TASK_STATE_WORKING"
        )
        for _ in range(50):
            if created[0].calls:
                break
            await asyncio.sleep(0.02)
        assert created[0].calls
        extra = await rpc(client, "SendMessage", message(message_id="m-3"))
        assert "error" not in extra.json()
        assert created[0].closed is False
