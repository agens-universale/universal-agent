"""Путь к корню репозитория в editable-установке.

Рецепты (`recommended_mcp`) — отдельный дистрибутив, и в рабочей копии их
исходники лежат в `packages/mcp/src/`. Чтобы `import recommended_mcp` работал
из dev-окружения, корень репозитория нужно найти и положить на `sys.path`
вместе с каталогами исходников всех пакетов.

Почему маркер продублирован. Каноническая реализация — в
`gc_core.bundled_assets.repo_root_from`, но именно этот модуль зовётся из
бутстрапа `install.py` / `run.py`, то есть ДО того, как gc_core вообще
появляется на `sys.path`: импортировать оттуда нечего, класть туда gc_core —
его работа. Реализации обязаны совпадать; совпадение стережёт
`tests/test_repo_root_marker.py`.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

#: Строгий маркер рабочей копии: `packages/core/pyproject.toml`, объявляющий
#: `name = "gc-core"`. Раньше маркером была пара «каталог packages/ +
#: pyproject.toml», и она ловила ЛЮБОЕ чужое монорепо: в проекте с
#: `packages/*/src` и нашими колёсами в .venv repo_root() отдавал ЕГО корень,
#: а ensure_repo_on_sys_path клал чужие каталоги ВПЕРЕДИ site-packages.
#: До этого маркером был `recommended_mcp/` в корне — он перестал работать,
#: когда рецепты уехали в packages/mcp.
_GC_CORE_NAME = re.compile(r'^[ \t]*name[ \t]*=[ \t]*["\']gc-core["\'][ \t]*$', re.MULTILINE)


def _declares_gc_core(candidate: Path) -> bool:
    """Лежит ли в `candidate` наш `packages/core/pyproject.toml`."""
    try:
        text = (candidate / "packages" / "core" / "pyproject.toml").read_text(
            encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _GC_CORE_NAME.search(text) is not None


def repo_root_from(start: Path) -> Path | None:
    """Корень рабочей копии вверх от `start`, иначе None.

    Отдельный вход от :func:`repo_root` нужен установщику: он ищет корень от
    каталога рецептов, который ему передали, а не от собственного ``__file__``.
    """
    for parent in Path(start).resolve().parents:
        if _declares_gc_core(parent):
            return parent
        # Дореформенные рабочие копии: до разбиения на пакеты был единый src/.
        # Ветка живая ТОЛЬКО здесь: в такой копии нет ни packages/core, ни
        # самого gc_core, и единственный, кому там нужен корень, — прямой
        # запуск install.py / run.py. В копиях маркера у ядра и тест-кита её
        # нет намеренно: в дореформенном дереве этих файлов не существует.
        if (parent / "src" / "mcp_runtime").is_dir():
            return parent
    return None


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    """Корень репозитория, или None если код запущен не из рабочей копии."""
    return repo_root_from(Path(__file__))


def ensure_repo_on_sys_path() -> Path | None:
    """Положить на `sys.path` корень репо и каталоги исходников всех пакетов."""
    root = repo_root()
    if root is None:
        return None
    candidates = [root, *sorted(root.glob("packages/*/src"))]
    legacy_src = root / "src"
    if legacy_src.is_dir():
        candidates.append(legacy_src)
    for path in candidates:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return root
