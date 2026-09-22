"""HTTP-тулы: fetch_url / simple_fetch_url.

С переезда на httpx (Задача 19) тулы асинхронные, а сеть подменяется
транспортом httpx, а не заглушкой requests: подмена на уровне транспорта
проверяет и путь редиректов, который у этих тулов свой.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx

from gc_core.tools.exec import fetch_url as fetch_url_module
from gc_core.tools.exec.fetch_url import FetchUrl
from gc_core.tools.exec.simple_fetch_url import SimpleFetchUrl
from gc_core.utils import async_http


from gc_testkit.helpers.tool_ctx import CallableTool, with_local_ctx


def _resp(text="", content_type="text/html", status_code=200, content=None):
    """Ответ сети как его отдаст httpx."""
    return httpx.Response(
        status_code,
        content=content if content is not None else text.encode("utf-8"),
        headers={"Content-Type": content_type},
    )


def _install(monkeypatch, handler, seen: dict | None = None):
    """Подменить транспорт httpx у HTTP-тулов.

    ``handler`` — либо ``httpx.Response``, либо функция ``request → Response``,
    либо исключение, которое надо поднять.
    """
    def _dispatch(request: httpx.Request) -> httpx.Response:
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler(request)
        return handler

    def factory(**kw):
        # `seen` нужен прокси-тестам: у httpx прокси задаётся при СОЗДАНИИ
        # клиента, а не аргументом запроса, как было у requests. Поэтому и
        # наблюдаем за фабрикой, а не за вызовом `.get`.
        if seen is not None:
            seen.setdefault("calls", []).append(kw)
        return httpx.AsyncClient(transport=httpx.MockTransport(_dispatch))

    # Патчим globals САМОЙ функции тула, а не модуль «по имени»: conftest
    # каталога tests/tools подменяет gc_core.tools на время импорта, и класс
    # оказывается замкнут на другой экземпляр модуля. Подмена по sys.modules
    # промахивалась — тест молча уходил в настоящую сеть и сравнивал ответ
    # example.com с ожидаемым HTML.
    import inspect

    monkeypatch.setattr(async_http, "get_async_client", factory)
    for cls in (FetchUrl, SimpleFetchUrl):
        target = inspect.unwrap(getattr(cls, 'acall', cls.__call__))
        globals_dict = getattr(target, "__globals__", None)
        if globals_dict is not None and "get_async_client" in globals_dict:
            monkeypatch.setitem(globals_dict, "get_async_client", factory)


def _call(tool, **kwargs) -> str:
    """Позвать тул синхронно — той же дорогой, что ``apply_tool``.

    У тула две точки входа: корутинная ``acall`` для цикла агента и
    синхронная ``__call__`` поверх неё (форма из docs/async.md). Гоняем
    синхронную: она проходит всю цепочку и заодно сторожит, что публичный
    синхронный вход не отвалится снова.
    """
    return tool(**kwargs)


def _url_policy_config(allowed_schemes=None, url_blacklist=None):
    return {
        "url_policy": {
            "enabled": True,
            "allowed_schemes": allowed_schemes or ["http", "https"],
            "url_blacklist": url_blacklist or [],
        }
    }


def test_fetch_url_validation():
    tool = with_local_ctx(FetchUrl)
    assert json.loads(_call(tool, url=""))["error"]["field"] == "url"
    assert json.loads(_call(tool, url="ftp://x"))["error"]["field"] == "url"
    assert json.loads(_call(tool, url="https://x", timeout=-1))["error"]["field"] == "timeout"
    assert json.loads(_call(tool, url="https://x", max_preview_chars=0))["error"]["field"] == "max_preview_chars"
    assert json.loads(_call(tool, url="https://x", offset=-1))["error"]["field"] == "offset"
    assert json.loads(_call(tool, url="https://x", limit=0))["error"]["field"] == "limit"
    assert json.loads(_call(tool, url="https://x", proxy="not-a-proxy"))["error"]["field"] == "proxy"
    assert json.loads(_call(tool, url="https://x", proxy=123))["error"]["field"] == "proxy"


def test_fetch_url_policy_blocks_before_request(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("request should not be executed for blocked URL")

    _install(monkeypatch, fail_if_called)

    data = json.loads(_call(
        with_local_ctx(FetchUrl, config_dict=_url_policy_config(url_blacklist=["blocked.example"])),
            url="https://blocked.example/page",
        ))

    assert data["ok"] is False
    assert data["result_type"] == "error"
    assert data["error"]["type"] == "url_blocked_by_policy"
    assert data["error"]["matched_rule"] == "blocked.example"


def test_fetch_url_policy_blocks_disallowed_scheme_before_legacy_validation(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("request should not be executed for blocked URL")

    _install(monkeypatch, fail_if_called)

    data = json.loads(_call(
        with_local_ctx(FetchUrl, config_dict=_url_policy_config(allowed_schemes=["https"])),
            url="http://example.com",
        ))

    assert data["ok"] is False
    assert data["error"]["type"] == "url_validation_failed"


def test_fetch_url_binary(monkeypatch):
    _install(monkeypatch, _resp("", content_type="application/pdf"))
    data = json.loads(_call(with_local_ctx(FetchUrl), url="https://example.com/a.pdf"))
    assert data["ok"] is True and data["is_binary"] is True and data["content"] == "binary data"


def test_fetch_url_extraction_fallback(monkeypatch):
    _install(monkeypatch, _resp("<html><body><p>hello world</p></body></html>"))
    monkeypatch.setattr(fetch_url_module.trafilatura, "extract", lambda *a, **k: None)
    monkeypatch.setattr(fetch_url_module, "Document", lambda *a, **k: SimpleNamespace(summary=lambda html_partial=True: "<p>fallback</p>"))
    data = json.loads(_call(with_local_ctx(FetchUrl), url="https://example.com"))
    assert data["ok"] is True
    assert data["is_binary"] is False
    assert data["content"]


def test_fetch_url_request_failure(monkeypatch):
    def _raise(*_a, **_k):
        raise httpx.ConnectError("boom")

    _install(monkeypatch, _raise)
    data = json.loads(_call(with_local_ctx(FetchUrl), url="https://example.com"))
    assert data["ok"] is False
    assert data["error"]["type"] == "request_failed"


def test_fetch_url_caches_full_content_and_slices(monkeypatch):
    calls = {"n": 0}

    def _get(*_a, **_k):
        calls["n"] += 1
        return _resp("<html></html>")

    _install(monkeypatch, _get)
    monkeypatch.setattr(fetch_url_module.trafilatura, "extract", lambda *a, **k: "ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    tool = CallableTool(FetchUrl(state=state))

    first = json.loads(_call(tool, url="https://example.com", offset=5, limit=10))
    assert first["ok"] is True
    assert first["content"] == "FGHIJKLMNO"
    assert first["total_chars"] == 26
    assert first["returned_chars"] == 10
    assert first["has_more"] is True
    assert first["next_offset"] == 15
    assert first["cache_hit"] is False
    assert state.shared.fetch_url_cache["https://example.com"]["content"] == "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    second = json.loads(_call(tool, url="https://example.com", offset=20, limit=10))
    assert second["ok"] is True
    assert second["content"] == "UVWXYZ"
    assert second["returned_chars"] == 6
    assert second["has_more"] is False
    assert second["next_offset"] is None
    assert second["cache_hit"] is True
    assert calls["n"] == 1


def test_fetch_url_offset_beyond_end_returns_empty_from_cache(monkeypatch):
    monkeypatch.setattr(fetch_url_module.trafilatura, "extract", lambda *a, **k: "short")
    _install(monkeypatch, _resp("<html></html>"))

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    tool = CallableTool(FetchUrl(state=state))
    _ = json.loads(_call(tool, url="https://example.com", offset=0, limit=5))

    data = json.loads(_call(tool, url="https://example.com", offset=99, limit=5))
    assert data["ok"] is True
    assert data["content"] == ""
    assert data["returned_chars"] == 0
    assert data["has_more"] is False
    assert data["next_offset"] is None
    assert data["cache_hit"] is True


def test_fetch_url_does_not_cache_binary(monkeypatch):
    _install(monkeypatch, _resp("", content_type="application/pdf"))

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    data = json.loads(_call(CallableTool(FetchUrl(state=state)), url="https://example.com/a.pdf"))
    assert data["ok"] is True
    assert data["is_binary"] is True
    assert state.shared.fetch_url_cache == {}


def test_fetch_url_marks_unknown_binary_bytes_as_binary(monkeypatch):
    binary_payload = b"\x00\x89\x10\x02\x00"
    _install(monkeypatch, _resp(
            "",
            content_type="",
            content=binary_payload,
        ),
    )
    data = json.loads(_call(with_local_ctx(FetchUrl), url="https://example.com/blob"))
    assert data["ok"] is True
    assert data["is_binary"] is True
    assert data["result_type"] == "binary"


def test_simple_fetch_url_extracts_html_and_supports_pagination(monkeypatch):
    _install(monkeypatch, _resp(
                "<html><body><div>One <b>Two</b></div><p>Three</p></body></html>",
                content_type="text/html",
            ))
    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    tool = CallableTool(SimpleFetchUrl(state=state))
    first = json.loads(_call(tool, url="https://example.com", offset=0, limit=7))
    second = json.loads(_call(tool, url="https://example.com", offset=7, limit=50))

    assert first["ok"] is True
    assert first["result_type"] == "content"
    assert first["cache_hit"] is False
    assert first["next_offset"] == 7
    assert second["ok"] is True
    assert second["cache_hit"] is True
    assert second["next_offset"] is None
    assert second["content"] != ""


def test_simple_fetch_url_policy_blocks_before_request(monkeypatch):
    def fail_session_if_called():
        raise AssertionError("session should not be created for blocked URL")

    def _fail_if_called(request):
        raise AssertionError("политика обязана заблокировать ДО запроса")

    _install(monkeypatch, _fail_if_called)

    data = json.loads(_call(
        with_local_ctx(SimpleFetchUrl, config_dict=_url_policy_config(url_blacklist=["*.internal"])),
            url="https://api.internal/path",
        ))

    assert data["ok"] is False
    assert data["result_type"] == "error"
    assert data["error"]["type"] == "url_blocked_by_policy"
    assert data["error"]["matched_rule"] == "*.internal"


def test_simple_fetch_url_handles_lists_headings_pre_and_tables(monkeypatch):
    html = (
        "<html><body>"
        "<h2>Title</h2>"
        "<ul><li>first</li><li>second</li></ul>"
        "<pre>line1\nline2</pre>"
        "<table><tr><td>a</td><td>b</td></tr></table>"
        "</body></html>"
    )
    _install(monkeypatch, _resp(html, content_type="text/html"),
    )
    data = json.loads(_call(with_local_ctx(SimpleFetchUrl), url="https://example.com"))
    text = data["content"]
    assert data["ok"] is True
    assert "## Title" in text
    assert "- first" in text and "- second" in text
    assert "line1\nline2" in text
    assert "a" in text and "b" in text


def test_simple_fetch_url_does_not_mark_utf8_html_as_binary(monkeypatch):
    html = "<html><body><p>Привет, мир!</p></body></html>"
    _install(monkeypatch, _resp(
                html,
                content_type="text/html; charset=utf-8",
                content=html.encode("utf-8"),
            ))
    data = json.loads(_call(with_local_ctx(SimpleFetchUrl), url="https://example.com"))
    assert data["ok"] is True
    assert data["is_binary"] is False
    assert data["result_type"] == "content"


# --- прокси -------------------------------------------------------------------
#
# Пять тестов ниже приехали с develop, где сеть шла через `requests.get` и прокси
# читались из `kwargs["proxies"]` вызова. У httpx прокси — свойство КЛИЕНТА,
# поэтому наблюдаем за фабрикой `get_async_client`. Проверяемое поведение то же:
# прокси доезжает, пустая строка означает прямой ход, кэш ответов разведён по
# прокси.


def test_fetch_url_passes_proxy_to_client(monkeypatch):
    seen: dict = {}
    _install(monkeypatch, _resp("<html><body><p>hello via proxy</p></body></html>"), seen)
    monkeypatch.setattr(
        fetch_url_module.trafilatura, "extract", lambda *a, **k: "hello via proxy"
    )

    proxy = "socks5h://127.0.0.1:1080"
    data = json.loads(with_local_ctx(FetchUrl)(url="https://example.com", proxy=proxy))

    assert data["ok"] is True
    assert data["content"] == "hello via proxy"
    proxies = [c.get("proxies") for c in seen.get("calls", [])]
    assert any(proxy in str(p) for p in proxies), proxies


def test_fetch_url_empty_proxy_is_direct(monkeypatch):
    seen: dict = {}
    _install(monkeypatch, _resp("<html></html>"), seen)
    monkeypatch.setattr(fetch_url_module.trafilatura, "extract", lambda *a, **k: "ok")

    for value in ("", "   "):
        data = json.loads(
            with_local_ctx(FetchUrl)(url="https://example.com", proxy=value)
        )
        assert data["ok"] is True
    for call in seen.get("calls", []):
        assert not call.get("proxies"), call


def test_fetch_url_cache_keyed_by_proxy(monkeypatch):
    calls = {"n": 0}

    def _handler(_request):
        calls["n"] += 1
        return _resp("<html></html>")

    _install(monkeypatch, _handler)
    monkeypatch.setattr(
        fetch_url_module.trafilatura, "extract", lambda *a, **k: f"body-{calls['n']}"
    )

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    tool = with_local_ctx(FetchUrl, state=state)

    first = json.loads(tool(url="https://example.com"))
    second = json.loads(tool(url="https://example.com", proxy="socks5h://127.0.0.1:1080"))

    # Разный прокси — разный ключ кэша: ответ через прокси может отличаться,
    # и отдавать прямой ответ на запрос через прокси нельзя.
    assert first["ok"] is True and second["ok"] is True
    assert calls["n"] == 2


# --- прокси у simple_fetch_url --------------------------------------------
#
# Два теста ниже вернулись: при переезде на httpx они были УДАЛЕНЫ без замены,
# и прокси у `simple_fetch_url` осталась непокрытой вовсе. Форма изменилась —
# у requests прокси задавалась на запросе, у httpx при создании клиента, —
# поэтому наблюдаем за фабрикой клиента (`seen`), а не за вызовом `.get`.


def test_simple_fetch_url_passes_proxy_to_the_client(monkeypatch):
    """Прокси обязана доехать до клиента, а не потеряться по дороге."""
    seen: dict = {}
    _install(monkeypatch, _resp("<html>hi</html>"), seen)

    proxy = "socks5h://127.0.0.1:1080"
    data = json.loads(_call(with_local_ctx(SimpleFetchUrl), url="https://example.com", proxy=proxy))

    assert data["ok"] is True, data
    proxies = [c.get("proxies") for c in seen.get("calls", [])]
    assert any(proxy in str(p) for p in proxies), proxies


def test_simple_fetch_url_proxy_validation(monkeypatch):
    """Схему прокси проверяем ДО сети: мусор не должен уходить в клиент."""
    seen: dict = {}
    _install(monkeypatch, _resp("<html>hi</html>"), seen)

    data = json.loads(
        _call(with_local_ctx(SimpleFetchUrl), url="https://example.com", proxy="ftp://x")
    )

    assert data["ok"] is False, data
    assert data["error"]["field"] == "proxy", data
    assert seen.get("calls") in (None, []), "запрос ушёл при невалидной прокси"


def test_simple_fetch_url_empty_proxy_is_direct(monkeypatch):
    """Пустая строка — это «без прокси», а не «прокси с пустым адресом»."""
    seen: dict = {}
    _install(monkeypatch, _resp("<html>hi</html>"), seen)

    for value in ("", "   "):
        data = json.loads(
            _call(with_local_ctx(SimpleFetchUrl), url="https://example.com", proxy=value)
        )
        assert data["ok"] is True, data
    for call in seen.get("calls", []):
        assert not call.get("proxies"), call
