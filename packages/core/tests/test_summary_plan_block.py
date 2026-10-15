"""План сессии переживает сжатие истории: дословно, со статусом."""

from gc_core.plan_store import new_plan_block
from gc_core.summarize.summary_message_body import (
    build_dialog_summary_content,
    render_plan_block,
)


def test_plan_block_carries_text_and_status():
    plan = new_plan_block("1. шаг\n2. проверка")
    plan["status"] = "reworking"
    block = render_plan_block(plan)
    assert block.startswith('<current_plan status="reworking" ')
    assert "1. шаг\n2. проверка" in block
    assert block.endswith("</current_plan>")


def test_no_plan_no_block():
    for plan in (None, {}, {"text": "  ", "status": "proposed"}):
        assert render_plan_block(plan) == ""


def test_summary_body_includes_the_plan_only_when_there_is_one():
    plain = build_dialog_summary_content("итог", "шаги")
    assert "current_plan" not in plain
    with_plan = build_dialog_summary_content(
        "итог", "шаги", plan_block=render_plan_block(new_plan_block("1. шаг"))
    )
    assert "<current_plan" in with_plan and "1. шаг" in with_plan
    # без плана тело сводки прежнее
    assert build_dialog_summary_content("итог", "шаги", plan_block="") == plain


def test_summary_body_carries_the_rules_of_the_announced_mode():
    """Сообщение о входе в режим сжатие уносит; режим продолжает действовать."""
    from gc_core.message_policy.mode_messages import (
        AUTO_ENTER_MESSAGE,
        render_mode_block,
    )

    plain = build_dialog_summary_content("итог", "шаги")
    body = build_dialog_summary_content(
        "итог", "шаги", mode_block=render_mode_block("auto")
    )
    assert AUTO_ENTER_MESSAGE in body
    assert body.index("<permission_mode") < body.index("<last_dialog_steps>")
    # у edit и manual правил для модели нет — тело сводки прежнее
    assert (
        build_dialog_summary_content(
            "итог", "шаги", mode_block=render_mode_block(None)
        )
        == plain
    )
