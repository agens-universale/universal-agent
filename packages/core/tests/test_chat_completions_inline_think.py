from gc_core.utils.chat_completions_stream import iter_sse_data_lines, assemble_streamed_completion


def _deltas(lines):
    out = []
    assemble_streamed_completion(iter_sse_data_lines(lines), on_delta=lambda k, t: out.append((k, t)))
    return out


def test_inline_think_split_within_one_chunk():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"<think>думаю тут</think>Ответ."}}]}',
        'data: [DONE]',
    ]
    d = _deltas(lines)
    assert ("think", "думаю тут") in d
    assert ("answer", "Ответ.") in d
    assert all("<think>" not in t and "</think>" not in t for _, t in d)


def test_inline_think_tag_split_across_chunks():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"<thi"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"nk>раз"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"мышляю</thi"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"nk>го"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"тово"}}]}',
        'data: [DONE]',
    ]
    d = _deltas(lines)
    think = "".join(t for k, t in d if k == "think")
    answer = "".join(t for k, t in d if k == "answer")
    assert think == "размышляю"
    assert answer == "готово"


def test_unclosed_think_all_routed_to_think():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"<think>всё ещё думаю"}}]}',
        'data: [DONE]',
    ]
    d = _deltas(lines)
    assert "".join(t for k, t in d if k == "think") == "всё ещё думаю"
    assert "".join(t for k, t in d if k == "answer") == ""


def test_reasoning_content_still_think_and_message_content_verbatim():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"rc"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"<think>x</think>Y"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    out = []
    res = assemble_streamed_completion(iter_sse_data_lines(lines), on_delta=lambda k, t: out.append((k, t)))
    assert ("think", "rc") in out
    assert ("think", "x") in out
    assert ("answer", "Y") in out
    assert res["choices"][0]["message"]["content"] == "<think>x</think>Y"


def test_plain_content_no_think_all_answer():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"Просто "}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"ответ."}}]}',
        'data: [DONE]',
    ]
    d = _deltas(lines)
    assert [(k, t) for k, t in d] == [("answer", "Просто "), ("answer", "ответ.")]
