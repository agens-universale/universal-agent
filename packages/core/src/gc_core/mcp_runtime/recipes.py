"""Доступ к пакету рецептов MCP (`recommended_mcp`) с внятным отказом.

Рецепты — отдельный пакет, и ядро тянет его ЛЕНИВО: без него агент работает,
просто не поднимает HTTP/SSE-daemon'ы и не умеет `gc-mcp`. Раньше каждое место
импортировало его напрямую, и отсутствие пакета давало сырой
``ModuleNotFoundError`` с трейсбеком — в отличие от команд профилей, у которых
охранники есть с самого начала (``gc_core/{cli,tui,serve}_entry.py``).

Здесь одна точка входа, чтобы отказ был одинаковым и типизированным. Это же
понадобится, когда рецепты уедут в свой дистрибутив: сейчас ветка отказа
недостижима, потому что они лежат в колесе ядра.

ПОЧЕМУ ЗДЕСЬ ОБЫЧНЫЕ ``import``, А НЕ ``importlib.import_module``.
Первая редакция этого модуля резолвила подмодуль динамически — казалось
элегантнее. Это молча сломало тесты TUI: они подменяют ГЛОБАЛЬНУЮ
``importlib.import_module``, чтобы перехватить загрузку ``gc_tui.app``, и
заодно перехватывали наш импорт рецептов. В ``manifest_resolve`` прилетал
``MagicMock``, и прогон повисал (наблюдалось 18.08.2026). Обычный ``import``
идёт мимо такой подмены, поэтому здесь по функции на подмодуль.
"""

from __future__ import annotations

from types import ModuleType

_PACKAGE = "recommended_mcp"


class RecipesUnavailable(RuntimeError):
    """Пакет рецептов не установлен.

    Отдельный тип, чтобы вызывающий отличал «нет рецептов» от настоящей
    поломки внутри них и печатал строку вместо трейсбека.
    """


def _unavailable(exc: ImportError) -> RecipesUnavailable:
    return RecipesUnavailable(
        f"MCP-рецепты не установлены (нет пакета {_PACKAGE})"
    )


def _prepare() -> None:
    """Положить корень репозитория на sys.path (нужно для dev-раскладки)."""
    from gc_core.mcp_runtime.repo_path import ensure_repo_on_sys_path

    ensure_repo_on_sys_path()


def recipes_package() -> ModuleType:
    """Сам пакет ``recommended_mcp``."""
    _prepare()
    try:
        import recommended_mcp
    except ImportError as exc:
        if getattr(exc, "name", None) != _PACKAGE:
            raise
        raise _unavailable(exc) from exc
    return recommended_mcp


def recipes_manifest() -> ModuleType:
    """``recommended_mcp.manifest`` — разбор курируемого манифеста."""
    _prepare()
    try:
        from recommended_mcp import manifest
    except ImportError as exc:
        if getattr(exc, "name", None) != _PACKAGE:
            raise
        raise _unavailable(exc) from exc
    return manifest


def recipes_install() -> ModuleType:
    """``recommended_mcp.install`` — установка и снос MCP."""
    _prepare()
    try:
        from recommended_mcp import install
    except ImportError as exc:
        if getattr(exc, "name", None) != _PACKAGE:
            raise
        raise _unavailable(exc) from exc
    return install


def recipes_autostart() -> ModuleType:
    """``recommended_mcp.autostart`` — подъём HTTP/SSE-daemon'ов."""
    _prepare()
    try:
        from recommended_mcp import autostart
    except ImportError as exc:
        if getattr(exc, "name", None) != _PACKAGE:
            raise
        raise _unavailable(exc) from exc
    return autostart


def import_recipes(module: str) -> ModuleType:
    """Совместимость: по имени подмодуля вернуть его через статический геттер.

    Динамики здесь нет намеренно — см. докстроку модуля.
    """
    getters = {
        "manifest": recipes_manifest,
        "install": recipes_install,
        "autostart": recipes_autostart,
    }
    try:
        return getters[module]()
    except KeyError:
        raise ValueError(f"нет геттера для recommended_mcp.{module}") from None
