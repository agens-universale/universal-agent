"""Рецепты MCP обязаны ограничивать версию `mcp` сверху.

Что случилось 27.08.2026. Три из четырёх вендоренных серверов объявляли `mcp>=1.26.0`
без верхней границы. Вышел `mcp 2.0.0`, где убран модуль `mcp.server.fastmcp`, — а
серверы импортируют `FastMCP` именно оттуда. pip честно поставил двойку (она
удовлетворяет `>=1.26.0`), и все три перестали запускаться:

    ModuleNotFoundError: No module named 'mcp.server.fastmcp'

Коварство в том, КАК это выглядело. `gc-mcp list` показывал «подключён», потому что
знает лишь то, что рецепт прописан в конфиг; проводка и PATH были исправны. А в
списке серверов агента те же три значились «нет соединения». `computer-mcp` при этом
работал бы — он единственный пинил `mcp==1.26.0` точно.

Переустановка рецепта НЕ лечила: `mcp 2.0.0` удовлетворяет `>=1.26.0`, и pip его не
трогал. Лечит только граница в манифесте.

Сетка смотрит на манифесты, а не на установленное окружение: она обязана падать на
машине, где venv ещё не собран, — иначе поймала бы поломку только после того, как
пользователь её словит.
"""

from __future__ import annotations

import tomllib

import pytest
from gc_testkit.paths import VENDOR

#: Манифесты вендоренных MCP-серверов. Каталог сканируется, а не перечисляется:
#: новый рецепт попадёт под сетку сам, без правки списка.
_MANIFESTS = sorted(VENDOR.glob("*/pyproject.toml"))


def _mcp_requirement(manifest) -> str | None:
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    for dep in data.get("project", {}).get("dependencies", []) or []:
        name = (
            dep.split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip()
        )
        if name == "mcp":
            return dep
    return None


def test_vendor_dir_is_not_empty():
    """Сканер должен что-то находить — иначе сетка молча проходит на пустом обходе."""
    assert _MANIFESTS, f"в {VENDOR} не найдено ни одного манифеста"


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=lambda p: p.parent.name)
def test_recipe_pins_mcp_below_2(manifest):
    """У требования `mcp` обязана быть верхняя граница."""
    req = _mcp_requirement(manifest)
    if req is None:
        pytest.skip(f"{manifest.parent.name} не зависит от mcp")

    assert "<" in req or "==" in req, (
        f"{manifest.parent.name}: требование {req!r} без верхней границы. "
        "mcp 2.0 убрал mcp.server.fastmcp — рецепт молча перестанет запускаться, "
        "а gc-mcp list всё равно покажет «подключён»."
    )
