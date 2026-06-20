from tui.widgets.confirm_banner import ConfirmBanner


def test_banner_holds_question_and_options():
    b = ConfirmBanner(banner_title="Write guard", question="Allow write to AGENTS.md?")
    assert b.question == "Allow write to AGENTS.md?"
    assert b.banner_title == "Write guard"
