"""SSE-сборщик не должен глотать error-события провайдера.

gigacode/vLLM может вернуть HTTP 200 + SSE-событие ошибки (`data: {"error": …}`)
вместо HTTP-статуса. Раньше такой чанк (без `choices`) молча пропускался →
сборка пустая → downstream «Unknown finish reason» (маскировка реальной причины).
Теперь error-событие поднимается с сообщением провайдера."""
import pytest
from gc_core.utils.chat_completions_stream import BackendStreamError, assemble_streamed_completion


def test_sse_error_event_is_surfaced():
    payloads = [
        '{"error": {"message": "prompt blocked by content filter", "code": "content_filter"}}'
    ]
    with pytest.raises(BackendStreamError) as ei:
        assemble_streamed_completion(payloads)
    msg = str(ei.value)
    assert "Backend stream error" in msg
    assert "content filter" in msg          # реальная причина провайдера видна
    assert ei.value.status_code == "content_filter"   # код из error-события несётся


def test_normal_content_stream_still_assembles():
    payloads = [
        '{"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":"stop"}]}',
    ]
    res = assemble_streamed_completion(payloads)
    assert res["choices"][0]["message"]["content"] == "Hello"
    assert res["choices"][0]["finish_reason"] == "stop"


def test_genuinely_empty_stream_unchanged():
    # Нет error, нет данных → пустая сборка с finish_reason=None (это территория #2,
    # #1 её не трогает — только error-события).
    res = assemble_streamed_completion([])
    assert res["choices"][0]["message"]["content"] == ""
    assert res["choices"][0]["finish_reason"] is None


def test_choice_level_error_is_surfaced():
    payloads = [
        '{"choices": [{"index": 0, "error": {"message": "safety block", "code": 400}}]}'
    ]
    with pytest.raises(BackendStreamError) as ei:
        assemble_streamed_completion(payloads)
    assert "safety block" in str(ei.value)
    assert ei.value.status_code == 400
