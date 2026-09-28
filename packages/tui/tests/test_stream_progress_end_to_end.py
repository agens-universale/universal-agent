"""Сквозной сторож: SSE от бэкенда → индикатор показывает «↓ N токенов».

Зачем отдельный файл. Цепь питания счётчика собрана из четырёх звеньев —
`assemble_streamed_completion` → `on_delta` → `StreamSink` → `ThinkingIndicator`, —
и до 27.08.2026 она была покрыта ДВУМЯ НЕПЕРЕСЕКАЮЩИМИСЯ половинами:
`packages/core/tests/test_chat_completions_stream.py` проверял сборщик, а
`packages/tui/tests/test_thinking_indicator_tail.py` — форматирование строки, подавая
в индикатор `_FakeSink` с готовым числом. Обе половины были зелёными, а цепь целиком
не работала: сборщик не отдавал дельт на шаге с вызовом инструмента, и счётчик всегда
показывал ноль.

Дыра прожила незамеченной, потому что маскировалась поведением модели: пока та
выдавала свободную прозу в `content`, счётчик оживал на этих шагах. Как только модель
стала уходить сразу в `turn_finish`, прозы не осталось ни на одном шаге — и стрелочки
пропали совсем. Жалоба пользователя 27.08.2026, разбор показал, что дефект был всё
время.

Поэтому здесь звенья соединены по-настоящему: настоящий сборщик, настоящий sink,
настоящий виджет. Фейков нет намеренно — фейк на любом стыке и вернул бы ту же
слепоту.
"""

import json

from gc_core.utils.chat_completions_stream import assemble_streamed_completion
from gc_tui.services.stream_sink import StreamSink
from gc_tui.widgets.thinking_indicator import ThinkingIndicator


def _sse(chunks):
    return (json.dumps(c) for c in chunks)


def _tool_call_step(arguments: str):
    """Настоящая форма шага агента: ни content, ни reasoning — только tool_calls."""
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "t1",
                                "type": "function",
                                "function": {
                                    "name": "turn_finish",
                                    "arguments": arguments,
                                },
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
            ],
            "usage": {"completion_tokens": 952},
        },
    ]


def _indicator_text(sink) -> str:
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 5.0
    )
    ind._sink = sink
    return ind._render_text().plain


def test_indicator_counts_tokens_on_a_tool_call_step():
    """Шаг без прозы — счётчик обязан идти. Ровно тот случай, что был сломан."""
    sink = StreamSink()
    sink.push("reset", "")
    payload = '{"task_report":"' + "и" * 400 + '"}'

    assemble_streamed_completion(
        _sse(_tool_call_step(payload)), on_delta=sink.push
    )

    assert sink.progress_units() > 0, "индикатор молчит при живой генерации"
    assert "токенов" in _indicator_text(sink)


def test_tool_arguments_never_reach_the_stream_block():
    """Аргументы идут в счётчик, но не в текст: иначе на экран полезет сырой JSON."""
    sink = StreamSink()
    sink.push("reset", "")

    assemble_streamed_completion(
        _sse(_tool_call_step('{"task_report":"готово"}')), on_delta=sink.push
    )

    assert sink.progress_units() > 0
    assert sink.has_content() is False
    assert sink.snapshot() == ("", "")


def test_tool_call_closes_the_reasoning_phase():
    """Рассуждение → вызов инструмента: фаза обязана закрыться.

    Раньше её закрывала только ветка ответа, поэтому шаг «подумал и позвал тул»
    навсегда оставался в состоянии "thinking" — время рассуждения не считалось.
    """
    sink = StreamSink()
    sink.push("reset", "")
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "прикидываю",
                    },
                }
            ]
        },
    ] + _tool_call_step('{"task_report":"ок"}')[1:]

    assemble_streamed_completion(_sse(chunks), on_delta=sink.push)

    state, duration = sink.reasoning_state()
    assert state == "done", (
        "фаза рассуждения не закрылась на вызове инструмента"
    )
    assert duration is not None


def test_openrouter_reasoning_name_feeds_the_counter():
    """OpenRouter шлёт рассуждение как `reasoning`, а не `reasoning_content`.

    Без чтения второго имени на reasoning-моделях терялись и текст рассуждения, и
    его вклад в счётчик.
    """
    sink = StreamSink()
    sink.push("reset", "")

    assemble_streamed_completion(
        _sse(
            [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "reasoning": "р" * 200,
                            },
                        }
                    ]
                },
                {
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ]
                },
            ]
        ),
        on_delta=sink.push,
    )

    assert sink.progress_units() > 0
    assert "токенов" in _indicator_text(sink)
