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

    factory = lambda **kw: httpx.AsyncClient(
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

    tool = CallableTool(with_local_ctx(FetchUrl, _policy_config())._tool)
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

    tool = CallableTool(with_local_ctx(SimpleFetchUrl, _policy_config())._tool)
    out = json.loads(tool(url="https://example.com/start"))

    assert out["ok"] is False, out
    assert not any("169.254" in u for u in visited), visited


def test_download_file_refuses_the_same_redirect(monkeypatch, tmp_path):
    from gc_core.tools.exec import download_file as mod

    launch_root = tmp_path / "root"
    launch_root.mkdir()
    visited: list = []
    factory = _redirecting(monkeypatch, mod, visited)
    _patch_tool_globals(
        monkeypatch, factory, DownloadFile.acall, DownloadFile._http_get_bytes
    )

    tool = CallableTool(
        with_local_ctx(DownloadFile, _policy_config(launch_root))._tool
    )
    out = json.loads(tool(url="https://example.com/start", filename="x.pdf"))

    assert out["ok"] is False, out
    assert not any("169.254" in u for u in visited), visited


def test_all_three_share_one_redirect_loop():
    """Копий правила быть не должно: две расходятся молча, третья дырявая."""
    import inspect

    from gc_core.tools.exec import (
        download_file,
        fetch_url,
        redirect_policy,
        simple_fetch_url,
    )

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


# --- R8: один потолок на три тула, и петля — не запрет политики -------------


def test_all_three_share_one_redirect_ceiling():
    """Потолок переходов общий: 5 / 10 / 5 — та же болезнь, что две петли.

    Общая петля пришла с умолчанием 5, и у ``fetch_url`` потолок молча упал
    вчетверо: до неё он звал ``follow_redirects=True``, а httpx берёт 20.
    Длинные цепочки на корпоративных порталах (SSO, нормализация домена,
    языковой редирект) в пять переходов укладываются не всегда — тул начинал
    отказывать там, где раньше работал.
    """
    import inspect

    from gc_core.tools.exec import download_file, fetch_url, simple_fetch_url
    from gc_core.tools.exec.redirect_policy import DEFAULT_MAX_REDIRECTS

    assert DEFAULT_MAX_REDIRECTS == 20, (
        "общий потолок разошёлся с тем, что брал httpx до переезда"
    )
    # Своих чисел у тулов быть не должно: разъезжаются они молча.
    for module in (fetch_url, simple_fetch_url, download_file):
        src = inspect.getsource(module)
        for literal in (
            "max_redirects=5",
            "max_redirects=10",
            "_MAX_REDIRECTS = ",
        ):
            assert literal not in src, (
                f"{module.__name__} завёл собственный потолок: {literal}"
            )


def _looping(monkeypatch, module, hops: int):
    """Сервер, который редиректит по кругу дольше любого разумного потолка."""
    from gc_core.utils import async_http

    seen = {"n": 0}

    def _dispatch(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        return httpx.Response(
            302, headers={"Location": f"https://example.com/hop{seen['n']}"}
        )

    factory = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(_dispatch)
    )
    monkeypatch.setattr(async_http, "get_async_client", factory)
    monkeypatch.setattr(module, "get_async_client", factory, raising=False)
    return factory, seen


def test_a_redirect_loop_is_not_reported_as_a_policy_block(monkeypatch):
    """Петля обязана называться петлёй, а не «запрещено правилами».

    Превышение потолка отдавалось моделью тем же конвертом, что и блокировка
    политикой. Модель делала неверный вывод — что адрес под запретом — и
    переставала пробовать, вместо того чтобы сообщить о петле.
    """
    from gc_core.tools.exec import fetch_url as mod

    factory, seen = _looping(monkeypatch, mod, hops=30)
    _patch_tool_globals(monkeypatch, factory, FetchUrl.acall)

    tool = CallableTool(with_local_ctx(FetchUrl, _policy_config())._tool)
    out = json.loads(tool(url="https://example.com/start"))

    assert out["ok"] is False, out
    assert out["error"]["type"] == "too_many_redirects", (
        f"петля редиректов названа не своим именем: {out['error']}"
    )
    assert out["error"].get("blocked_on_redirect") is not True, (
        "петля всё ещё отдаётся как запрет политики"
    )
