import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gc_core import model_picker as mp


@pytest.fixture
def local_http_server():
    """Настоящий доступный HTTP-сервер на свободном порту → базовый URL.

    Нужен там, где проверяется поведение транспорта ДО ответа сервера (кодирование
    заголовков): на закрытом порту соединение отваливается раньше, и тест зеленеет
    по неверной причине."""
    class _H(BaseHTTPRequestHandler):
        def _reply(self):
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_parse_config_models():
    payload = {"chat": {"models": [
        {"model": "vllm/Qwen3-Coder-Next-262k", "description": "Qwen3-Coder",
         "plugin_side": {"supports_parallel_tool_calls": True, "context_window_size": 262144}},
        {"model": "CodeChat", "description": "CodeChat(Qwen3-32B)",
         "plugin_side": {"supports_parallel_tool_calls": False, "context_window_size": 131072}},
    ]}}
    out = mp.parse_config_models(payload)
    assert [m.id for m in out] == ["vllm/Qwen3-Coder-Next-262k", "CodeChat"]
    assert out[0].tool_capable is True and out[0].context == 262144
    assert out[1].tool_capable is False


def test_parse_config_models_empty():
    assert mp.parse_config_models({}) == []
    assert mp.parse_config_models({"chat": {}}) == []


def test_parse_models_list_filters_chat():
    payload = {"object": "list", "data": [
        {"id": "CodeChat", "endpoints": ["/chat/completions", "/responses"]},
        {"id": "Gigacode-Inline-13B", "endpoints": ["/completions"]},
        {"id": "no-endpoints-field"},
    ]}
    out = mp.parse_models_list(payload)
    ids = [m.id for m in out]
    assert "CodeChat" in ids
    assert "Gigacode-Inline-13B" not in ids       # только /completions
    assert "no-endpoints-field" in ids            # нет поля endpoints → не фильтруем
    assert all(m.tool_capable is None for m in out)


def test_api_base():
    assert mp.api_base("https://h/api/v2/chat/completions") == "https://h/api/v2"
    assert mp.api_base("https://h/v1/chat/completions") == "https://h/v1"


def test_fetch_prefers_config_then_models_then_none():
    calls = []
    def get_config_ok(url, headers, verify):
        calls.append(url)
        if url.endswith("/config"):
            return {"chat": {"models": [{"model": "M1", "plugin_side": {"supports_parallel_tool_calls": True}}]}}
        raise RuntimeError("no")
    got = mp.fetch_models("https://h/api/v2/chat/completions", "Bearer x", http_get=get_config_ok)
    assert [m.id for m in got] == ["M1"]

    def get_only_models(url, headers, verify):
        if url.endswith("/config"):
            raise RuntimeError("404")
        return {"data": [{"id": "M2", "endpoints": ["/chat/completions"]}]}
    got = mp.fetch_models("https://h/api/v2/chat/completions", None, http_get=get_only_models)
    assert [m.id for m in got] == ["M2"]

    def get_all_fail(url, headers, verify):
        raise RuntimeError("down")
    assert mp.fetch_models("https://h/api/v2/chat/completions", None, http_get=get_all_fail) is None


def test_probe_fetch_dedups_identical_network_errors(monkeypatch):
    err = "<urlopen error [WinError 10061] отказано>"
    monkeypatch.setattr(mp, "_requests_get_ex",
                        lambda url, headers, verify, *, config_proxy=None: mp.HttpReply(
                            status=None, payload=None, network_error=err))
    out = mp.probe_fetch("http://x/v1", None, verify=False)
    assert out.status is None
    assert out.network_error.count(err) == 1        # дедуп: не дважды


class _FakeResp:
    status = 200

    def read(self):
        return b'{"data": []}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_user_agents(monkeypatch) -> list:
    seen = []

    def _urlopen(req, timeout=None, context=None):
        seen.append(req.get_header("User-agent"))
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return seen


def test_requests_introduce_themselves_by_product_name():
    """Каждый исходящий запрос несёт визитку продукта. Без неё библиотека шлёт свой
    дефолт («python-requests/X.Y», «Python-urllib/X.Y»), и щит провайдера (Cloudflare
    у Groq — проверено живьём) отвечает 403 ещё ДО проверки токена: список моделей
    молчал бы на рабочем ключе.

    Спрашиваем сами точки входа, а не транспорт: заголовки собирает `_headers`, и
    подменённый транспорт видит ровно тот набор, что ушёл бы в сеть. Тест на самом
    транспорте проверял бы функцию, которую в бою никто не зовёт."""
    from gc_core import app_version
    seen = []

    def get_ex(url, headers, verify):
        seen.append(headers.get("User-Agent"))
        return mp.HttpReply(status=200, payload={})

    def post_ex(url, headers, body, verify):
        seen.append(headers.get("User-Agent"))
        return mp.HttpReply(status=200, payload={})

    def get(url, headers, verify):
        seen.append(headers.get("User-Agent"))
        return {}

    mp.probe_fetch("https://h/v1/chat/completions", "Bearer t", http_get_ex=get_ex)
    mp.probe_post("https://h/v1/chat/completions", "Bearer t", "m", http_post_ex=post_ex)
    mp.fetch_models("https://h/v1/chat/completions", "Bearer t", http_get=get)

    expected = f"{app_version.DIST_NAME}/{app_version.agent_version()}"
    assert seen and set(seen) == {expected}
    assert expected.startswith("gc-ua/")


def test_the_legacy_urllib_transport_introduces_itself_too(monkeypatch):
    """`_urllib_get_ex` — легаси-транспорт без прокси: в бою его не зовут, но он
    остаётся инъектируемой заменой для `probe_fetch`, и визитку ставит своим путём
    (`_request`), мимо `_headers`. Отвалится — отвалится молча."""
    from gc_core import app_version
    seen = _capture_user_agents(monkeypatch)

    mp._urllib_get_ex("https://h/v1/models", {}, True)

    expected = f"{app_version.DIST_NAME}/{app_version.agent_version()}"
    assert seen == [expected]
    assert "urllib" not in seen[0]


def test_the_user_agent_carries_the_live_version(monkeypatch):
    """Имя и версия — из метаданных пакета, не из строки в коде: подняли версию в
    pyproject — визитка поехала сама."""
    from gc_core import app_version
    monkeypatch.setattr(app_version, "agent_version", lambda: "9.9.9")
    mp._user_agent.cache_clear()
    try:
        seen = _capture_user_agents(monkeypatch)
        mp._urllib_get_ex("https://h/v1/models", {}, True)
        assert seen == ["gc-ua/9.9.9"]
    finally:
        mp._user_agent.cache_clear()


def test_transport_survives_non_latin1_header(local_http_server):
    """Кириллица в токене — ошибка ДАННЫХ, а не сбой процесса: заголовки HTTP
    кодируются в latin-1, и http.client бросает UnicodeEncodeError. Это ValueError,
    а не RequestException, поэтому мимо перехвата он рвёт вызывающий код и доезжает
    до юзера как «сервер недоступен» — при том, что сервера никто не спрашивал.

    Сервер здесь настоящий и доступный: на закрытом порту соединение отвалилось бы
    РАНЬШЕ кодирования заголовка, и тест был бы зелёным даже без фикса."""
    url = f"{local_http_server}/v1/chat/completions"
    ok = mp._requests_post_ex(url, mp._headers("Bearer sk-latin"), {"model": "m"}, True)
    assert ok.status == 200          # контроль: до сервера доезжаем

    reply = mp._requests_post_ex(url, mp._headers("Bearer уквxxx"), {"model": "m"}, True)
    assert reply.status is None
    assert reply.network_error
