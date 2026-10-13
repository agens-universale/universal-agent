"""Карточка, конверт JSON-RPC, версия и Bearer."""

from harness import message, rpc, session


async def test_card_matches_the_channel_defaults(tmp_path):
    async with session(tmp_path) as (client, _facade, _created):
        response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "GigaCode"
    assert card["description"] == "Универсальный агент GigaCode"
    assert card["supportedInterfaces"] == [
        {
            "url": "http://127.0.0.1:8091/rpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]
    assert card["version"] == "0.4.0"
    assert card["capabilities"] == {
        "streaming": False,
        "pushNotifications": False,
        "extendedAgentCard": False,
    }
    assert card["defaultInputModes"] == ["text/plain"]
    assert card["defaultOutputModes"] == ["text/plain"]
    assert card["skills"] == [
        {
            "id": "agent",
            "name": "Агент",
            "description": "Универсальный агент GigaCode",
            "tags": ["agent"],
        }
    ]
    assert "securitySchemes" not in card


async def test_card_reads_section_and_declares_bearer(tmp_path):
    async with session(
        tmp_path,
        token="secret",
        **{
            "a2a.name": "Пир",
            "a2a.description": "Свой агент",
            "a2a.host": "127.0.0.1",
            "a2a.port": 9000,
        },
    ) as (client, _facade, _created):
        open_card = await client.get("/.well-known/agent-card.json")
    card = open_card.json()
    assert card["name"] == "Пир"
    assert card["description"] == "Свой агент"
    assert card["skills"][0]["description"] == "Свой агент"
    assert card["supportedInterfaces"][0]["url"] == "http://127.0.0.1:9000/rpc"
    assert card["securitySchemes"] == {
        "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
    }
    assert card["securityRequirements"] == [{"schemes": {"bearer": {}}}]


async def test_card_public_url_is_not_bind_host(tmp_path):
    async with session(
        tmp_path,
        **{
            "a2a.host": "0.0.0.0",
            "a2a.port": 8091,
            "a2a.public_url": "http://192.168.1.10:8091/rpc",
        },
    ) as (client, _facade, _created):
        response = await client.get("/.well-known/agent-card.json")
    assert response.json()["supportedInterfaces"][0]["url"] == (
        "http://192.168.1.10:8091/rpc"
    )


async def test_rpc_requires_bearer_card_does_not(tmp_path):
    async with session(tmp_path, token="secret") as (client, _f, _c):
        card = await client.get("/.well-known/agent-card.json")
        missing = await rpc(client, "ListTasks")
        wrong = await rpc(client, "ListTasks", token="nope")
        ok = await rpc(client, "ListTasks", token="secret")
    assert card.status_code == 200
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == -32000
    assert missing.json()["error"]["message"] == "Unauthorized"
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["result"]["totalSize"] == 0


async def test_version_header_query_and_rejection(tmp_path):
    async with session(tmp_path) as (client, _f, created):
        missing = await rpc(client, "ListTasks", version=None)
        other = await rpc(client, "ListTasks", version="0.3")
        query = await rpc(
            client, "ListTasks", version=None, query="A2A-Version=1.0"
        )
        header_wins = await rpc(
            client,
            "ListTasks",
            version="0.3",
            query="A2A-Version=1.0",
        )
        empty = await rpc(
            client, "ListTasks", version="", query="A2A-Version=1.0"
        )
    assert missing.json()["error"]["code"] == -32009
    assert missing.json()["error"]["data"]["reason"] == (
        "VersionNotSupportedError"
    )
    assert other.json()["error"]["code"] == -32009
    assert query.status_code == 200
    assert header_wins.json()["error"]["code"] == -32009
    assert empty.json()["error"]["code"] == -32009
    assert created == []


async def test_envelope_errors(tmp_path):
    async with session(tmp_path) as (client, _f, _c):
        broken = await client.post(
            "/rpc",
            content=b"not-json",
            headers={"A2A-Version": "1.0"},
        )
        not_object = await client.post(
            "/rpc",
            json=["nope"],
            headers={"A2A-Version": "1.0"},
        )
        bare = await client.post(
            "/rpc",
            json={"id": "abc", "method": "ListTasks"},
            headers={"A2A-Version": "1.0"},
        )
    assert broken.status_code == 400
    assert broken.json()["error"]["code"] == -32700
    assert broken.json()["error"]["data"]["domain"] == "a2a-protocol.org"
    assert broken.json()["error"]["data"]["@type"].endswith("ErrorInfo")
    assert not_object.status_code == 200
    assert not_object.json()["error"]["code"] == -32600
    assert bare.status_code == 200
    assert bare.json()["id"] == "abc"
    assert bare.json()["error"]["code"] == -32600


async def test_unknown_and_refused_methods_do_not_start_a_turn(tmp_path):
    async with session(tmp_path) as (client, _f, created):
        missing = await rpc(client, "Foo")
        stream = await rpc(client, "SendStreamingMessage", {})
        extended = await rpc(client, "GetExtendedAgentCard", {})
        push = await rpc(client, "CreateTaskPushNotificationConfig", {})
        listed = await rpc(client, "ListTasks")
    assert missing.json()["error"]["code"] == -32601
    assert missing.json()["error"]["data"]["reason"] == "MethodNotFoundError"
    assert stream.json()["error"]["code"] == -32004
    assert stream.json()["error"]["data"]["reason"] == (
        "UnsupportedOperationError"
    )
    assert extended.json()["error"]["code"] == -32004
    assert push.json()["error"]["code"] == -32003
    assert push.json()["error"]["data"]["reason"] == (
        "PushNotificationNotSupportedError"
    )
    assert listed.json()["result"]["totalSize"] == 0
    assert created == []


async def test_push_config_and_tenant_reject_before_a_task(tmp_path):
    async with session(tmp_path) as (client, _f, created):
        pushed = await rpc(
            client,
            "SendMessage",
            {
                "configuration": {"taskPushNotificationConfig": {"url": "x"}},
                **message(),
            },
        )
        tenant = await rpc(
            client,
            "SendMessage",
            {"tenant": "acme", **message(message_id="m-2")},
        )
        listed = await rpc(client, "ListTasks")
    assert pushed.json()["error"]["code"] == -32003
    assert tenant.json()["error"]["code"] == -32602
    assert tenant.json()["error"]["data"]["reason"] == "INVALID_PARAMS"
    assert listed.json()["result"]["totalSize"] == 0
    assert created == []
