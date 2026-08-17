"""palette.py и styles.tcss — два источника цветов (TCSS-движок ≠ rich.Text);
этот тест ловит рассинхрон по любому $var <-> palette.*."""
from __future__ import annotations

import re
from pathlib import Path

from gc_tui import palette
from gc_testkit.paths import TUI_PKG


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
            mismatches.append(f"css_variables['{tcss_name}'] → нет palette.{py_name}")
            continue
        if getattr(palette, py_name) != css_value:
            mismatches.append(
                f"${tcss_name}={css_value} ≠ palette.{py_name}={getattr(palette, py_name)}"
            )
    assert not mismatches, "\n  ".join(mismatches)
