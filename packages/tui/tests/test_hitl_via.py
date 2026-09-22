"""HITL via-label on ask/confirm banners + consumer ask actions."""

from gc_core.tools.subagents.progress import SubagentProgressEvent
from gc_tui.services.subagent_progress import plan_progress_actions


def _ev(**kw):
    base = dict(
        parent_call_id="p1",
        child_run_id="c1",
        kind="ask_human",
        step=1,
        max_steps=10,
        elapsed_ms=0,
    )
    base.update(kw)
    return SubagentProgressEvent(**base)


def test_plan_ask_human_actions():
    actions = plan_progress_actions(
        [
            _ev(kind="ask_human", prose="Continue?", tool_name="subagent_worker"),
            _ev(kind="ask_human_done", tool_name="subagent_worker"),
        ]
    )
    kinds = [a.kind for a in actions]
    assert kinds == ["show_ask_human", "hide_ask_human"]
    show = actions[0]
    assert show.prose == "Continue?"
    assert show.via == "subagent_worker"


def test_ask_human_banner_via_label():
    from gc_tui.widgets.ask_human_banner import AskHumanBanner, format_ask_label

    b = AskHumanBanner(question="Q?", via="subagent_worker")
    assert b._via == "subagent_worker"
    assert "via subagent_worker" in format_ask_label("subagent_worker")
    assert format_ask_label(None) == "? АГЕНТ СПРАШИВАЕТ"


def test_confirm_banner_via_in_label():
    from gc_tui.widgets.confirm_banner import build_guard_label

    plain = build_guard_label("Запись файла", via="subagent_critic").plain
    assert "Запись файла" in plain
    assert "via subagent_critic" in plain
