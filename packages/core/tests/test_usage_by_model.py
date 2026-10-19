"""Расход по моделям вызова: под автовыбором один ход идёт на двух моделях, и
замеру нужен расход каждой — без разбора debug-лога по ответам.

Пейлоады usage — настоящие ответы vLLM со смоука two_level 2026-09-22
(пара с ecs3: слабая Qwen3.6-35B, сильная DeepSeek-V4-Flash)."""

import gc_core.utils.connect_backend as cb
from gc_core.usage.account import (
    CATEGORY_MAIN,
    CATEGORY_SUBAGENT,
    CATEGORY_SUMMARY,
    UsageAccount,
)
from gc_core.usage.context import record_usage, usage_scope
from gc_core.usage.report import build_run_usage_dict, build_run_usage_report

from transport_fakes import (
    install_transport,
    json_response,
    sse_body,
    stream_response,
)
from gc_testkit import run_coro

WEAK = "Qwen3.6-35B"
STRONG = "dsv4flash-ctx327k-bb"
WEAK_USAGE = {
    "prompt_tokens": 26458,
    "total_tokens": 26623,
    "completion_tokens": 165,
}
STRONG_USAGE = {
    "prompt_tokens": 20645,
    "total_tokens": 20807,
    "completion_tokens": 162,
}


# ---------------- накопитель ----------------


def test_add_with_model_counts_tokens_per_model():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 26458, 165, model=WEAK)
    acc.add(CATEGORY_MAIN, 20645, 162, model=STRONG)
    acc.add(CATEGORY_SUMMARY, 1000, 10, model=STRONG)

    assert acc.by_model[WEAK]["input"] == 26458
    assert acc.by_model[WEAK]["output"] == 165
    assert acc.by_model[WEAK]["calls"] == 1
    assert acc.by_model[STRONG]["input"] == 21645
    assert acc.by_model[STRONG]["calls"] == 2
    # Категории — прежние, модель — отдельное измерение, итог не удваивается.
    assert acc.by_category[CATEGORY_MAIN]["input"] == 47103
    assert acc.totals()["input"] == 48103


def test_add_without_model_leaves_models_empty():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    assert acc.by_model == {}


def test_missing_usage_is_counted_per_model():
    acc = UsageAccount()
    acc.add_missing(CATEGORY_MAIN, model=WEAK)
    assert acc.by_model[WEAK]["calls"] == 1
    assert acc.by_model[WEAK]["missing_usage_calls"] == 1
    assert acc.by_model[WEAK]["input"] == 0


def test_subagent_merge_keeps_model_of_child_calls():
    """Вклад ребёнка сворачивается в категорию subagent, но модели остаются
    своими: токены ушли в реальную модель, и её расход должен это показать."""
    parent = UsageAccount()
    parent.add(CATEGORY_MAIN, 1000, 10, model=STRONG)
    child = UsageAccount()
    child.add(CATEGORY_MAIN, 500, 5, model=WEAK)

    parent.merge(child, as_category=CATEGORY_SUBAGENT)

    assert parent.by_category[CATEGORY_SUBAGENT]["input"] == 500
    assert parent.by_model[WEAK]["input"] == 500
    assert parent.by_model[STRONG]["input"] == 1000


def test_delta_from_isolates_models_of_this_run():
    cumulative = UsageAccount()
    cumulative.add(CATEGORY_MAIN, 100, 20, model=STRONG)  # прошлый прогон
    baseline = UsageAccount.from_meta(cumulative.to_meta())
    cumulative.add(CATEGORY_MAIN, 50, 10, model=WEAK)  # этот прогон

    run = cumulative.delta_from(baseline)

    assert run.by_model == {WEAK: run.by_model[WEAK]}
    assert run.by_model[WEAK]["input"] == 50
    assert run.by_model[WEAK]["calls"] == 1


def test_meta_roundtrip_keeps_models():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 26458, 165, model=WEAK)
    restored = UsageAccount.from_meta(acc.to_meta())
    assert restored.by_model[WEAK]["input"] == 26458
    assert restored.by_model[WEAK]["calls"] == 1


