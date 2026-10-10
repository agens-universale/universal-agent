"""Реальные TLS-рукопожатия модели и MCP: CA, имя сервера и mTLS."""

import contextlib
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from gc_core.tools.connect_mcp.stream_client import (
    _make_httpx_factory,
    _make_stream_http_client,
)
from gc_core.utils.async_http import (
    aclose_clients,
    build_ssl_context,
    get_async_client,
)
from gc_core.utils.ssl_config import get_client_cert, get_verify_ssl


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("Для генерации временных TLS-сертификатов нужен openssl")
    root = tmp_path_factory.mktemp("tls")
    config = root / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
        "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\nbasicConstraints=critical,CA:TRUE\n",
        encoding="utf-8",
    )
    for name in ("trusted", "other"):
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-config",
                str(config),
                "-keyout",
                str(root / f"{name}.key"),
                "-out",
                str(root / f"{name}.pem"),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    return root


@contextlib.contextmanager
def https_server(root, *, require_client=False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(
                {
                    "client_cert": bool(self.connection.getpeercert()),
                    "data": [{"id": "test"}],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(root / "trusted.pem", root / "trusted.key")
    if require_client:
        ctx.load_verify_locations(root / "trusted.pem")
        ctx.verify_mode = ssl.CERT_REQUIRED
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def make_client(kind, verify, cert, url):
    if kind == "model":
        return get_async_client(verify=verify, cert=cert)
    if kind == "mcp-sse":
        return _make_httpx_factory(verify, cert, url=url)(timeout=2)
    return _make_stream_http_client(None, None, 2, verify, cert, base_url=url)


@pytest.mark.parametrize("kind", ["model", "mcp-sse", "mcp-stream"])
@pytest.mark.parametrize("verify_mode", ["ca", "off"])
async def test_client_certificate_survives_each_verification_mode(
    tls_files, kind, verify_mode
):
    cfg = {
        "verify-ssl": str(tls_files / "trusted.pem")
        if verify_mode == "ca"
        else False,
        "client-cert": str(tls_files / "trusted.pem"),
        "client-key": str(tls_files / "trusted.key"),
    }
    with https_server(tls_files, require_client=True) as url:
        client = make_client(
            kind, get_verify_ssl(cfg), get_client_cert(cfg), url
        )
        try:
            response = await client.get(url, timeout=2)
            assert response.status_code == 200
            assert response.json()["client_cert"] is True
        finally:
            await client.aclose()
            await aclose_clients()


@pytest.mark.parametrize("kind", ["model", "mcp-sse", "mcp-stream"])
async def test_custom_ca_keeps_chain_and_hostname_verification(
    tls_files, kind
):
    with https_server(tls_files) as url:
        good = make_client(kind, str(tls_files / "trusted.pem"), None, url)
        bad = make_client(kind, str(tls_files / "other.pem"), None, url)
        try:
            assert (await good.get(url, timeout=2)).status_code == 200
            with pytest.raises(
                httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"
            ):
                await bad.get(url, timeout=2)
            with pytest.raises(
                httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"
            ):
                await good.get(
                    url.replace("localhost", "127.0.0.1"), timeout=2
                )
        finally:
            await good.aclose()
            await bad.aclose()
            await aclose_clients()


def test_sync_probe_accepts_configured_ca(tls_files):
    from gc_core.model_picker import _requests_get_ex

    with https_server(tls_files) as url:
        get = _requests_get_ex
        assert get(url, {}, str(tls_files / "trusted.pem")).status == 200
        rejected = get(
            url.replace("localhost", "127.0.0.1"),
            {},
            str(tls_files / "trusted.pem"),
        )
        assert rejected.status is None
        assert "CERTIFICATE_VERIFY_FAILED" in rejected.network_error


def test_ca_paths_expand_without_disabling_verification(
    tls_files, monkeypatch
):
    monkeypatch.setenv("GC_TEST_TLS_DIR", str(tls_files))
    ctx = build_ssl_context("$GC_TEST_TLS_DIR/trusted.pem")
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.cert_store_stats()["x509_ca"] >= 1


def test_missing_ca_fails_without_insecure_fallback(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_ssl_context(str(tmp_path / "missing.pem"))


@pytest.mark.parametrize("kind", ["model", "mcp-sse", "mcp-stream"])
async def test_proxy_uses_the_same_tls_context_as_direct_client(
    tls_files, monkeypatch, kind
):
    proxy = "http://proxy.example:3128"
    url = "https://mcp.example/mcp"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    verify = str(tls_files / "trusted.pem")
    cert = (verify, str(tls_files / "trusted.key"))
    client = (
        get_async_client(verify=verify, cert=cert, proxies={"https": proxy})
        if kind == "model"
        else make_client(kind, verify, cert, url)
    )
    try:
        ctx = client._transport._pool._ssl_context
        assert ctx.check_hostname
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        transports = [
            transport
            for transport in client._mounts.values()
            if transport is not None
        ]
        assert transports
        assert all(
            transport._pool._ssl_context is ctx for transport in transports
        )
    finally:
        await client.aclose()
        await aclose_clients()


@pytest.fixture
def env_ca(tls_files, tmp_path, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    hashed_dir = tmp_path / "ca"
    hashed_dir.mkdir()
    subject_hash = subprocess.check_output(
        [
            "openssl",
            "x509",
            "-hash",
            "-noout",
            "-in",
            str(tls_files / "trusted.pem"),
        ],
        text=True,
    ).strip()
    shutil.copyfile(
        tls_files / "trusted.pem", hashed_dir / f"{subject_hash}.0"
    )
    return {
        "SSL_CERT_FILE": str(tls_files / "trusted.pem"),
        "SSL_CERT_DIR": str(hashed_dir),
    }


@pytest.mark.parametrize("env_name", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
@pytest.mark.parametrize("kind", ["model", "mcp-sse", "mcp-stream"])
async def test_environment_ca_preserves_transport_policy(
    tls_files, env_ca, monkeypatch, env_name, kind
):
    monkeypatch.setenv(env_name, env_ca[env_name])
    cert = (str(tls_files / "trusted.pem"), str(tls_files / "trusted.key"))
    with https_server(tls_files, require_client=True) as url:
        client = make_client(kind, True, cert, url)
        try:
            if kind == "model":
                # Клиенты модели по-прежнему требуют CA в явном конфиге.
                with pytest.raises(
                    httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"
                ):
                    await client.get(url, timeout=2)
            else:
                response = await client.get(url, timeout=2)
                assert response.json()["client_cert"] is True
                with pytest.raises(
                    httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"
                ):
                    await client.get(
                        url.replace("localhost", "127.0.0.1"), timeout=2
                    )
        finally:
            await client.aclose()
            await aclose_clients()


@pytest.mark.parametrize("kind", ["mcp-sse", "mcp-stream"])
async def test_mcp_ca_precedence(tls_files, env_ca, monkeypatch, kind):
    # HTTPX предпочитает SSL_CERT_FILE каталогу, а явный verify — окружению.
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files / "other.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", env_ca["SSL_CERT_DIR"])
    with https_server(tls_files) as url:
        environment = make_client(kind, True, None, url)
        explicit = make_client(kind, str(tls_files / "trusted.pem"), None, url)
        try:
            with pytest.raises(
                httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"
            ):
                await environment.get(url, timeout=2)
            assert (await explicit.get(url, timeout=2)).status_code == 200
        finally:
            await environment.aclose()
            await explicit.aclose()


@pytest.mark.parametrize("verify", [True, False])
def test_mcp_invalid_environment_ca_is_not_silently_ignored(
    tmp_path, monkeypatch, verify
):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    if verify:
        with pytest.raises(FileNotFoundError):
            build_ssl_context(verify, trust_env=True)
    else:
        assert (
            build_ssl_context(verify, trust_env=True).verify_mode
            == ssl.CERT_NONE
        )
