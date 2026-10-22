"""Палитра от фона терминала: `apply_background` пересчитывает фон и
зависящие от него поверхности (тема auto), `reset` возвращает литералы
темы fixed. Без вызова `apply_background` палитра — ровно литералы."""

from __future__ import annotations

import pytest
import test_palette_diff_tokens as diff_guards
import test_palette_sync as sync_guards

from gc_tui import palette

# Снимок темы fixed: то, что видят все, кто не вызвал apply_background.
_FIXED_CSS = {
    "bg": "#0E1119",
    "ink": "#ebe9e6",
    "bright": "#eef2f6",
    "dim": "#8b949e",
    "sub-ink": "#bbbec2",
    "muted": "#6e7681",
    "deep-dim": "#7a8290",
    "accent": "#54B3A6",
    "code": "#afbdff",
    "tool-name": "#BCC3CC",
    "code-block": "#9aa4b2",
    "ok": "#5FAE7F",
    "run-color": "#D4A574",
    "olive": "#8FA05A",
    "err": "#C75C54",
    "warn": "#f0883e",
    "refused": "#8a522d",
    "info": "#58a6ff",
    "brand": "#9A6DD7",
    "band-bg": "#1f2128",
    "block-bg": "#171a22",
    "focus-bg": "#363b47",
    "screen-selection-background": "#1e416c",
    "card-select-bg": "#2e323d",
    "frame-archive": "#4b525d",
    "note-ink": "#a5adb8",
    "user-band-bg": "#282c36",
    "queue-bg": "#1c2029",
    "pill-hover-bg": "#4c505a",
    "overlay-bg": "#161A23",
    "zebra-bg": "#232836",
    "skill": "#afd7ff",
    "diff-add-bg": "#253f37",
    "diff-del-bg": "#44272a",
    "diff-add-hl": "#36604c",
    "diff-del-hl": "#6a3636",
    "diff-gutter": "#6e7681",
    "diff-add-gutter": "#5FAE7F",
    "diff-del-gutter": "#C75C54",
}
_FIXED_TRACK = "#3f4654"
_RAMPS = (
    "GLOW_SHADES",
    "PROCESSING_SHADES",
    "VALIDATING_SHADES",
)
_TINTED = (
    "FOCUS_BG",
    "CARD_SELECT_BG",
    "USER_BAND_BG",
    "OVERLAY_BG",
    "ZEBRA_BG",
    "TRACK",
)
_FIXED_BG = (0x0E, 0x11, 0x19)
_TERMINAL_BGS = ["#212733", "#16191e", "#000000", "#1e1e1e"]


@pytest.fixture(autouse=True)
def _restore_palette():
    yield
    palette.reset()


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _channel_gap(a: str, b: str) -> int:
    return max(abs(x - y) for x, y in zip(_rgb(a), _rgb(b)))


def _fixed_snapshot() -> dict[str, object]:
    snapshot: dict[str, object] = dict(palette.css_variables())
    snapshot["TRACK"] = palette.TRACK
    for name in _RAMPS:
        snapshot[name] = [c.lower() for c in getattr(palette, name)]
    return snapshot


def _expected_fixed() -> dict[str, object]:
    expected: dict[str, object] = dict(_FIXED_CSS)
    expected["TRACK"] = _FIXED_TRACK
    expected.update(sync_guards._PREVIOUS_RAMPS)
    return expected


def test_without_apply_palette_is_fixed_literals():
    assert _fixed_snapshot() == _expected_fixed()


def test_reset_restores_fixed_literals():
    palette.apply_background((0x21, 0x27, 0x33))
    palette.reset()
    assert _fixed_snapshot() == _expected_fixed()


def test_apply_background_sets_bg_everywhere():
    palette.apply_background((0x21, 0x27, 0x33))
    from gc_tui.palette import BG

    assert BG == "#212733"
    assert palette.css_variables()["bg"] == "#212733"


def test_apply_background_recomputes_surfaces():
    palette.apply_background((0x21, 0x27, 0x33))
    from gc_tui.palette import BAND_BG, DIFF_ADD_BG

    assert BAND_BG == palette._mix("#212733", palette.INK, 0.075)
    assert DIFF_ADD_BG == "#385551"  # #253f37 + (#212733 − #0E1119)
    assert palette.css_variables()["band-bg"] == BAND_BG


def test_refused_is_muted_warn_on_terminal_background():
    """Отказ в ленте — приглушённый оранжевый: примесь WARN к фону."""
    palette.apply_background((0x21, 0x27, 0x33))
    assert palette.REFUSED == palette._mix("#212733", palette.WARN, 0.55)
    assert palette.css_variables()["refused"] == palette.REFUSED


def test_bg_derived_exact_at_fixed_background():
    palette.apply_background(_FIXED_BG)
    css = palette.css_variables()
    exact = ("band-bg", "block-bg", "diff-add-bg", "diff-del-bg")
    exact += ("diff-add-hl", "diff-del-hl")
    assert {k: css[k] for k in exact} == {k: _FIXED_CSS[k] for k in exact}


@pytest.mark.parametrize("name", _TINTED)
def test_tinted_surfaces_close_at_fixed_background(name):
    fixed = getattr(palette, name)
    palette.apply_background(_FIXED_BG)
    assert _channel_gap(getattr(palette, name), fixed) <= 3


@pytest.mark.parametrize("name", _RAMPS)
def test_ramps_close_at_fixed_background(name):
    palette.apply_background(_FIXED_BG)
    expected = sync_guards._PREVIOUS_RAMPS[name]
    ramp = getattr(palette, name)
    gaps = [_channel_gap(a, b) for a, b in zip(ramp, expected)]
    assert len(ramp) == len(expected)
    assert max(gaps) <= 3


_SYNC_GUARDS = (
    "test_css_variables_match_palette_constants",
    "test_selection_distinguishable_from_focus",
    "test_comments_readable_inside_selection",
    "test_selection_does_not_swallow_syntax_colors",
)
_DIFF_GUARDS = sorted(n for n in vars(diff_guards) if n.startswith("test_"))


@pytest.mark.parametrize("bg", _TERMINAL_BGS)
@pytest.mark.parametrize(
    "guard",
    [(sync_guards, n) for n in _SYNC_GUARDS]
    + [(diff_guards, n) for n in _DIFF_GUARDS],
    ids=lambda g: g[1],
)
def test_guards_hold_on_terminal_background(bg, guard):
    palette.apply_background(_rgb(bg))
    module, name = guard
    getattr(module, name)()


def test_contrast_is_wcag_ratio():
    assert palette.contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21)
    assert palette.contrast((9, 9, 9), (9, 9, 9)) == pytest.approx(1)
