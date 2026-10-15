"""Ход: ответ, вопрос, отмена, список и чтение задачи."""

import asyncio

from harness import (
    AnswerAgent,
    AskAgent,
    Clock,
    ConfirmAgent,
    FailAgent,
    HungStreamAgent,
    SlowAgent,
    message,
    rpc,
    session,
)


async def test_send_returns_completed_task_with_artifact(tmp_path):
    async with session(tmp_path) as (client, _f, created):
        response = await rpc(
            client,
            "SendMessage",
            message(),
            extra_headers={"A2A-Extensions": "something"},
        )
    body = response.json()
    assert response.status_code == 200
    assert "error" not in body
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["timestamp"].endswith("Z")
    assert task["artifacts"][0]["parts"] == [
        {"text": "В src 42 файла.", "mediaType": "text/plain"}
    ]
    assert task["history"] == [
        {
            "messageId": "m-1",
            "role": "ROLE_USER",
            "parts": [{"text": "Сколько файлов в src?"}],
        }
    ]
    assert "В src 42 файла." not in str(task["history"])
    call = created[0].calls[0]
    assert call["system"] == "a2a"
    assert call["thread_id"] == task["contextId"]
    assert call["prompt"] == "Сколько файлов в src?"


async def test_same_message_id_does_not_start_a_second_turn(tmp_path):
    async with session(tmp_path) as (client, _f, created):
        first = await rpc(client, "SendMessage", message())
        second = await rpc(client, "SendMessage", message(text="другое"))
    assert (
        first.json()["result"]["task"]["id"]
        == (second.json()["result"]["task"]["id"])
    )
    assert len(created[0].calls) == 1
    assert second.json()["result"]["task"]["history"][0]["parts"] == [
        {"text": "Сколько файлов в src?"}
    ]


async def test_duplicate_message_id_rejects_a_different_request(tmp_path):
    """Повтор messageId с другим taskId или contextId — не чужая задача."""
    async with session(tmp_path, agents=[AskAgent()]) as (
        client,
        _f,
        created,
    ):
        asked = await rpc(client, "SendMessage", message())
        task = asked.json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        reply = await rpc(
            client,
            "SendMessage",
            message(
                "Только *.py",
                message_id="m-1",
                taskId=task["id"],
                contextId=task["contextId"],
            ),
        )
        other = await rpc(
            client, "SendMessage", message("второй", message_id="m-other")
        )
        foreign = other.json()["result"]["task"]
        stolen = await rpc(
            client,
            "SendMessage",
            message(
                "чужое",
                message_id="m-1",
                contextId=foreign["contextId"],
            ),
        )
        replay = await rpc(client, "SendMessage", message())
        waiting = await rpc(client, "GetTask", {"id": task["id"]})
    assert reply.json()["error"]["code"] == -32602
    assert stolen.json()["error"]["code"] == -32602
    assert "result" not in stolen.json()
    assert replay.json()["result"]["task"]["id"] == task["id"]
    assert waiting.json()["result"]["status"]["state"] == (
        "TASK_STATE_INPUT_REQUIRED"
    )
    assert len(created[0].calls) == 1


async def test_question_then_reply_continues_the_same_task(tmp_path):
    async with session(tmp_path, agents=[AskAgent()]) as (client, _f, created):
        asked = await rpc(client, "SendMessage", message())
        task = asked.json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert task["status"]["message"]["role"] == "ROLE_AGENT"
        assert task["status"]["message"]["parts"][0]["text"] == "Какие файлы?"
        assert [item["role"] for item in task["history"]] == [
            "ROLE_USER",
            "ROLE_AGENT",
        ]
        replied = await rpc(
            client,
            "SendMessage",
            message(
                "Только *.py",
                message_id="m-2",
                taskId=task["id"],
                contextId=task["contextId"],
            ),
        )
    done = replied.json()["result"]["task"]
    assert done["id"] == task["id"]
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert done["artifacts"][0]["parts"][0]["text"] == "учтено: Только *.py"
    roles = [item["role"] for item in done["history"]]
    assert roles == ["ROLE_USER", "ROLE_AGENT", "ROLE_USER"]
    assert len(created[0].calls) == 1


async def test_confirm_does_not_read_the_server_terminal(
    tmp_path, monkeypatch
):
    """tty на stdin сервера не становится вопросом write/command guard."""

    class Tty:
        def isatty(self) -> bool:
            return True

    def terminal(*_args, **_kwargs):
        raise AssertionError("stdin")

    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setattr(
        "gc_core.security.path_policy.prompt_user_confirmation_choice",
        terminal,
    )
    async with session(tmp_path, agents=[ConfirmAgent()]) as (
        client,
        _f,
        created,
    ):
        response = await rpc(client, "SendMessage", message())
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert created[0].allowed is False
    assert task["artifacts"][0]["parts"][0]["text"] == "нет"


