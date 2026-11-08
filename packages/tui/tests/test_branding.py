"""branding — единый источник бренд-контента TUI GIGACODE."""

from rich.console import Console

from gc_tui import branding
from gc_tui.services.start_status import loading_line, settled_row


def test_gradient_color_stops():
    # стопы градиента бренда: GRADIENT_WARM -> MID -> COOL
    assert branding.gradient_color(0.0) == "#D9A36A"
    assert branding.gradient_color(0.5) == "#8FA059"
    assert branding.gradient_color(1.0) == "#4EB4A6"


def test_gradient_color_clamps():
    assert branding.gradient_color(-1.0) == "#D9A36A"
    assert branding.gradient_color(2.0) == "#4EB4A6"


def test_version_forms_follow_one_rule(monkeypatch):
    """Короткая форма — для тесных строк (шапка /settings), длинная — где есть место
    (карточка, секция «Обновление»). Правило одно: из исходников номер не показываем,
    он остался от установки пакета и коду уже не соответствует."""
    from gc_core import app_version

    monkeypatch.setattr(
        app_version,
        "install_source",
        lambda: app_version.InstallSource("u", None, None, True),
    )
    assert app_version.display_version() == "dev"
    assert app_version.display_version(long=True) == app_version.DEV_LABEL

    monkeypatch.setattr(
        app_version,
        "install_source",
        lambda: app_version.InstallSource("u", "master", "c", False),
    )
    monkeypatch.setattr(app_version, "agent_version", lambda: "1.2.3")
    # установлено штатно — обе формы одинаковы, пояснять нечего
    assert app_version.display_version() == "v1.2.3"
    assert app_version.display_version(long=True) == "v1.2.3"


# --- BrandHeader (стартовая шапка) ---


_TAIL = "↑ доступна новая версия · /settings → Обновление"
_DEV = "dev · установка из исходников"


def _lines(renderable, width):
    console = Console(width=width, record=True, color_system="truecolor")
    console.print(renderable)
    return [ln.rstrip() for ln in console.export_text().split("\n")][:-1]


def test_mark_lines_exact():
    assert branding.MARK == (
        "█▀▀ █ █▀▀ ▄▀█ █▀▀ █▀█ █▀▄ █▀▀",
        "█▄█ █ █▄█ █▀█ █▄▄ █▄█ █▄▀ ██▄",
    )
    assert all(len(line) == 29 for line in branding.MARK)


def test_brand_wide():
    lines = _lines(branding.BrandHeader("v0.5.0"), 96)
    assert lines == [
        branding.MARK[0] + "   Universal Agent",
        branding.MARK[1] + "   v0.5.0",
    ]


def test_update_tail_beside_when_fits():
    lines = _lines(branding.BrandHeader("v0.5.0", update=True), 96)
    assert len(lines) == 2
    assert lines[1].endswith("v0.5.0  " + _TAIL)


def test_update_tail_beside_threshold():
    r = branding.BrandHeader("v0.5.0", update=True)
    assert len(_lines(r, 88)) == 2
    assert len(_lines(r, 87)) == 3


def test_update_tail_own_row_when_narrow():
    r = branding.BrandHeader("v0.5.0", update=True, status=settled_row(9, 9))
    lines = _lines(r, 70)
    assert lines[1] == branding.MARK[1] + "   v0.5.0"
    assert lines[-1] == "  " + _TAIL


def test_single_line_below_threshold():
    lines = _lines(branding.BrandHeader("v0.5.0"), 45)
    assert lines[0] == "GigaCode  Universal Agent  v0.5.0"
    assert len(_lines(branding.BrandHeader("v0.5.0"), 47)) == 2
    assert len(_lines(branding.BrandHeader("v0.5.0"), 46)) == 1


def test_dev_version_threshold():
    r = branding.BrandHeader(_DEV)
    assert _lines(r, 61)[0].startswith(branding.MARK[0])
    assert _lines(r, 60)[0].startswith("GigaCode  Universal Agent")


def test_status_follows_after_blank():
    r = branding.BrandHeader("v0.5.0", status=settled_row(9, 9))
    lines = _lines(r, 96)
    assert lines[2:] == ["", "✓ готов к работе   MCP 9/9 · /mcp"]


def test_no_blank_without_status():
    assert len(_lines(branding.BrandHeader("v0.5.0"), 96)) == 2


def test_loading_status_truncated_not_wrapped():
    long = ("очень-длинное-имя-сервера-mcp", True)
    r = branding.BrandHeader("v0.5.0", status=loading_line(3, 9, long, 0))
    lines = _lines(r, 30)
    assert lines[-1].endswith("…")
    assert sum(1 for ln in lines if "MCP" in ln) == 1


def test_mark_gradient_spans_columns():
    console = Console(width=96, color_system="truecolor")
    opts = console.options
    line = console.render_lines(
        branding.BrandHeader("v0.5.0"), opts, pad=False
    )[0]
    cells = [(c, seg.style) for seg in line for c in seg.text]
    first, last = cells[0][1], cells[28][1]
    assert first.color.get_truecolor().hex.lower() == (
        branding.gradient_color(0.0).lower()
    )
    assert last.color.get_truecolor().hex.lower() == (
        branding.gradient_color(1.0).lower()
    )
    assert first.bold


def test_measure_is_widest_layout():
    from rich.measure import Measurement

    console = Console(width=200)
    r = branding.BrandHeader("v0.5.0", update=True)
    assert Measurement.get(console, console.options, r).maximum == 88
