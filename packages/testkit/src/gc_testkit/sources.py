"""Каталоги исходников пакетов на ``sys.path``.

Тесты гоняются по рабочей копии, а не по установленным колёсам. Раньше эту
работу делал единственный корневой ``conftest.py``; после разделения наборов по
пакетам запуск возможен и из ``packages/<имя>``, поэтому бутстрап переехал сюда
и вызывается плагином тест-кита.
"""

from __future__ import annotations

import sys

from .paths import PACKAGE_SRC_DIRS, REPO


def ensure_sources_on_path() -> None:
    """Положить на ``sys.path`` каталоги всех ``packages/*/src`` и корень репо.

    Корень нужен ради ``recommended_mcp`` — это отдельный импортируемый пакет,
    он лежит в корне репозитория, а не внутри ``packages/``.
    """
    for path in (*PACKAGE_SRC_DIRS, REPO):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