async def test_failed_turn_is_a_task_not_an_rpc_error(tmp_path):
    async with session(tmp_path, agents=[FailAgent()]) as (client, _f, _c):
        response = await rpc(client, "SendMessage", message())
    body = response.json()
    assert response.status_code == 200
    assert "error" not in body
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"][0]["text"] == "Ход не выполнен."


async def test_unrecorded_failure_is_internal_error(tmp_path):
    async with session(tmp_path) as (client, facade, _c):

        async def broken(*_args, **_kwargs):
            raise RuntimeError("disk")

        facade._store.mark_completed = broken
        facade._store.mark_failed = broken
        response = await rpc(client, "SendMessage", message())
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32603
    assert response.json()["error"]["data"]["reason"] == "InternalError"


async def test_cancel_during_agent_create_does_not_start_turn(tmp_path):
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = AnswerAgent()

    async with session(tmp_path) as (client, facade, created):

        async def slow_factory(agent_settings, **kwargs):
            del kwargs
            agent.launch_root = agent_settings.runtime.launch_root
            created.append(agent)
            ready.set()
            await release.wait()
            return agent

        facade._agent_factory = slow_factory
        send = asyncio.create_task(
            rpc(
                client,
                "SendMessage",
                {
                    **message(),
                    "configuration": {"returnImmediately": True},
                },
            )
        )
        await ready.wait()
        listed = await rpc(client, "ListTasks", {})
        task_id = listed.json()["result"]["tasks"][0]["id"]
        canceled = await rpc(client, "CancelTask", {"id": task_id})
        assert canceled.json()["result"]["status"]["state"] == (
            "TASK_STATE_CANCELED"
        )
        release.set()
        sent = await send
        assert sent.json()["result"]["task"]["status"]["state"] == (
            "TASK_STATE_CANCELED"
        )
        got = await rpc(client, "GetTask", {"id": task_id})
        assert got.json()["result"]["status"]["state"] == (
            "TASK_STATE_CANCELED"
        )
    assert agent.calls == []


async def test_request_cancel_during_agent_create_seals_task(tmp_path):
    ready = asyncio.Event()
    first = True

    async with session(tmp_path) as (client, facade, created):

        async def slow_factory(agent_settings, **kwargs):
            nonlocal first
            del kwargs
            agent = AnswerAgent("после")
            agent.launch_root = agent_settings.runtime.launch_root
            created.append(agent)
            if first:
                first = False
                ready.set()
                await asyncio.Event().wait()
            return agent

        facade._agent_factory = slow_factory
        send = asyncio.create_task(
            rpc(client, "SendMessage", message(message_id="m-1"))
        )
        await ready.wait()
        listed = await rpc(client, "ListTasks", {})
        task = listed.json()["result"]["tasks"][0]
        task_id = task["id"]
        context_id = task["contextId"]
        send.cancel()
        try:
            await send
        except asyncio.CancelledError:
            pass
        again = await rpc(client, "SendMessage", message(message_id="m-1"))
        assert again.json()["result"]["task"]["id"] == task_id
        assert again.json()["result"]["task"]["status"]["state"] == (
            "TASK_STATE_CANCELED"
        )
        nxt = await rpc(
            client,
            "SendMessage",
            message("дальше", message_id="m-2", contextId=context_id),
        )
        assert "error" not in nxt.json()
        assert nxt.json()["result"]["task"]["status"]["state"] == (
            "TASK_STATE_COMPLETED"
        )
        assert nxt.json()["result"]["task"]["artifacts"][0]["parts"][0][
            "text"
        ] == ("после")


async def test_cancel_waits_until_the_run_finishes(tmp_path):
    """Отмена не снимает диалог, пока прогон после stop() ещё идёт."""
    async with session(tmp_path, agents=[HungStreamAgent()]) as (
        client,
        _f,
        created,
    ):
        first = await rpc(
            client,
            "SendMessage",
            {
                **message(message_id="m-1"),
                "configuration": {"returnImmediately": True},
            },
        )
        hung = created[0]
        await hung.entered.wait()
        task = first.json()["result"]["task"]
        cancel = asyncio.create_task(
            rpc(client, "CancelTask", {"id": task["id"]})
        )
        await asyncio.sleep(0.05)
        assert cancel.done() is False
        assert hung.active == 1
        overlap = await rpc(
            client,
            "SendMessage",
            {
                **message(
                    "ещё", message_id="m-2", contextId=task["contextId"]
                ),
                "configuration": {"returnImmediately": True},
            },
        )
        assert overlap.json()["error"]["code"] == -32004
        assert hung.peak == 1
        hung.release.set()
        body = await cancel
        assert body.json()["result"]["status"]["state"] == (
            "TASK_STATE_CANCELED"
        )
        assert hung.active == 0
        nxt = await rpc(
            client,
            "SendMessage",
            message("дальше", message_id="m-3", contextId=task["contextId"]),
        )
    assert nxt.json()["result"]["task"]["status"]["state"] == (
        "TASK_STATE_COMPLETED"
    )
    assert hung.peak == 1


