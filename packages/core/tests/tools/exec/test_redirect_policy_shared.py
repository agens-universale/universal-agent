"""Политика URL проверяется на КАЖДОМ переходе — у всех трёх HTTP-тулов.

Гвард ``enforce_url_policy`` проверяет только адрес, который назвала модель.
Этого мало: сервер вправе ответить ``302`` куда угодно, и запрет «не ходить
в 169.254.169.254» обходится одним заголовком ``Location``.

Петля разворота редиректов была скопирована дважды — в ``simple_fetch_url`` и
``download_file``, — а у ``fetch_url``, который регистрируется ПО УМОЛЧАНИЮ,
её не было вовсе: он звал ``follow_redirects=True`` и доверял всей цепочке.
То есть дырявым был именно тот тул, которым агент пользуется чаще всего.

Здесь проверяется свойство, а не наличие кода: сервер отвечает редиректом на
запрещённый адрес, и тул обязан отказать, не сходив туда.
"""
import json

import httpx
import pytest

from gc_core.tools.exec.download_file import DownloadFile
from gc_core.tools.exec.fetch_url import FetchUrl
from gc_core.tools.exec.simple_fetch_url import SimpleFetchUrl
from gc_testkit.helpers.tool_ctx import CallableTool, with_local_ctx

#: Классика SSRF: облачные метаданные. Запрещаем явно, чтобы тест не зависел
#: от того, какие правила стоят по умолчанию.
FORBIDDEN = "http://169.254.169.254/latest/meta-data/"


def _policy_config(launch_root=None):
    cfg = {
        "url_policy": {
            "enabled": True,
            "allowed_schemes": ["http", "https"],
            "url_blacklist": ["169.254.169.254"],
        }
    }
    if launch_root is not None:
        cfg["launch_root"] = str(launch_root)
        cfg["download"] = {"download_dir": "downloads", "max_bytes": 1_000_000}
    return cfg


def _redirecting(monkeypatch, module, visited: list):
    """Сервер, который на первый запрос отвечает 302 на запрещённый адрес."""
    from gc_core.utils import async_http

    def _dispatch(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if "169.254" in str(request.url):
            return httpx.Response(200, content=b"SECRET")
        return httpx.Response(302, headers={"Location": FORBIDDEN})

    factory = lambda **kw: httpx.AsyncClient(  # noqa: E731
        transport=httpx.MockTransport(_dispatch)
    )
    monkeypatch.setattr(async_http, "get_async_client", factory)
    monkeypatch.setattr(module, "get_async_client", factory, raising=False)
    return factory


def _patch_tool_globals(monkeypatch, factory, *targets):
    import inspect

    for target in targets:
        g = getattr(inspect.unwrap(target), "__globals__", None)
        if g is not None and "get_async_client" in g:
            monkeypatch.setitem(g, "get_async_client", factory)


def test_fetch_url_refuses_a_redirect_into_a_blocked_address(monkeypatch):
    """Главный тул: раньше он шёл по цепочке не глядя."""
    from gc_core.tools.exec import fetch_url as mod

    visited: list = []
    factory = _redirecting(monkeypatch, mod, visited)
    _patch_tool_globals(monkeypatch, factory, FetchUrl.acall)

    tool = CallableTool(with_local_ctx(FetchUrl, config_dict=_policy_config())._tool)
    out = json.loads(tool(url="https://example.com/start"))

    assert out["ok"] is False, out
    assert out["error"].get("blocked_on_redirect") is True, out
    assert not any("169.254" in u for u in visited), (
        f"тул всё-таки сходил на запрещённый адрес: {visited}"
    )


def test_simple_fetch_url_refuses_the_same_redirect(monkeypatch):
    from gc_core.tools.exec import simple_fetch_url as mod

    visited: list = []
    factory = _redirecting(monkeypatch, mod, visited)
    _patch_tool_globals(monkeypatch, factory, SimpleFetchUrl.acall)

    tool = CallableTool(with_local_ctx(SimpleFetchUrl, config_dict=_policy_config())._tool)
    out = json.loads(tool(url="https://example.com/start"))

    assert out["ok"] is False, out
    assert not any("169.254" in u for u in visited), visited


def test_download_file_refuses_the_same_redirect(monkeypatch, tmp_path):
    from gc_core.tools.exec import download_file as mod

    launch_root = tmp_path / "root"
    launch_root.mkdir()
    visited: list = []
    factory = _redirecting(monkeypatch, mod, visited)
    _patch_tool_globals(monkeypatch, factory, DownloadFile.acall,
                        DownloadFile._http_get_bytes)

    tool = CallableTool(
        with_local_ctx(DownloadFile, config_dict=_policy_config(launch_root))._tool
    )
    out = json.loads(tool(url="https://example.com/start", filename="x.pdf"))

    assert out["ok"] is False, out
    assert not any("169.254" in u for u in visited), visited


def test_all_three_share_one_redirect_loop():
    """Копий правила быть не должно: две расходятся молча, третья дырявая."""
    import inspect

    from gc_core.tools.exec import download_file, fetch_url, simple_fetch_url
    from gc_core.tools.exec import redirect_policy

    for module in (fetch_url, simple_fetch_url, download_file):
        src = inspect.getsource(module)
        assert "redirect_policy" in src, (
            f"{module.__name__} не пользуется общей петлёй редиректов"
        )
        assert "urljoin(current" not in src, (
            f"{module.__name__} завёл собственную петлю редиректов заново"
        )
    assert hasattr(redirect_policy, "fetch_validating_redirects")
    assert hasattr(redirect_policy, "stream_validating_redirects")
