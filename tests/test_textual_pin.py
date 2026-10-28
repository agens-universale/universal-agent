"""Пин Textual: диапазон, на котором проверены патчи TUI, во всех манифестах.

Патчи и приватные импорты Textual проверены только на 8.2.8; переход на
новую версию — отдельная задача, а не побочный эффект `uv lock`.
"""

import tomllib

from gc_testkit.paths import REPO

VERIFIED_PIN = "textual>=8.2.8,<8.3"


def _textual_deps(manifest: str) -> list[str]:
    data = tomllib.loads((REPO / manifest).read_text(encoding="utf-8"))
    groups = [data["project"]["dependencies"]]
    groups.extend(data.get("dependency-groups", {}).values())
    return [
        dep
        for group in groups
        for dep in group
        if isinstance(dep, str) and dep.startswith("textual")
    ]


def test_tui_manifest_pins_verified_textual():
    assert _textual_deps("packages/tui/pyproject.toml") == [VERIFIED_PIN]


def test_root_manifest_pins_verified_textual():
    assert _textual_deps("pyproject.toml") == [VERIFIED_PIN]