async def test_server_exit_stops_a_blocking_turn(tmp_path):
    """should_exit останавливает ход, не дожидаясь конца SendMessage."""
    from gc_a2a.main import stop_turns_on_exit

    async with session(tmp_path, agents=[SlowAgent()]) as (
        client,
        facade,
        created,
    ):
        pending = asyncio.create_task(rpc(client, "SendMessage", message()))
        for _ in range(50):
            if created and created[0].calls:
                break
            await asyncio.sleep(0.02)
        assert created[0].calls
        server = type("Server", (), {"should_exit": False})()
        watcher = asyncio.create_task(stop_turns_on_exit(server, facade))
        await asyncio.sleep(0.05)
        assert pending.done() is False
        server.should_exit = True
        response = await asyncio.wait_for(pending, timeout=2)
        await watcher
    assert response.json()["result"]["task"]["status"]["state"] == (
        "TASK_STATE_CANCELED"
    )
    assert created[0].stopped is True


async def test_get_and_list_and_cancel(tmp_path):
    clock = Clock()
    async with session(
        tmp_path, agents=[SlowAgent(), AnswerAgent("второй")], clock=clock
    ) as (client, _f, created):
        working = await rpc(
            client,
            "SendMessage",
            {
                **message(message_id="slow"),
                "configuration": {"returnImmediately": True},
            },
        )
        slow_id = working.json()["result"]["task"]["id"]
        assert working.json()["result"]["task"]["status"]["state"] == (
            "TASK_STATE_WORKING"
        )
        done = await rpc(
            client, "SendMessage", message("ещё", message_id="fast")
        )
        fast = done.json()["result"]["task"]
        listed = await rpc(
            client, "ListTasks", {"pageSize": 1, "includeArtifacts": False}
        )
        page = listed.json()["result"]
        assert page["pageSize"] == 1
        assert page["totalSize"] == 2
        assert page["tasks"][0]["id"] == fast["id"]
        assert "artifacts" not in page["tasks"][0]
        assert page["nextPageToken"] != ""
        assert "|" in page["nextPageToken"]
        rest = await rpc(
            client,
            "ListTasks",
            {"pageSize": 1, "pageToken": page["nextPageToken"]},
        )
        assert rest.json()["result"]["tasks"][0]["id"] == slow_id
        assert rest.json()["result"]["nextPageToken"] == ""
        fetched = await rpc(client, "GetTask", {"id": fast["id"]})
        assert fetched.json()["result"]["artifacts"][0]["parts"][0][
            "text"
        ] == ("второй")
        missing = await rpc(client, "GetTask", {"id": "no-such"})
        assert missing.json()["error"]["code"] == -32001
        canceled = await rpc(client, "CancelTask", {"id": slow_id})
        assert canceled.json()["result"]["status"]["state"] == (
            "TASK_STATE_CANCELED"
        )
        again = await rpc(client, "CancelTask", {"id": slow_id})
        assert (
            again.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
        )
        terminal = await rpc(client, "CancelTask", {"id": fast["id"]})
        assert terminal.json()["error"]["code"] == -32002
    assert created[0].stopped is True


async def test_history_length_and_filters(tmp_path):
    clock = Clock()
    async with session(tmp_path, clock=clock) as (client, _f, _c):
        first = await rpc(client, "SendMessage", message(message_id="a"))
        first_task = first.json()["result"]["task"]
        context = first_task["contextId"]
        await rpc(
            client,
            "SendMessage",
            message("дальше", message_id="b", contextId=context),
        )
        hidden = await rpc(
            client,
            "GetTask",
            {"id": first_task["id"], "historyLength": 0},
        )
        tail = await rpc(
            client,
            "ListTasks",
            {
                "contextId": context,
                "status": "TASK_STATE_COMPLETED",
                "historyLength": 1,
                "statusTimestampAfter": first_task["status"]["timestamp"],
            },
        )
        wide = await rpc(client, "ListTasks", {"pageSize": 0})
    assert "history" not in hidden.json()["result"]
    tasks = tail.json()["result"]["tasks"]
    assert tail.json()["result"]["totalSize"] == 1
    assert tasks[0]["history"][0]["messageId"] == "b"
    assert wide.json()["error"]["code"] == -32602
