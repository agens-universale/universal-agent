import pytest

from tui.widgets.confirm_banner import ConfirmBanner, _fmt_field_value


def test_banner_holds_question_and_options():
    b = ConfirmBanner(banner_title="Write guard", question="Allow write to AGENTS.md?")
    assert b.question == "Allow write to AGENTS.md?"
    assert b.banner_title == "Write guard"


def test_banner_defaults_to_no_fields():
    b = ConfirmBanner(banner_title="t", question="q")
    assert b.fields == []


def test_banner_stores_fields():
    fields = [("Tool", "execute_command"), ("Команда", "rm -rf /tmp/x")]
    b = ConfirmBanner(banner_title="Command moderation", question="Разрешить?", fields=fields)
    assert b.fields == fields


def test_fmt_field_value_joins_lists():
    assert _fmt_field_value(["/a", "/b"]) == "/a, /b"


def test_fmt_field_value_collapses_whitespace():
    assert _fmt_field_value("rm   -rf\n/tmp") == "rm -rf /tmp"


def test_fmt_field_value_truncates_long():
    out = _fmt_field_value("x" * 500, limit=200)
    assert len(out) == 200 and out.endswith("…")


@pytest.mark.asyncio
async def test_banner_renders_field_rows(tmp_path):
    """fields рендерятся строками cb-field; пустые значения пропускаются."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner(
            "Command moderation",
            "Разрешить выполнение команды?",
            [("Команда", "rm -rf /tmp/x"), ("Риск", "destructive"), ("Reason", "")],
        )
        await pilot.pause()
        field_texts = [str(s.render()) for s in scr.query(Static) if s.has_class("cb-field")]
        joined = "\n".join(field_texts)
        assert "Команда: rm -rf /tmp/x" in joined
        assert "Риск: destructive" in joined
        # Пустое поле Reason="" не рендерится.
        assert "Reason:" not in joined
