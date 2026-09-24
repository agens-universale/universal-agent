"""palette.py и styles.tcss — два источника цветов (TCSS-движок ≠ rich.Text);
этот тест ловит рассинхрон по любому $var <-> palette.*."""

from __future__ import annotations

import re

from gc_testkit.paths import TUI_PKG
from gc_tui import palette

_STYLES_TCSS = TUI_PKG / "styles.tcss"


def _tcss_to_py_name(tcss_name: str) -> str:
    """`$run-color` → `RUN_COLOR`; `$ink` → `INK`."""
    return tcss_name.replace("-", "_").upper()


# `$name` — ссылка на переменную (единый источник значений — palette.css_variables()).
_TCSS_USE_RE = re.compile(r"\$([\w-]+)")


def _tcss_used_vars() -> set[str]:
    return set(_TCSS_USE_RE.findall(_STYLES_TCSS.read_text(encoding="utf-8")))


def test_tcss_vars_provided_by_palette():
    """Единый источник цветов — `palette.css_variables()` (инжектится в
    `TuiApp.get_css_variables()`); styles.tcss лишь ССЫЛАЕТСЯ на `$var`. Каждый
    использованный `$var` должен быть определён палитрой — иначе Textual не
    отрезолвит цвет (чёрный/ошибка рендера)."""
    used = _tcss_used_vars()
    provided = set(palette.css_variables())
    missing = sorted(v for v in used if v not in provided)
    assert not missing, (
        f"$var используются в styles.tcss, но нет в palette.css_variables(): {missing}"
    )


def test_css_variables_match_palette_constants():
    """`css_variables()` должен отдавать те же значения, что одноимённые константы
    `palette.*` (`$kebab-case` → `KEBAB_CASE`). CSS и rich.Text оба берут цвет из
    palette; опечатка в маппинге развела бы их — этот тест её ловит."""
    mismatches: list[str] = []
    for tcss_name, css_value in palette.css_variables().items():
        py_name = _tcss_to_py_name(tcss_name)
        if not hasattr(palette, py_name):
            mismatches.append(
                f"css_variables['{tcss_name}'] → нет palette.{py_name}"
            )
            continue
        if getattr(palette, py_name) != css_value:
            mismatches.append(
                f"${tcss_name}={css_value} ≠ palette.{py_name}={getattr(palette, py_name)}"
            )
    assert not mismatches, "\n  ".join(mismatches)


def _luminance(hex_color: str) -> float:
    value = hex_color.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    light, dark = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _distance(a: str, b: str) -> float:
    pa = [int(a.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    pb = [int(b.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    return sum((x - y) ** 2 for x, y in zip(pa, pb)) ** 0.5


def test_selection_distinguishable_from_focus():
    """Выделение текста и подсветка сфокусированного события не должны сливаться.

    Раньше они отличались на 21 — визуально неразличимо, когда выделение лежит
    поверх фокуса. Порог 40 держит нынешнюю разницу; уронить его правкой цвета
    молча теперь нельзя."""
    assert (
        _distance(palette.SCREEN_SELECTION_BACKGROUND, palette.FOCUS_BG) >= 40
    )


def test_comments_readable_inside_selection():
    """Комментарии кода читаемы поверх выделения (порог WCAG для крупного текста).

    Именно этот контраст ограничивает светлоту фона выделения: родной серый
    monokai давал здесь 1.0-1.9, поэтому комментарии переведены на $dim."""
    assert _contrast(palette.DIM, palette.SCREEN_SELECTION_BACKGROUND) >= 3.0


def test_selection_does_not_swallow_syntax_colors():
    """Самый тёмный токен подсветки остаётся различим на фоне выделения."""
    darkest_token = "#ae81ff"  # фиолетовый monokai — числа
    assert _contrast(darkest_token, palette.SCREEN_SELECTION_BACKGROUND) >= 3.0
