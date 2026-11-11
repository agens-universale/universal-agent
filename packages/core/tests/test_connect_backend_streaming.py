"""Стриминговый путь chat/completions.

Транспорт с Задачи 9 — httpx.AsyncClient (синхронный вход остался шимом через
портал), поэтому подменяется не приватная requests-сессия, а фабрика клиента.
Утверждения о поведении прежние: они и есть гейт переезда транспорта.
"""

import json

import httpx
import pytest
from gc_core.utils import connect_backend
from gc_testkit import run_coro


def _install(monkeypatch, handler):
    """Подменить транспорт запросов к модели фиктивным.

    Клиент создаётся на каждый вызов: MockTransport состояния не держит, а
    общий кэш пер-loop клиентов трогать из теста не следует.
    """
    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _sse(*payloads):
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)
    return body + "data: [DONE]\n\n"


_STREAM_BODY = _sse(
    {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
    {
        "id": "x",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 1},
    },
)


def test_streaming_path_used_when_on_delta_given(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["stream"] = payload.get("stream")
        captured["stream_options"] = payload.get("stream_options")
        return httpx.Response(
            200,
            text=_STREAM_BODY,
            headers={"content-type": "text/event-stream"},
        )

    _install(monkeypatch, handler)
    deltas = []
    res = run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
            on_delta=lambda k, t: deltas.append((k, t)),
        )
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
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "blk"}}]}
        )

    _install(monkeypatch, handler)
    res = run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
        )
    )
    assert res["choices"][0]["message"]["content"] == "blk"


def test_streaming_emits_reset_before_content(monkeypatch):
    """on_delta("reset", "") — самый первый вызов, до любой дельты контента."""
    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            text=_STREAM_BODY,
            headers={"content-type": "text/event-stream"},
        ),
    )

    deltas = []
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
            on_delta=lambda k, t: deltas.append((k, t)),
        )
    )
    assert deltas[0] == ("reset", ""), (
        f"Expected reset first, got: {deltas[0]}"
    )
    assert ("answer", "hi") in deltas


def test_streaming_http_error_reraises(monkeypatch):
    """Ошибка HTTP в стриме пробрасывается, не падая в блоке логов/печати.

    Тело ошибки у стримового ответа недоступно до aread() — на этом легко
    получить вместо HTTP-ошибки невразумительное StreamConsumed.
    """
    _install(
        monkeypatch,
        lambda request: httpx.Response(500, text="Internal Server Error"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(
            connect_backend.arequest_backend_chat_completions(
                {"model": "m", "messages": []},
                {},
                url="http://x",
                verify=True,
                use_retry=False,
                on_delta=lambda k, t: None,
            )
        )


def test_non_stream_http_error_reraises_with_body(monkeypatch, capsys):
    """Не-стримовый 422: причина валидации обязана попасть в вывод."""
    _install(
        monkeypatch,
        lambda request: httpx.Response(422, text="messages[0].content"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(
            connect_backend.arequest_backend_chat_completions(
                {"model": "m", "messages": [{"role": "user"}]},
                {},
                url="http://x",
                verify=True,
                use_retry=False,
            )
        )
    assert "messages[0].content" in capsys.readouterr().out


def test_stream_body_is_decoded_as_utf8_regardless_of_declared_charset(
    monkeypatch,
):
    """Кодировка тела SSE — всегда UTF-8, что бы ни объявил провайдер.

    Регрессия переезда: ``httpx.aiter_lines()`` декодирует по charset из
    заголовка, а синхронный путь до переезда декодировал UTF-8 принудительно.
    Провайдер, объявивший windows-1251 (или ISO-8859-1) при UTF-8 теле, портил
    ответ агента — без единого покрасневшего теста.
    """
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "Привет, мир"},
            }
        ]
    }
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    body = (
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
        "data: [DONE]\n\n"
    ).encode()

    def handler(_request):
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "text/event-stream; charset=windows-1251"
            },
        )

    _install(monkeypatch, handler)
    res = run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            on_delta=lambda kind, text: None,
            use_retry=False,
        )
    )

    assert res["choices"][0]["message"]["content"] == "Привет, мир"


