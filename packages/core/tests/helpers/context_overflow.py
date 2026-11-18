"""Переполнение окна модели на запросе и настоящая компакция после него.

Общий для тестов цикла, где ход идёт через ``AgentExecutor``: первые запросы
к модели падают переполнением окна, сводку строит настоящая компакция, у
которой подменён только вызов суммаризатора.
"""

from __future__ import annotations

OVERFLOW = (
    "Requested token count exceeds the model's maximum context "
    "length of 131072 tokens. Please reduce the number of tokens"
)


def overflow_then_summarize(monkeypatch, times: int = 1) -> list:
    """Подменить запрос и суммаризатор: первые ``times`` запросов к модели —
    переполнение окна. → список попыток с переполнением."""
    from gc_core import processing_responses as pr

    real = pr.aprocess_user_ask
    overflowed = []

    async def _overflow(messages, state, ctx):
        if len(overflowed) < times:
            overflowed.append(True)
            raise ValueError(OVERFLOW)
        return await real(messages, state, ctx)

    async def _summarize(**_kwargs):
        return {
            "raw_summary": "<task_and_context>итог</task_and_context>",
            "num_batches": 1,
            "last_dialog_steps": "",
            "trace": {"memory_patch": {"executed": False}},
        }

    monkeypatch.setattr(pr, "aprocess_user_ask", _overflow)
    monkeypatch.setattr(
        "gc_core.summarize.compaction.asummarize_dialog_via_chat_completions",
        _summarize,
    )
    return overflowed
