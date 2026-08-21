"""Стриминговый путь chat/completions.

Транспорт с Задачи 9 — httpx.AsyncClient (синхронный вход остался шимом через
портал), поэтому подменяется не приватная requests-сессия, а фабрика клиента.
Утверждения о поведении прежние: они и есть гейт переезда транспорта.
"""
import json

import httpx
import pytest

from gc_core.utils import connect_backend


def _install(monkeypatch, handler):
    """Подменить транспорт запросов к модели фиктивным.

    Клиент создаётся на каждый вызов: MockTransport состояния не держит, а
    общий кэш пер-loop клиентов трогать из теста не следует.
    """
    monkeypatch.setattr(
        connect_backend, "get_async_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _sse(*payloads):
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)
    return body + "data: [DONE]\n\n"


_STREAM_BODY = _sse(
    {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"completion_tokens": 1}},
)


def test_streaming_path_used_when_on_delta_given(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["stream"] = payload.get("stream")
        captured["stream_options"] = payload.get("stream_options")
        return httpx.Response(200, text=_STREAM_BODY,
                              headers={"content-type": "text/event-stream"})

    _install(monkeypatch, handler)
    deltas = []
    res = connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
        on_delta=lambda k, t: deltas.append((k, t)),
    )

    assert captured["stream"] is True
    # usage в стриме приходит только по явному запросу — иначе токены основного
    # цикла не учитывались бы вовсе.
    assert captured["stream_options"] == {"include_usage": True}
    assert res["choices"][0]["message"]["content"] == "hi"
    assert res["usage"] == {"completion_tokens": 1}
    assert ("answer", "hi") in deltas


def test_blocking_path_when_no_on_delta(monkeypatch):
    """Без on_delta стрим не задействуется — независимо от cfg['stream']."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content).get("stream") is None
        return httpx.Response(200, json={"choices": [{"message": {"content": "blk"}}]})

    _install(monkeypatch, handler)
    res = connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
    )
    assert res["choices"][0]["message"]["content"] == "blk"


def test_streaming_emits_reset_before_content(monkeypatch):
    """on_delta("reset", "") — самый первый вызов, до любой дельты контента."""
    _install(monkeypatch, lambda request: httpx.Response(
        200, text=_STREAM_BODY, headers={"content-type": "text/event-stream"}))

    deltas = []
    connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
        on_delta=lambda k, t: deltas.append((k, t)),
    )
    assert deltas[0] == ("reset", ""), f"Expected reset first, got: {deltas[0]}"
    assert ("answer", "hi") in deltas


def test_streaming_http_error_reraises(monkeypatch):
    """Ошибка HTTP в стриме пробрасывается, не падая в блоке логов/печати.

    Тело ошибки у стримового ответа недоступно до aread() — на этом легко
    получить вместо HTTP-ошибки невразумительное StreamConsumed.
    """
    _install(monkeypatch, lambda request: httpx.Response(500, text="Internal Server Error"))

    with pytest.raises(httpx.HTTPStatusError):
        connect_backend.request_backend_chat_completions(
            {"model": "m", "messages": []}, {}, url="http://x", verify=True,
            use_retry=False,
            on_delta=lambda k, t: None,
        )


def test_non_stream_http_error_reraises_with_body(monkeypatch, capsys):
    """Не-стримовый 422: причина валидации обязана попасть в вывод."""
    _install(monkeypatch, lambda request: httpx.Response(422, text="messages[0].content"))

    with pytest.raises(httpx.HTTPStatusError):
        connect_backend.request_backend_chat_completions(
            {"model": "m", "messages": [{"role": "user"}]}, {},
            url="http://x", verify=True, use_retry=False,
        )
    assert "messages[0].content" in capsys.readouterr().out


def test_stream_body_is_decoded_as_utf8_regardless_of_declared_charset(monkeypatch):
    """Кодировка тела SSE — всегда UTF-8, что бы ни объявил провайдер.

    Регрессия переезда: ``httpx.aiter_lines()`` декодирует по charset из
    заголовка, а синхронный путь до переезда декодировал UTF-8 принудительно.
    Провайдер, объявивший windows-1251 (или ISO-8859-1) при UTF-8 теле, портил
    ответ агента — без единого покрасневшего теста.
    """
    chunk = {"choices": [{"index": 0,
                          "delta": {"role": "assistant", "content": "Привет, мир"}}]}
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    body = (
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
        "data: [DONE]\n\n"
    ).encode("utf-8")

    def handler(request):
        return httpx.Response(
            200, content=body,
            headers={"content-type": "text/event-stream; charset=windows-1251"},
        )

    _install(monkeypatch, handler)
    res = connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x",
        on_delta=lambda kind, text: None, use_retry=False,
    )

    assert res["choices"][0]["message"]["content"] == "Привет, мир"
