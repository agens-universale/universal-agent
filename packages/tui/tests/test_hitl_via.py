"""HITL via-label on ask/confirm banners."""


def test_ask_human_banner_via_label():
    from gc_tui.widgets.ask_human_banner import (
        AskHumanBanner,
        format_ask_label,
    )

    b = AskHumanBanner(question="Q?", via="subagent_worker")
    assert b._via == "subagent_worker"
    assert "via subagent_worker" in format_ask_label("subagent_worker")
    assert format_ask_label(None) == "? АГЕНТ СПРАШИВАЕТ"


def test_confirm_banner_via_in_label():
    from gc_tui.widgets.confirm_banner import build_guard_label

    plain = build_guard_label("Запись файла", via="subagent_critic").plain
    assert "Запись файла" in plain
    assert "via subagent_critic" in plain
