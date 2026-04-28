import asyncio
import socket
import threading
import time

import httpx
import pytest
from starlette.testclient import TestClient

from office_mcp.server import ServerConfig, build_asgi_app, build_server


def test_build_server_returns_fastmcp_instance(tmp_path):
    cfg = ServerConfig(
        allowed_dirs=[str(tmp_path)],
        allow_any=False,
        max_file_size=10**7,
        max_output_chars=10**6,
        timeout_s=60,
    )
    server = build_server(cfg)
    assert server is not None


async def test_build_server_registers_tools_with_correct_names(tmp_path):
    cfg = ServerConfig(
        allowed_dirs=[str(tmp_path)],
        allow_any=False,
        max_file_size=10**7,
        max_output_chars=10**6,
        timeout_s=60,
    )
    server = build_server(cfg)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "read_file",
        "read_document",
        "create_docx_file",
        "create_docx",
        "create_xlsx_file",
        "create_xlsx",
        "inspect_xlsx",
        "query_xlsx",
        "aggregate_xlsx",
        "pivot_xlsx",
    }


def test_health_endpoint_returns_ok(tmp_path):
    cfg = ServerConfig([str(tmp_path)], False, 10**7, 10**6, 60)
    app = build_asgi_app(cfg)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def running_server(tmp_path, fixtures_dir):
    """Start the ASGI app on a free port in a background thread; yield base URL.
    Allows reading from both tmp_path and the test fixtures dir."""
    import uvicorn

    cfg = ServerConfig(
        [str(tmp_path), str(fixtures_dir)], False, 10**7, 10**6, 60,
    )
    app = build_asgi_app(cfg)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def run():
        asyncio.run(server.serve())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not become ready in 10s")

    yield base

    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.asyncio
async def test_e2e_list_tools_via_streamable_http(running_server):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"{running_server}/mcp"
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {t.name for t in result.tools}
            assert names == {
        "read_file",
        "read_document",
        "create_docx_file",
        "create_docx",
        "create_xlsx_file",
        "create_xlsx",
        "inspect_xlsx",
        "query_xlsx",
        "aggregate_xlsx",
        "pivot_xlsx",
    }


@pytest.mark.asyncio
async def test_e2e_call_read_file_via_streamable_http(running_server, sample_docx):
    """End-to-end: client calls read_file, server returns markdown with meta-block."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"{running_server}/mcp"
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("read_file", {"path": str(sample_docx)})
            assert not result.isError
            assert len(result.content) >= 1
            text = result.content[0].text
            assert text.startswith("[document: format=docx")
            assert "Sample Heading" in text


def test_server_config_default_max_query_rows():
    cfg = ServerConfig(
        allowed_dirs=["/tmp"],
        allow_any=False,
        max_file_size=1,
        max_output_chars=1,
        timeout_s=1,
    )
    assert cfg.max_query_rows == 10000


def test_server_config_custom_max_query_rows():
    cfg = ServerConfig(
        allowed_dirs=["/tmp"],
        allow_any=False,
        max_file_size=1,
        max_output_chars=1,
        timeout_s=1,
        max_query_rows=5000,
    )
    assert cfg.max_query_rows == 5000
