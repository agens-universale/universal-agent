"""palette.py и styles.tcss — два источника цветов (TCSS-движок ≠ rich.Text);
этот тест ловит рассинхрон по любому $var <-> palette.*."""
from __future__ import annotations

import re
from pathlib import Path

from tui import palette


# `$var-name: #RRGGBB;` — допускаем пробелы/табуляции, игнорируем хвостовой
# комментарий. Сложные значения (например `outer #hex`) не матчатся — это и есть
# желаемое поведение: трекаем только токены палитры.
_TCSS_VAR_RE = re.compile(r"^\$([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")
_STYLES_TCSS = Path(__file__).resolve().parents[2] / "src" / "tui" / "styles.tcss"


def _tcss_to_py_name(tcss_name: str) -> str:
    """`$run-color` → `RUN_COLOR`; `$ink` → `INK`."""
    return tcss_name.replace("-", "_").upper()


def _parse_tcss_vars() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _STYLES_TCSS.read_text(encoding="utf-8").splitlines():
        m = _TCSS_VAR_RE.match(line.lstrip())
        if m:
            out[m.group(1)] = m.group(2).lower()
    return out


def test_palette_matches_tcss():
    """Каждая `$var` из styles.tcss должна совпадать с одноимённой константой
    `palette.*` (правило: `$kebab-case` → `KEBAB_CASE`).

    Лечит обе типичные ошибки правки цвета:
    - изменил в TCSS, забыл в palette → rich.Text красит по-старому;
    - изменил в palette, забыл в TCSS → CSS-классы красят по-старому.
    """
    tcss = _parse_tcss_vars()
    assert tcss, f"styles.tcss не отдал ни одного $var (путь: {_STYLES_TCSS})"

    mismatches: list[str] = []
    missing_in_palette: list[str] = []
    for tcss_name, tcss_hex in tcss.items():
        py_name = _tcss_to_py_name(tcss_name)
        if not hasattr(palette, py_name):
            missing_in_palette.append(f"${tcss_name} → palette.{py_name}")
            continue
        py_hex = getattr(palette, py_name).lower()
        if py_hex != tcss_hex:
            mismatches.append(
                f"${tcss_name}={tcss_hex} (TCSS) ≠ palette.{py_name}={py_hex}"
            )

    parts: list[str] = []
    if mismatches:
        parts.append("Расходящиеся значения:\n  " + "\n  ".join(mismatches))
    if missing_in_palette:
        parts.append(
            "Есть в TCSS, нет в palette.py:\n  " + "\n  ".join(missing_in_palette)
        )
    assert not parts, "\n\n".join(parts)
