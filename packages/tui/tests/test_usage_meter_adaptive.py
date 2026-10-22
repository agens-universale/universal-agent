"""usage_segments + compose_adaptive_footer_right: раздельные сегменты и
адаптивная вёрстка (сессия уезжает под запрос, когда не хватает ширины)."""

from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
from gc_tui.services.usage_meter import (
    compose_adaptive_footer_right,
    usage_segments,
)
from rich.text import Text

_PRICING = {
    "enabled": True,
    "input": 1.0,
    "output": 2.0,
    "currency": "₽",
    "per_tokens": 1_000_000,
}


def _meta(i, o):
    a = UsageAccount()
    a.add(CATEGORY_MAIN, i, o, model="m")
    return a.to_meta()


def test_segments_returns_request_and_session_separately():
    # Чисто тарифный отчёт (нет факта провайдера) — тильды нет.
    req, sess = usage_segments(
        _meta(2_000_000, 0), _meta(1_000_000, 0), _PRICING, "m"
    )
    assert req.plain == "запрос 1.0M/1.00₽"
    assert sess.plain == "сессия 2.0M/2.00₽"


def test_segments_none_none_when_session_empty():
    assert usage_segments(UsageAccount().to_meta(), None, _PRICING, "m") == (
        None,
        None,
    )


def test_segments_request_none_when_no_request():
    req, sess = usage_segments(_meta(2_000_000, 0), None, _PRICING, "m")
    assert req is None
    assert sess.plain == "сессия 2.0M/2.00₽"


def _head():
    h = Text()
    h.append("model")
    return h


def test_single_line_when_fits():
    req = Text("запрос 1.0M/1.0₽")
    sess = Text("сессия 2.0M/2.0₽")
    out = compose_adaptive_footer_right(
        _head(), req, sess, available_width=200
    )
    assert "\n" not in out.plain
    assert "запрос" in out.plain and "сессия" in out.plain


def test_stacks_both_usage_segments_when_tight():
    req = Text("запрос 1.0M/1.0₽")
    sess = Text("сессия 2.0M/2.0₽")
    out = compose_adaptive_footer_right(_head(), req, sess, available_width=25)
    lines = out.plain.split("\n")
    assert len(lines) == 2
    assert lines[0] == "model"  # head без usage
    assert (
        lines[1] == "запрос 1.0M/1.0₽ · сессия 2.0M/2.0₽"
    )  # токен-блок целиком


def test_no_stack_when_only_one_segment():
    # нет «запроса» → стекать нечего, одна строка даже при узкой ширине
    sess = Text("сессия 2.0M/2.0₽")
    out = compose_adaptive_footer_right(_head(), None, sess, available_width=5)
    assert "\n" not in out.plain
