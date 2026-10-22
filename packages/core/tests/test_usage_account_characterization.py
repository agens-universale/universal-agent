"""Характеризация учёта расхода: точный формат ``to_meta`` после чтения, слияния и
разности счётов и точный структурный отчёт прогона.

Meta персистится в треде и снимках Temporal, поэтому сравнение идёт по
сериализованной строке — порядок ключей тоже часть формата."""

import json

from gc_core.usage.account import UsageAccount
from gc_core.usage.report import build_run_usage_dict

_FIELDS = (
    "input",
    "output",
    "cached",
    "calls",
    "missing_usage_calls",
    "billed_calls",
    "billed_input",
    "billed_output",
    "billed_cached",
)
_MODEL_FIELDS = _FIELDS[:5]


def _fields(**values):
    return {field: values.get(field, 0) for field in _FIELDS}


def _bucket(money, by_model=None, **values):
    bucket = _fields(**values)
    bucket["cost_by_currency"] = money
    if by_model is not None:
        bucket["by_model"] = by_model
    return bucket


def _model(**values):
    return {field: values.get(field, 0) for field in _MODEL_FIELDS}


# Сырой meta с мусором: дробные и нечисловые значения, не-словари, legacy-цена,
# нестроковая категория.
RAW = {
    "by_category": {
        "main": {
            "input": 1000.9,
            "output": 50,
            "cached": "x",
            "calls": 3,
            "missing_usage_calls": 1,
            "billed_calls": 1,
            "billed_input": 400,
            "billed_output": 20,
            "billed_cached": 0,
            "cost_by_currency": {"USD": 0.5, "RUB": 3},
            "by_model": {
                "fast": {
                    "input": 600,
                    "output": 30,
                    "calls": 2,
                    "billed_calls": 1,
                    "billed_input": 400.7,
                },
                "bad": "not a dict",
            },
        },
        "summary": {"input": 10, "output": 2, "calls": 1, "cost": 0.25},
        "broken": [1, 2],
        7: {"input": 5, "calls": 1},
    },
    "by_model": {
        "fast": {
            "input": 600,
            "output": "?",
            "calls": 2.0,
            "missing_usage_calls": 1,
        },
        "junk": None,
    },
}

RAW_MAIN = _bucket(
    {"USD": 0.5, "RUB": 3.0},
    {
        "fast": _fields(
            input=600, output=30, calls=2, billed_calls=1, billed_input=400
        )
    },
    input=1000,
    output=50,
    calls=3,
    missing_usage_calls=1,
    billed_calls=1,
    billed_input=400,
    billed_output=20,
)
RAW_SUMMARY = _bucket({"": 0.25}, input=10, output=2, calls=1)
RAW_SEVEN = _bucket({}, input=5, calls=1)

CHILD_BIG = _fields(
    input=100,
    output=10,
    cached=5,
    calls=2,
    missing_usage_calls=1,
    billed_calls=1,
    billed_input=100,
    billed_output=10,
    billed_cached=5,
)
CHILD_FAST = _fields(input=20, output=1, calls=1)
MODELS_AFTER_CHILD = {
    "fast": _model(input=620, output=1, calls=3, missing_usage_calls=1),
    "big": _model(
        input=100, output=10, cached=5, calls=2, missing_usage_calls=1
    ),
}


def _child():
    child = UsageAccount()
    child.add(
        "main",
        100,
        10,
        cached_tokens=5,
        model="big",
        billed=True,
        cost=0.1,
        currency="USD",
    )
    child.add("summary", 20, 1, model="fast")
    child.add_missing("main", model="big")
    return child


def _dump(meta):
    return json.dumps(meta)


def test_from_meta_normalizes_raw_meta_exactly():
    expected = {
        "by_category": {
            "main": RAW_MAIN,
            "summary": RAW_SUMMARY,
            "7": RAW_SEVEN,
        },
        "by_model": {
            "fast": _model(input=600, calls=2, missing_usage_calls=1)
        },
    }
    assert _dump(UsageAccount.from_meta(RAW).to_meta()) == _dump(expected)


