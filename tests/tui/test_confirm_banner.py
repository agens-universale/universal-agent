import pytest

from tui.widgets.confirm_banner import (
    ConfirmBanner,
    _fmt_field_value,
    _fmt_payload_value,
    _is_payload_label,
    _OPTIONS,
    choice_for,
)


def test_banner_has_four_options():
    assert [k for k, _, _ in _OPTIONS] == ["[Y]", "[O]", "[N]", "[T]"]
    assert [c for _, _, c in _OPTIONS] == [
        "allow_remember", "allow_once", "deny_remember", "deny_once",
    ]
    labels = [lab for _, lab, _ in _OPTIONS]
    assert "подобные в сессии" in labels[0]
    assert labels[1] == "Разрешить разово"
    assert "подобные в сессии" in labels[2]
    assert labels[3] == "Запретить разово"


def test_choice_for_maps_indices():
    assert choice_for(0) == "allow_remember"
    assert choice_for(1) == "allow_once"
    assert choice_for(2) == "deny_remember"
    assert choice_for(3) == "deny_once"
    assert choice_for(-1) == "deny_remember"
    assert choice_for(99) == "deny_remember"


def test_is_payload_label_case_insensitive():
    assert _is_payload_label("Команда")
    assert _is_payload_label("command")
    assert _is_payload_label("CODE")
    assert not _is_payload_label("Причина")
    assert not _is_payload_label("Preview")


def test_fmt_payload_value_truncates_with_omitted_count():
    out = _fmt_payload_value("a" * 520, limit=500)
    assert out.startswith("a" * 500)
    assert out.endswith("… (+20 chars)")


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
async def test_banner_renders_payload_and_meta_fields(tmp_path):
    """Payload-поле рендерится cb-payload; meta-поля cb-field; пустые пропускаются."""
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
        payload_texts = [str(s.render()) for s in scr.query(Static) if s.has_class("cb-payload")]
        meta_texts = [str(s.render()) for s in scr.query(Static) if s.has_class("cb-field")]
        payload_joined = "\n".join(payload_texts)
        meta_joined = "\n".join(meta_texts)
        assert "Команда: rm -rf /tmp/x" in payload_joined
        assert "Риск: destructive" in meta_joined
        # Пустое поле Reason="" не рендерится.
        assert "Reason:" not in payload_joined
        assert "Reason:" not in meta_joined
