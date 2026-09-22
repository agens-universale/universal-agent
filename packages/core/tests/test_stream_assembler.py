"""StreamAssembler: push-парсер SSE (план async-переезда, Задача 6).

Прежняя сборка была pull-циклом и из асинхронного кода не звалась. Здесь
проверяется, что состояние вынесено корректно: кормить можно построчно
(как это делает транспорт) и чанки могут приходить как угодно нарезанными.
"""

import json

from gc_core.utils.chat_completions_stream import assemble_streamed_completion
from gc_core.utils.stream_assembler import StreamAssembler


def _lines(*payloads):
    return [f"data: {json.dumps(p, ensure_ascii=False)}" for p in payloads] + ["data: [DONE]"]


def _delta(content=None, **extra):
    d = dict(extra)
    if content is not None:
        d["content"] = content
    return {"id": "x", "model": "m", "choices": [{"index": 0, "delta": d}]}


def test_assembles_content_and_usage_from_lines():
    deltas = []
    asm = StreamAssembler(on_delta=lambda k, t: deltas.append((k, t)))
    for line in _lines(
        _delta("при"),
        _delta("вет"),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}},
    ):
        asm.feed_line(line)
    asm.flush()

    res = asm.result()
    assert res["choices"][0]["message"]["content"] == "привет"
    assert res["choices"][0]["finish_reason"] == "stop"
    assert res["usage"] == {"completion_tokens": 2}
    assert ("answer", "при") in deltas


def test_accepts_bytes_lines():
    """Транспорт requests отдаёт bytes, httpx — str: обе формы обязаны работать."""
    asm = StreamAssembler()
    for line in _lines(_delta("b")):
        asm.feed_line(line.encode("utf-8"))
    asm.flush()
    assert asm.result()["choices"][0]["message"]["content"] == "b"


def test_ignores_lines_after_done():
    """Источник может дослать хвост после [DONE] — он не должен попасть в сборку."""
    asm = StreamAssembler()
    asm.feed_line("data: [DONE]")
    asm.feed_line(json.dumps(_delta("поздно")))
    assert asm.done is True
    assert asm.result()["choices"][0]["message"]["content"] == ""


def test_think_tag_split_across_chunks():
    """`<think>` разорван между чанками — тег не должен утечь в ответ."""
    deltas = []
    asm = StreamAssembler(on_delta=lambda k, t: deltas.append((k, t)))
    for piece in ("<thi", "nk>рассуждение</thi", "nk>ответ"):
        asm.feed_payload(json.dumps(_delta(piece)))
    asm.flush()

    kinds = {}
    for kind, text in deltas:
        kinds.setdefault(kind, "")
        kinds[kind] += text
    assert kinds.get("think") == "рассуждение"
    assert kinds.get("answer") == "ответ"
    # content сохраняется дословно, вместе с тегами: их снимает downstream
    assert asm.result()["choices"][0]["message"]["content"] == "<think>рассуждение</think>ответ"


def test_tool_calls_are_joined_by_index():
    asm = StreamAssembler()
    asm.feed_payload(
        json.dumps(
            _delta(
                tool_calls=[
                    {"index": 0, "id": "c1", "type": "function", "function": {"name": "calc", "arguments": '{"a"'}}
                ]
            )
        )
    )
    asm.feed_payload(json.dumps(_delta(tool_calls=[{"index": 0, "function": {"arguments": ": 1}"}}])))
    asm.flush()

    calls = asm.result()["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == "c1"
    assert calls[0]["function"] == {"name": "calc", "arguments": '{"a": 1}'}


def test_facade_matches_assembler():
    """Старый вход assemble_streamed_completion даёт то же самое.

    Это и есть гейт рефакторинга: у него 20+ вызывающих и свой набор тестов.
    """
    payloads = [json.dumps(_delta("раз")), json.dumps(_delta("два"))]

    asm = StreamAssembler()
    for p in payloads:
        asm.feed_payload(p)
    asm.flush()

    assert assemble_streamed_completion(payloads) == asm.result()