def test_merge_as_category_folds_child_into_one_bucket():
    parent = UsageAccount.from_meta(RAW)
    parent.merge(_child(), as_category="subagent")
    expected = {
        "by_category": {
            "main": RAW_MAIN,
            "summary": RAW_SUMMARY,
            "7": RAW_SEVEN,
            "subagent": _bucket(
                {"USD": 0.1},
                {"big": CHILD_BIG, "fast": CHILD_FAST},
                input=120,
                output=11,
                cached=5,
                calls=3,
                missing_usage_calls=1,
                billed_calls=1,
                billed_input=100,
                billed_output=10,
                billed_cached=5,
            ),
        },
        "by_model": MODELS_AFTER_CHILD,
    }
    assert _dump(parent.to_meta()) == _dump(expected)


MERGED_MAIN = _bucket(
    {"USD": 0.6, "RUB": 3.0},
    {"fast": RAW_MAIN["by_model"]["fast"], "big": CHILD_BIG},
    input=1100,
    output=60,
    cached=5,
    calls=5,
    missing_usage_calls=2,
    billed_calls=2,
    billed_input=500,
    billed_output=30,
    billed_cached=5,
)
MERGED_SUMMARY = _bucket(
    {"": 0.25}, {"fast": CHILD_FAST}, input=30, output=3, calls=2
)


def test_merge_keeps_child_categories():
    parent = UsageAccount.from_meta(RAW)
    parent.merge(_child())
    expected = {
        "by_category": {
            "main": MERGED_MAIN,
            "summary": MERGED_SUMMARY,
            "7": RAW_SEVEN,
        },
        "by_model": MODELS_AFTER_CHILD,
    }
    assert _dump(parent.to_meta()) == _dump(expected)


def test_merge_ignores_non_account():
    parent = UsageAccount.from_meta(RAW)
    parent.merge(RAW)
    assert _dump(parent.to_meta()) == _dump(
        UsageAccount.from_meta(RAW).to_meta()
    )


def _merged():
    account = UsageAccount.from_meta(RAW)
    account.merge(_child())
    return account


def test_delta_from_snapshot_keeps_only_this_run():
    delta = _merged().delta_from(UsageAccount.from_meta(RAW))
    expected = {
        "by_category": {
            "main": _bucket(
                # Float-хвост вычитания выше эпсилона остаётся как есть.
                {"USD": (0.5 + 0.1) - 0.5},
                {"big": CHILD_BIG},
                input=100,
                output=10,
                cached=5,
                calls=2,
                missing_usage_calls=1,
                billed_calls=1,
                billed_input=100,
                billed_output=10,
                billed_cached=5,
            ),
            "summary": _bucket(
                {}, {"fast": CHILD_FAST}, input=20, output=1, calls=1
            ),
        },
        "by_model": {
            "fast": _model(input=20, output=1, calls=1),
            "big": _model(
                input=100, output=10, cached=5, calls=2, missing_usage_calls=1
            ),
        },
    }
    assert _dump(delta.to_meta()) == _dump(expected)


def test_delta_from_legacy_baseline_without_models():
    baseline = UsageAccount.from_meta(
        {
            "by_category": {
                "main": {
                    "input": 900,
                    "output": 50,
                    "calls": 2,
                    "missing_usage_calls": 1,
                    "cost": 0.5,
                },
                "summary": {
                    "input": 10,
                    "output": 2,
                    "calls": 1,
                    "cost_by_currency": {"": 0.25},
                },
            }
        }
    )
    delta = _merged().delta_from(baseline)
    expected = {
        "by_category": {
            "main": _bucket(
                {"USD": 0.6, "RUB": 3.0},
                MERGED_MAIN["by_model"],
                input=200,
                output=10,
                cached=5,
                calls=3,
                missing_usage_calls=1,
                billed_calls=2,
                billed_input=500,
                billed_output=30,
                billed_cached=5,
            ),
            "summary": _bucket(
                {}, {"fast": CHILD_FAST}, input=20, output=1, calls=1
            ),
            "7": RAW_SEVEN,
        },
        "by_model": MODELS_AFTER_CHILD,
    }
    assert _dump(delta.to_meta()) == _dump(expected)


