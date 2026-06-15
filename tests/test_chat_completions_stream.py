from utils.chat_completions_stream import (
    iter_sse_data_lines,
    assemble_streamed_completion,
)

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
    assert all(p != "[DONE]" and p != "" for p in payloads)
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