def test_meta_without_models_loads_as_empty():
    """meta.json тредов, записанный до разбивки по моделям, читается как раньше."""
    old = {"by_category": {"main": {"input": 10, "output": 2, "calls": 1}}}
    restored = UsageAccount.from_meta(old)
    assert restored.by_model == {}
    assert restored.by_category["main"]["input"] == 10


# ---------------- захват в транспорте ----------------


def test_record_usage_writes_model_of_the_call():
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_MAIN, "chat_completions"):
        record_usage(
            WEAK_USAGE, "http://127.0.0.1:6002/v1/chat/completions", model=WEAK
        )
    assert acc.by_model[WEAK]["input"] == 26458


def test_chat_completions_records_model_from_request(monkeypatch):
    install_transport(
        monkeypatch,
        json_response(
            {
                "model": STRONG,
                "choices": [{"message": {"content": "ok"}}],
                "usage": STRONG_USAGE,
            }
        ),
    )
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_MAIN, "chat_completions"):
        run_coro(
            cb.arequest_backend_chat_completions(
                {"model": STRONG, "messages": []},
                {},
                url="http://127.0.0.1:6003/v1/chat/completions",
                use_retry=False,
            )
        )
    assert acc.by_model[STRONG]["input"] == 20645
    assert acc.by_model[STRONG]["output"] == 162


def test_streaming_records_model_from_request(monkeypatch):
    """Основной цикл в TUI идёт потоком — модель должна доехать и сюда."""
    body = sse_body(
        {
            "id": "x",
            "model": WEAK,
            "choices": [{"index": 0, "delta": {"content": "hi"}}],
        },
        {"id": "x", "model": WEAK, "choices": [], "usage": WEAK_USAGE},
    )
    install_transport(monkeypatch, stream_response(body))
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_MAIN, "chat_completions"):
        run_coro(
            cb.arequest_backend_chat_completions(
                {"model": WEAK, "messages": []},
                {},
                url="http://127.0.0.1:6002/v1/chat/completions",
                on_delta=lambda k, t: None,
            )
        )
    assert acc.by_model[WEAK]["input"] == 26458
    assert acc.by_model[WEAK]["calls"] == 1


# ---------------- отчёт прогона ----------------


def _two_model_run() -> UsageAccount:
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 26458, 165, model=WEAK)
    acc.add(CATEGORY_MAIN, 20645, 162, model=STRONG)
    return acc


def test_run_usage_dict_has_models_section():
    report = build_run_usage_dict(
        _two_model_run(), {"enabled": False}, "<ВПИШИТЕ_МОДЕЛЬ>"
    )
    assert report["models"] == {
        WEAK: {"input": 26458, "output": 165, "calls": 1},
        STRONG: {"input": 20645, "output": 162, "calls": 1},
    }


def test_run_usage_text_names_called_models_not_config_placeholder():
    """Заголовок называет модели, которые реально вызывались: верхнее поле
    `model` при реестре моделей для вызовов не читается и врёт."""
    text = build_run_usage_report(
        _two_model_run(), {"enabled": False}, "<ВПИШИТЕ_МОДЕЛЬ>"
    )
    header = text.splitlines()[0]
    assert "<ВПИШИТЕ_МОДЕЛЬ>" not in header
    assert WEAK in header and STRONG in header


def test_run_usage_text_lists_models_when_more_than_one():
    lines = build_run_usage_report(
        _two_model_run(), {"enabled": False}, None
    ).splitlines()
    assert "by model:" in lines
    rows = lines[lines.index("by model:") + 1 :]
    assert rows[0].split() == [WEAK, "26.5k", "165"]
    assert rows[1].split() == [STRONG, "20.6k", "162"]


def test_run_usage_text_single_model_has_no_model_section():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20, model=WEAK)
    text = build_run_usage_report(acc, {"enabled": False}, None)
    assert "by model:" not in text
    assert WEAK in text.splitlines()[0]
