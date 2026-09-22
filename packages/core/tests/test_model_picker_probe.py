"""probe-расширение model_picker: статусы, 401-приоритет, POST-пинг, latency."""
from gc_core import model_picker as mp


def _ok_get(payload):
    def get_ex(url, headers, verify):
        return mp.HttpReply(status=200, payload=payload, network_error="")
    return get_ex


def _err_get(status=None, network="timeout"):
    def get_ex(url, headers, verify):
        return mp.HttpReply(status=status, payload=None,
                            network_error="" if status else network)
    return get_ex


def test_probe_fetch_success_returns_models_and_latency():
    payload = {"data": [{"id": "m1"}]}
    out = mp.probe_fetch("http://x/v1/chat/completions", None, http_get_ex=_ok_get(payload))
    assert out.models and out.models[0].id == "m1"
    assert out.status == 200
    assert out.latency_ms >= 0


def test_probe_fetch_401_wins_over_404():
    calls = []

    def get_ex(url, headers, verify):
        calls.append(url)
        if url.endswith("/config"):
            return mp.HttpReply(status=401, payload=None, network_error="")
        return mp.HttpReply(status=404, payload=None, network_error="")

    out = mp.probe_fetch("http://x/v1/chat/completions", "Bearer t", http_get_ex=get_ex)
    assert out.models is None
    assert out.status == 401                      # 401 приоритетнее прочих кодов


def test_probe_fetch_pure_network_error():
    out = mp.probe_fetch("http://x/v1/chat/completions", None,
                         http_get_ex=_err_get(status=None, network="timeout"))
    assert out.models is None and out.status is None
    assert "timeout" in out.network_error


def test_probe_fetch_404_on_both_reports_status():
    out = mp.probe_fetch("http://x/v1/chat/completions", None,
                         http_get_ex=_err_get(status=404))
    assert out.models is None and out.status == 404


def test_probe_post_status_and_body():
    seen = {}

    def post_ex(url, headers, body, verify):
        seen["url"], seen["body"] = url, body
        return mp.HttpReply(status=200, payload={"ok": True}, network_error="")

    status, err, lat, payload = mp.probe_post("http://x/v1/chat/completions", "Bearer t", "m1",
                                              http_post_ex=post_ex)
    assert status == 200 and err == "" and lat >= 0
    # Отдаём само тело, а не флаг «оно было JSON»: по нему проба отличает ответ на
    # наш токен от ответа на заведомо мусорный, когда статус у обоих одинаковый.
    assert payload == {"ok": True}
    assert seen["url"] == "http://x/v1/chat/completions"
    assert seen["body"]["max_tokens"] == 1 and seen["body"]["model"] == "m1"


def test_probe_post_non_json_body_flagged():
    """HTML/текстовое тело при живом статусе → payload=None. По нему проба
    различает «ответило приложение» и «404-страница гейтвея/Express»."""
    def post_ex(url, headers, body, verify):
        return mp.HttpReply(status=404, payload=None, network_error="")

    status, err, _, payload = mp.probe_post("http://x/v1/chat/completions", None, "m1",
                                            http_post_ex=post_ex)
    assert status == 404 and payload is None


def test_probe_post_network_error():
    def post_ex(url, headers, body, verify):
        return mp.HttpReply(status=None, payload=None, network_error="connection refused")

    status, err, _, payload = mp.probe_post("http://x/v1/chat/completions", None, "m1",
                                            http_post_ex=post_ex)
    assert status is None and "connection" in err and payload is None


def test_get_ex_broken_body_keeps_status(monkeypatch):
    """_urllib_get_ex: битое тело (не JSON) → status сохраняется, payload=None."""
    import urllib.request

    class FakeResponse:
        def __init__(self):
            self.status = 200
        def read(self):
            return b"not a json"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout, context):
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reply = mp._urllib_get_ex("http://x", {}, verify=True)
    assert reply.status == 200
    assert reply.payload is None
    assert reply.network_error == ""


def test_probe_fetch_status_kept_when_body_broken():
    """probe_fetch: битое тело на обоих путях → финальный статус = последний, не None."""
    def get_ex_broken(url, headers, verify):
        # Оба пути возвращают 200 со статусом, но payload=None (битое тело)
        return mp.HttpReply(status=200, payload=None, network_error="")

    out = mp.probe_fetch("http://x/v1/chat/completions", None, http_get_ex=get_ex_broken)
    assert out.models is None
    assert out.status == 200  # статус сохранён, не None
    assert out.network_error == ""
