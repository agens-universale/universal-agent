from gc_testkit.tui_support import tui_settings
import pytest
from gc_tui.widgets.confirm_banner import (
    _OPTIONS,
    ConfirmBanner,
    _fmt_field_value,
    _fmt_payload_value,
    _is_payload_label,
    choice_for,
)


def test_banner_has_four_options():
    assert [k for k, _, _ in _OPTIONS] == ["[Y]", "[O]", "[N]", "[T]"]
    assert [c for _, _, c in _OPTIONS] == [
        "allow_remember",
        "allow_once",
        "deny_remember",
        "deny_once",
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


def test_fmt_payload_value_keeps_full_script():
    """Normal scripts are shown as-is (scroll handles overflow; no 500-char cut)."""
    body = "\n".join(f"line_{i} = {i}" for i in range(80))
    out = _fmt_payload_value(body)
    assert out == body
    assert "line_79" in out
    assert "… (+" not in out


def test_fmt_payload_value_keeps_long_lines():
    """Long single lines are not width-clipped — terminal soft-wraps inside scroll."""
    long_line = "p" * 400
    assert _fmt_payload_value(long_line) == long_line


def test_fmt_payload_value_safety_net_only():
    """Only pathological megabyte blobs hit the safety net."""
    huge = "x" * 100_050
    out = _fmt_payload_value(huge, safety_limit=100_000)
    assert out.startswith("x" * 100_000)
    assert out.endswith("… (+50 chars)")


def test_banner_holds_question_and_options():
    b = ConfirmBanner(
        banner_title="Write guard", question="Allow write to AGENTS.md?"
    )
    assert b.question == "Allow write to AGENTS.md?"
    assert b.banner_title == "Write guard"


def test_banner_defaults_to_no_fields():
    b = ConfirmBanner(banner_title="t", question="q")
    assert b.fields == []


def test_banner_stores_fields():
    fields = [("Tool", "execute_command"), ("Команда", "rm -rf /tmp/x")]
    b = ConfirmBanner(
        banner_title="Command moderation", question="Разрешить?", fields=fields
    )
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
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner(
            "Command moderation",
            "Разрешить выполнение команды?",
            [
                ("Команда", "rm -rf /tmp/x"),
                ("Риск", "destructive"),
                ("Reason", ""),
            ],
        )
        await pilot.pause()
        payload_texts = [
            str(s.render())
            for s in scr.query(Static)
            if s.has_class("cb-payload")
        ]
        meta_texts = [
            str(s.render())
            for s in scr.query(Static)
            if s.has_class("cb-field")
        ]
        payload_joined = "\n".join(payload_texts)
        meta_joined = "\n".join(meta_texts)
        assert "Команда: rm -rf /tmp/x" in payload_joined
        assert "Риск: destructive" in meta_joined
        # Пустое поле Reason="" не рендерится.
        assert "Reason:" not in payload_joined
        assert "Reason:" not in meta_joined


@pytest.mark.asyncio
async def test_banner_long_payload_keeps_options_visible(tmp_path):
    """Длинный multiline payload не выталкивает Y/O/N/T за край баннера."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from textual.widgets import Static

    # Много строк + длинные path'ы (как на скрине) — soft-wrap раньше съедал экран.
    long_code = "\n".join(
        f'path_{i} = "/home/alex/very/long/directory/tree/file_{i}.html"'
        for i in range(40)
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test(size=(100, 28)) as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner(
            "Проверка команды",
            "Разрешить запуск? (exec_python)",
            [("Код", long_code), ("Причина", "network"), ("Риски", "x")],
        )
        # cap sticky banner runs call_after_refresh → need a second layout pass.
        await pilot.pause()
        await pilot.pause()
        banners = list(scr.query(ConfirmBanner))
        assert len(banners) == 1
        banner = banners[0]
        opts = list(banner.query("Static.cb-options"))
        assert len(opts) == 1
        opt_text = str(opts[0].render())
        assert "[Y]" in opt_text and "[T]" in opt_text
        # Options docked inside banner box; banner capped ≤ half screen.
        b_region = banner.region
        o_region = opts[0].region
        assert o_region.y >= b_region.y
        assert o_region.bottom <= b_region.bottom
        assert o_region.height >= 4
        assert b_region.height <= max(14, pilot.app.size.height // 2 + 1), (
            f"banner too tall: {b_region.height}"
        )
        assert b_region.bottom <= pilot.app.size.height
        # Docked options near bottom of banner (1-row round border may remain below).
        assert o_region.bottom >= b_region.bottom - 2
        assert o_region.y > b_region.y + 2  # below title/question
        # Full payload is in the widget (scrollable); not soft-truncated.
        payload = [
            s for s in banner.query(Static) if s.has_class("cb-payload")
        ]
        assert payload
        rendered = str(payload[0].render())
        assert "path_0" in rendered
        assert "path_39" in rendered
        assert "… (+" not in rendered


@pytest.mark.asyncio
async def test_banner_hugs_short_content(tmp_path):
    """Короткий контент: баннер не раздувается до потолка.

    `.cb-body { height: 1fr }` внутри `auto`-родителя убирает зависимость от
    контента, и баннер всегда занимал максимум — при пяти строках содержимого.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner(
            "Проверка команды",
            "Разрешить запуск?",
            [("Команда", "ls -la"), ("Риски", "low")],
        )
        await pilot.pause()
        await pilot.pause()
        banner = list(scr.query(ConfirmBanner))[0]
        cap = pilot.app.size.height // 2

        assert banner.region.height < cap, (
            f"баннер занял потолок ({banner.region.height}) при коротком контенте"
        )


@pytest.mark.asyncio
async def test_banner_body_scrolls_from_keyboard(tmp_path):
    """Длинную команду должно быть видно целиком без мыши."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner

    long_code = "\n".join(f"line_{i} = {i}" for i in range(60))
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner(
            "Проверка команды", "Разрешить запуск?", [("Код", long_code)]
        )
        await pilot.pause()
        await pilot.pause()
        banner = list(scr.query(ConfirmBanner))[0]
        body = banner.query_one(".cb-body")
        assert body.max_scroll_y > 0, "нечего скроллить — тест бессмыслен"

        await pilot.press("pagedown")
        await pilot.pause()
        assert body.scroll_y > 0, "pagedown не прокрутил тело баннера"

        await pilot.press("home")
        await pilot.pause()
        assert body.scroll_y == 0, "home не вернул тело в начало"

        await pilot.press("end")
        await pilot.pause()
        assert body.scroll_y == body.max_scroll_y


@pytest.mark.asyncio
async def test_scroll_keys_do_not_answer_the_banner(tmp_path):
    """Прокрутка — не ответ: баннер остаётся активным."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    long_code = "\n".join(f"line_{i}" for i in range(60))
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Проверка", "Разрешить?", [("Код", long_code)])
        await pilot.pause()
        await pilot.pause()

        await pilot.press("pagedown")
        await pilot.press("end")
        await pilot.pause()

        assert scr._confirm_active is True
