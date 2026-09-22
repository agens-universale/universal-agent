from gc_core.utils.chat_completions_stream import assemble_streamed_completion, iter_sse_data_lines

# Realistic OpenAI-compatible stream: reasoning_content, content, a fragmented
# tool call, finish_reason, and usage in the final chunk.
_LINES = [
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"ана"}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"reasoning_content":"лизирую"}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"content":"Это "}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"content":"проект."}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"t1","type":"function","function":{"name":"ping","arguments":"{\\"a\\":"}}]}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"completion_tokens":42}}',
    'data: [DONE]',
    '',
]


def test_iter_sse_data_lines_skips_done_and_blanks():
    payloads = list(iter_sse_data_lines(_LINES))
    assert payloads[0].startswith('{"id":"c1"')
    assert all(p not in {"[DONE]", ""} for p in payloads)
    assert len(payloads) == 7


def test_assemble_reassembles_message_content_and_tool_calls():
    deltas = []
    result = assemble_streamed_completion(
        iter_sse_data_lines(_LINES),
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )
    msg = result["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Это проект."
    assert msg["reasoning_content"] == "анализирую"
    assert msg["tool_calls"] == [
        {"id": "t1", "type": "function",
         "function": {"name": "ping", "arguments": '{"a":1}'}}
    ]
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert result["usage"] == {"completion_tokens": 42}
    assert ("think", "ана") in deltas
    assert ("answer", "Это ") in deltas
    assert [t for k, t in deltas if k == "answer"] == ["Это ", "проект."]


def test_assemble_without_on_delta():
    """assemble_streamed_completion works when on_delta is omitted (default None)."""
    payloads = iter_sse_data_lines([
        'data: {"id":"x","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"hello"}}]}',
        'data: {"id":"x","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ])
    result = assemble_streamed_completion(payloads)
    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["choices"][0]["finish_reason"] == "stop"


# Форма шага, которой в наборе не было вовсе: НИ content, НИ reasoning_content —
# только tool_calls. Это не экзотика, а обычный шаг агента: он почти всегда зовёт
# инструмент, и финальный ответ тоже уезжает в аргумент turn_finish. Пока дельт от
# аргументов не отдавали, счётчик прогресса на таком шаге оставался нулём — при
# живой генерации в сотни токенов (найдено 27.08.2026 разбором жалобы «пропали
# стрелочки с токенами»).
_TOOL_ONLY_LINES = [
    'data: {"id":"c2","model":"m","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
    'data: {"id":"c2","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"t9","type":"function","function":{"name":"turn_finish","arguments":"{\\"task_report\\":\\""}}]}}]}',
    'data: {"id":"c2","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"готово\\"}"}}]}}]}',
    'data: {"id":"c2","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"completion_tokens":952}}',
    'data: [DONE]',
]


def test_tool_only_stream_still_emits_progress_deltas():
    """Шаг без прозы обязан давать дельты: иначе индикатор молчит при живой работе."""
    deltas = []
    result = assemble_streamed_completion(
        iter_sse_data_lines(_TOOL_ONLY_LINES),
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )
    tool_deltas = [t for k, t in deltas if k == "tool"]
    assert tool_deltas, "на шаге с вызовом инструмента не пришло ни одной дельты"
    assert "".join(tool_deltas) == '{"task_report":"готово"}'

    # Вид отдельный, а не "answer": иначе сырой JSON уехал бы в блок потока на экран.
    assert not [t for k, t in deltas if k == "answer"]

    # Сборка ответа при этом не изменилась.
    msg = result["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"task_report":"готово"}'
    assert result["usage"] == {"completion_tokens": 952}


def test_reasoning_alias_is_read_and_not_double_counted():
    """`reasoning` — второе имя того же поля (шлёт OpenRouter).

    Читаем ОДНО из двух: провайдер, приславший оба, иначе удвоил бы и текст
    рассуждения, и счётчик прогресса.
    """
    deltas = []
    result = assemble_streamed_completion(
        iter_sse_data_lines([
            'data: {"choices":[{"index":0,"delta":{"role":"assistant","reasoning":"думаю"}}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        ]),
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )
    assert result["choices"][0]["message"]["reasoning_content"] == "думаю"
    assert [t for k, t in deltas if k == "think"] == ["думаю"]

    both = []
    assemble_streamed_completion(
        iter_sse_data_lines([
            'data: {"choices":[{"index":0,"delta":{"reasoning_content":"канон","reasoning":"дубль"}}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        ]),
        on_delta=lambda kind, text: both.append((kind, text)),
    )
    assert [t for k, t in both if k == "think"] == ["канон"], "оба поля сложились"
