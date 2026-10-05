"""`exec_fetch_url` через бэкенд: carrier-скрипт, редиректы, ошибки."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from gc_core.backends.local import LocalFsBackend
from gc_core.tools.exec.redirect_policy import (
    RedirectBlocked,
    TooManyRedirects,
)
from gc_core.tools.exec.remote_fetch import (
    RESULT_MARKER,
    afetch_via_backend,
    aone_hop,
    fetches_on_backend,
)


class ScriptedBackend:
    """Бэкенд, отвечающий на `arun_python` заготовленными результатами."""

    is_sandbox = True

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def aexec(self, *a, **k):
        raise AssertionError("not used")

    async def aexec_argv(self, *a, **k):
        raise AssertionError("not used")

    async def arun_python(self, code, **kwargs):
        self.calls.append((code, kwargs))
        return self.results.pop(0)


def _carrier_result(status, headers, body: bytes, url, truncated=False):
    import base64

    payload = {
        "status": status,
        "headers": headers,
        "truncated": truncated,
        "body": base64.b64encode(body).decode(),
        "url": url,
    }
    return SimpleNamespace(
        stdout=RESULT_MARKER + json.dumps(payload),
        stderr="",
        return_code=0,
        timed_out=False,
    )


async def test_one_hop_builds_httpx_response():
    backend = ScriptedBackend(
        [
            _carrier_result(
                200,
                [["Content-Type", "text/html; charset=utf-8"]],
                b"<p>hi</p>",
                "https://x/final",
            )
        ]
    )
    resp = await aone_hop(backend, "https://x", {"User-Agent": "ua"}, 7, None)
    assert isinstance(resp, httpx.Response)
    assert resp.status_code == 200
    assert resp.text == "<p>hi</p>"
    assert str(resp.url) == "https://x/final"
    code, kwargs = backend.calls[0]
    assert '\\"User-Agent\\": \\"ua\\"' in code  # JSON-литерал внутри скрипта
    assert kwargs["timeout"] == 7 + 15


async def test_redirect_hops_are_validated_on_host():
    backend = ScriptedBackend(
        [
            _carrier_result(302, [["Location", "/next"]], b"", "https://x/a"),
            _carrier_result(200, [], b"ok", "https://x/next"),
        ]
    )
    resp = await afetch_via_backend(
        backend,
        "https://x/a",
        {},
        5,
        None,
        allowed_schemes=["https"],
        url_blacklist=[],
    )
    assert resp.text == "ok"
    assert "https://x/next" in backend.calls[1][0]

    blocked = ScriptedBackend(
        [
            _carrier_result(
                302, [["Location", "https://evil/"]], b"", "https://x"
            )
        ]
    )
    with pytest.raises(RedirectBlocked):
        await afetch_via_backend(
            blocked,
            "https://x",
            {},
            5,
            None,
            allowed_schemes=["https"],
            url_blacklist=["evil"],
        )

    looping = ScriptedBackend(
        [_carrier_result(301, [["Location", "https://x"]], b"", "https://x")]
        * 3
    )
    with pytest.raises(TooManyRedirects):
        await afetch_via_backend(
            looping,
            "https://x",
            {},
            5,
            None,
            allowed_schemes=["https"],
            url_blacklist=[],
            max_redirects=2,
        )


async def test_carrier_failures_surface_as_httpx_errors():
    no_python = ScriptedBackend(
        [
            SimpleNamespace(
                stdout="",
                stderr="sh: 1: python3: not found",
                return_code=127,
                timed_out=False,
            )
        ]
    )
    with pytest.raises(httpx.ConnectError, match="no python3"):
        await aone_hop(no_python, "https://x", {}, 5, None)

    net_error = ScriptedBackend(
        [
            SimpleNamespace(
                stdout=RESULT_MARKER
                + json.dumps({"error": "URLError: timed out"}),
                stderr="",
                return_code=0,
                timed_out=False,
            )
        ]
    )
    with pytest.raises(httpx.ReadTimeout):
        await aone_hop(net_error, "https://x", {}, 5, None)


def test_fetches_on_backend_needs_setting_and_sandbox():
    sandbox = SimpleNamespace(is_sandbox=True)
    host = SimpleNamespace(is_sandbox=False)
    on = {"os_backend": {"fetch_url_execute_on": "os_backend"}}
    assert fetches_on_backend(on, sandbox) is True
    assert fetches_on_backend(on, host) is False
    assert (
        fetches_on_backend({"os_backend": {"kind": "docker"}}, sandbox)
        is False
    )
    assert fetches_on_backend({}, sandbox) is False
    assert fetches_on_backend({"os_backend": "docker"}, sandbox) is False


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/a":
            self.send_response(302)
            self.send_header("Location", "/b")
            self.end_headers()
            return
        body = "<html><body><p>привет</p></body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_carrier_script_runs_on_real_python(http_server, tmp_path):
    # Настоящий скрипт на настоящем python3 (локальный бэкенд) против
    # локального сервера: редирект не следуется скриптом, а хостом.
    backend = LocalFsBackend()
    resp = await afetch_via_backend(
        backend,
        f"{http_server}/a",
        {"User-Agent": "t"},
        10,
        None,
        allowed_schemes=["http"],
        url_blacklist=[],
    )
    assert resp.status_code == 200
    assert "привет" in resp.text
    assert str(resp.url).endswith("/b")