def test_delta_from_non_account_baseline_is_a_copy():
    account = _merged()
    assert _dump(account.delta_from(None).to_meta()) == _dump(
        account.to_meta()
    )


def test_delta_drops_categories_and_models_without_change():
    account = UsageAccount()
    account.add(
        "main", 10, 1, model="m", billed=True, cost=0.3, currency="USD"
    )
    baseline = UsageAccount.from_meta(account.to_meta())
    account.by_category["main"]["cost_by_currency"]["USD"] += 1e-12
    assert _dump(account.delta_from(baseline).to_meta()) == _dump(
        {"by_category": {}}
    )

    account.add("summary", 0, 0)
    expected = {"by_category": {"summary": _bucket({}, calls=1)}}
    assert _dump(account.delta_from(baseline).to_meta()) == _dump(expected)


PRICING = {
    "enabled": True,
    "currency": "USD",
    "per_tokens": 1000,
    "models": {"fast": {"input": 1, "output": 2, "cache_read": 0.1}},
}


def _report_run():
    run = UsageAccount()
    run.add(
        "main",
        1000,
        100,
        cached_tokens=200,
        model="fast",
        billed=True,
        cost=0.01,
        currency="USD",
    )
    run.add("main", 500, 50, model="big")
    run.add_missing("summary", model="big")
    return run


REPORT_MODELS = {
    "fast": {"input": 1000, "output": 100, "calls": 1, "cached": 200},
    "big": {"input": 500, "output": 50, "calls": 2, "missing_usage_calls": 1},
}
REPORT_TOTAL = {
    "input": 1500,
    "output": 150,
    "cached": 200,
    "missing_usage_calls": 1,
}


def test_run_usage_dict_with_cost_is_exact():
    report = build_run_usage_dict(
        _report_run(), PRICING, "fast", "https://api.example.com"
    )
    expected = {
        "model": "fast",
        "categories": {
            "main": {
                "input": 1500,
                "output": 150,
                "cached": 200,
                "cost_source": "provider",
                "cost_parts": [
                    {"amount": 0.01, "currency": "USD", "source": "provider"}
                ],
                "unpriced": True,
                "cost": 0.01,
                "currency": "USD",
            },
            "summary": {
                "input": 0,
                "output": 0,
                "missing_usage_calls": 1,
                "cost_source": "none",
                "cost_parts": [],
                "unpriced": False,
            },
        },
        "total": REPORT_TOTAL,
        "models": REPORT_MODELS,
        "cost": {"by_currency": {"USD": 0.01}, "unpriced": True},
    }
    assert _dump(report) == _dump(expected)


def test_run_usage_dict_without_cost_is_exact():
    report = build_run_usage_dict(_report_run(), {"enabled": False}, None)
    expected = {
        "model": None,
        "categories": {
            "main": {"input": 1500, "output": 150, "cached": 200},
            "summary": {"input": 0, "output": 0, "missing_usage_calls": 1},
        },
        "total": REPORT_TOTAL,
        "models": REPORT_MODELS,
    }
    assert _dump(report) == _dump(expected)


def test_run_usage_dict_without_models_has_no_models_key():
    run = UsageAccount()
    run.add("main", 10, 1)
    report = build_run_usage_dict(run, {"enabled": False}, None)
    assert _dump(report) == _dump(
        {
            "model": None,
            "categories": {"main": {"input": 10, "output": 1}},
            "total": {"input": 10, "output": 1},
        }
    )
