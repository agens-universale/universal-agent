"""gc-serve: поверхности по адресам, диспетчер, занятый порт, разбор команды."""

import asyncio
import socket

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gc_server import cli
from gc_server.app import (
    AddressConflict,
    Dispatcher,
    ServerRuntime,
    aserve,
    bind_socket,
    build_surface,
    group_surfaces,
)
from gc_server.config import ServerConfig
from gc_testkit.settings import make_settings


def _cfg(**sections) -> ServerConfig:
    return ServerConfig.model_validate(sections)


def test_surfaces_get_their_own_addresses():
    groups = group_surfaces(_cfg(), ["webhook", "chat", "a2a", "swarm"])
    assert groups == {
        ("127.0.0.1", 8000): ["webhook"],
        ("127.0.0.1", 8090): ["chat"],
        ("127.0.0.1", 8091): ["a2a"],
        ("127.0.0.1", 8092): ["swarm"],
    }


def test_surfaces_on_one_port_share_one_group():
    cfg = _cfg(chat={"port": 9000}, a2a={"port": 9000})
    assert group_surfaces(cfg, ["chat", "a2a"]) == {
        ("127.0.0.1", 9000): ["chat", "a2a"]
    }


def test_webhook_and_swarm_cannot_share_a_port():
    cfg = _cfg(swarm={"port": 8000})
    with pytest.raises(AddressConflict) as err:
        group_surfaces(cfg, ["webhook", "swarm"])
    assert "webhook и swarm" in str(err.value)


def test_busy_port_is_refused_with_its_address():
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        with pytest.raises(AddressConflict) as err:
            bind_socket("127.0.0.1", port, ["chat", "a2a"])
    finally:
        held.close()
    text = str(err.value)
    assert f"127.0.0.1:{port}" in text and "chat, a2a" in text
    assert "Traceback" not in text


def _app(path: str, method: str = "GET") -> FastAPI:
    app = FastAPI()

    async def handler():
        return {"from": path}

    app.add_api_route(path, handler, methods=[method])
    return app


def test_dispatcher_routes_by_path_and_falls_back_to_404():
    client = TestClient(Dispatcher([_app("/v1/models"), _app("/rpc", "POST")]))
    assert client.get("/v1/models").json() == {"from": "/v1/models"}
    assert client.post("/rpc").json() == {"from": "/rpc"}
    assert client.get("/rpc").status_code == 405  # путь свой, метод не тот
    assert client.get("/nope").status_code == 404


def test_cli_rejects_a_repeated_surface(monkeypatch, tmp_path, capsys):
    (tmp_path / "config_server.yml").write_text("{}\n", encoding="utf-8")
    code = cli.main(
        [
            "chat",
            "chat",
            "--server-config",
            str(tmp_path / "config_server.yml"),
        ]
    )
    assert code == 2
    assert "дважды" in capsys.readouterr().err


def test_cli_rejects_an_unknown_surface(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["ftp"])
    assert exc.value.code == 2
    assert "неизвестная поверхность 'ftp'" in capsys.readouterr().err


def test_cli_setting_flag_value_is_not_a_surface():
    args = cli._parse_args(["chat", "--agent.max_steps", "5"])
    assert args.surfaces == ["chat"]
    assert args.overrides == {"agent.max_steps": 5}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_two_surfaces_answer_on_their_own_ports(tmp_path):
    webhook_port, chat_port = _free_port(), _free_port()
    cfg = _cfg(webhook={"port": webhook_port}, chat={"port": chat_port})
    runtime = ServerRuntime(
        base=make_settings(
            {
                "agent.model": "m",
                "paths.threads_data_path": str(tmp_path / "threads"),
            }
        ),
        config=cfg,
    )
    surfaces = [
        build_surface(runtime, "webhook"),
        build_surface(runtime, "chat"),
    ]
    stop = asyncio.Event()
    serving = asyncio.create_task(aserve(runtime, surfaces, stop=stop))
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    health = await client.get(
                        f"http://127.0.0.1:{webhook_port}/healthcheck"
                    )
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            models = await client.get(
                f"http://127.0.0.1:{chat_port}/v1/models"
            )
            wrong = await client.get(
                f"http://127.0.0.1:{chat_port}/healthcheck"
            )
    finally:
        stop.set()
        await asyncio.wait_for(serving, 15)
    assert health.json() == {"status": "ok"}
    assert models.status_code == 200
    assert wrong.status_code == 404  # health вебхука не на порту chat