async def test_stream_stops_at_done_even_if_the_server_holds_the_body_open(
    monkeypatch,
):
    """После `[DONE]` читать больше нечего — ждать чужого закрытия нельзя.

    Сборщик на этом маркере поднимает флаг «стрим кончился», но цикл дочитывал
    тело до конца. Сервер или корп-прокси, не закрывший SSE сразу, держал ход
    до read-timeout (600 с по умолчанию), а таймаут — повод повторить ВЕСЬ
    запрос к модели: показанный ответ стирается и генерируется заново. На
    develop генератор SSE-строк делал на этом маркере `return`, и чтение
    прекращалось.
    """
    import asyncio

    holding = asyncio.Event()  # «сервер» ответил и не закрывает поток

    async def body():
        yield (
            b'data: {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}]}\n\n'
        )
        yield b"data: [DONE]\n\n"
        await holding.wait()  # дальше тишина, но тело не закрыто
        yield b""

    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        ),
    )

    deltas = []
    res = await asyncio.wait_for(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
            on_delta=lambda k, t: deltas.append((k, t)),
        ),
        5,
    )

    assert res["choices"][0]["message"]["content"] == "hi"
    assert ("answer", "hi") in deltas


def test_stream_logs_dropped_chunk(monkeypatch):
    """Чанк, не разобранный как JSON-объект, в сборку не идёт, но и не
    пропадает молча: транспорт пишет его в лог предупреждением."""
    from unittest.mock import MagicMock

    body = (
        'data: {"id": "x", "choices": [{"index": 0, "delta": {"content": "a"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "b"\n\n'
        'data: {"id": "x", "choices": [{"index": 0, "delta": {}, '
        '"finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        ),
    )
    log = MagicMock()
    res = run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
            on_delta=lambda k, t: None,
            logger=log,
        )
    )
    assert res["choices"][0]["message"]["content"] == "a"
    warnings = [c.args[0] % c.args[1:] for c in log.warning.call_args_list]
    assert warnings == [
        "chat/completions stream chunk dropped (not a JSON object): "
        '{"choices": [{"delta": {"content": "b"'
    ]


async def test_dropped_chunk_is_logged_even_if_the_stream_breaks(monkeypatch):
    """Обрыв соединения после неразобранного чанка: именно тогда важно
    видеть, что потерялось у нас, — запись в лог не должна зависеть от
    того, дочитан ли поток."""
    from unittest.mock import MagicMock

    async def body():
        yield b'data: {"choices": [{"delta": {"content": "b"\n\n'
        raise httpx.ReadError("обрыв")

    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        ),
    )
    log = MagicMock()
    with pytest.raises(httpx.ReadError):
        await connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
            on_delta=lambda k, t: None,
            logger=log,
        )
    warnings = [c.args[0] % c.args[1:] for c in log.warning.call_args_list]
    assert (
        "chat/completions stream chunk dropped (not a JSON object): "
        '{"choices": [{"delta": {"content": "b"'
    ) in warnings


def test_dropped_chunks_log_is_capped_with_a_total(monkeypatch):
    """Поток в нечитаемом формате: в лог первые обрывки и итог, сколько
    отброшено всего, — не по предупреждению на каждый чанк."""
    from unittest.mock import MagicMock

    from gc_core.utils.stream_assembler import MAX_DROPPED_PAYLOADS

    extra = 5
    bad = "".join(
        f"data: {{bad {i}\n\n" for i in range(MAX_DROPPED_PAYLOADS + extra)
    )
    body = (
        bad + 'data: {"id": "x", "choices": [{"index": 0, '
        '"delta": {"content": "a"}, "finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        ),
    )
    log = MagicMock()
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
            on_delta=lambda k, t: None,
            logger=log,
        )
    )
    warnings = [c.args[0] % c.args[1:] for c in log.warning.call_args_list]
    assert len(warnings) == MAX_DROPPED_PAYLOADS + 1
    assert warnings[-1] == (
        f"chat/completions stream: {MAX_DROPPED_PAYLOADS + extra} chunks "
        f"dropped in total, {extra} not logged"
    )
