"""Точки входа `gc-mcp` и `gc-mcp-install`. Живут в ЯДРЕ, как и соседние шимы.

Менеджер MCP уехал в дистрибутив gc-mcp (пакет ``recommended_mcp.runtime``),
а команда объявляется всеми профилями приложения. Значит её могут набрать в
окружении, где этого дистрибутива нет — тогда нужна одна строка, а не трейсбек.

Тот же приём, что у ``cli_entry`` / ``tui_entry`` / ``serve_entry``. Отличие
одно: там не хватает ПАКЕТА профиля, здесь — пакета рецептов, поэтому отказ
приходит типизированным ``RecipesUnavailable`` из ``mcp_runtime.recipes``.
"""
from __future__ import annotations

import sys
from typing import Optional, Sequence

_MISSING_MCP_MSG = (
    "Менеджер MCP не установлен (нет пакета recommended_mcp). "
    "Он приезжает с любым профилем приложения — переустановите профиль."
)


def _run(argv: Optional[Sequence[str]], *, install: bool) -> int:
    from gc_core.mcp_runtime.recipes import RecipesUnavailable

    try:
        from recommended_mcp.runtime import cli, install_cli
    except ImportError as exc:
        if getattr(exc, "name", None) not in ("recommended_mcp", "recommended_mcp.runtime"):
            raise
        print(_MISSING_MCP_MSG, file=sys.stderr)
        return 1

    try:
        return install_cli.main() if install else cli.main(argv)
    except RecipesUnavailable as exc:
        print(f"{exc}. Переустановите профиль.", file=sys.stderr)
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`gc-mcp` — менеджер MCP-серверов."""
    return _run(argv, install=False)


def install(argv: Optional[Sequence[str]] = None) -> int:
    """`gc-mcp-install` — back-compat алиас на `gc-mcp install`."""
    return _run(argv, install=True)


if __name__ == "__main__":
    raise SystemExit(main())
