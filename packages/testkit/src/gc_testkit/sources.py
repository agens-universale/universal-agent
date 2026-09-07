"""Каталоги исходников пакетов на ``sys.path``.

Тесты гоняются по рабочей копии, а не по установленным колёсам. Раньше эту
работу делал единственный корневой ``conftest.py``; после разделения наборов по
пакетам запуск возможен и из ``packages/<имя>``, поэтому бутстрап переехал сюда
и вызывается плагином тест-кита.

Корень репозитория на ``sys.path`` НЕ кладётся. Раньше клался — «ради
``recommended_mcp``, он лежит в корне». Обоснование протухло: рецепты уехали в
``packages/mcp/src`` и попадают на путь вместе с остальными ``packages/*/src``.
Вред при этом остался: корень на ``sys.path`` делает namespace-пакетами
``docs``, ``external``, ``integrations``, ``packages`` и ``tests``, и наш
``external/`` СМЕШИВАЕТСЯ со сторонним дистрибутивом того же имени в одном
``_NamespacePath``.
"""

from __future__ import annotations

import sys

from gc_testkit.paths import package_src_dirs


def ensure_sources_on_path() -> None:
    """Положить на ``sys.path`` каталоги всех ``packages/*/src``.

    Вне рабочей копии список пуст и функция ничего не делает: плагин тест-кита
    подхватывается через ``pytest11`` в любом окружении, куда попало колесо.
    Спрашиваем функцию, а не константу ``PACKAGE_SRC_DIRS``: константа честно
    бросает, а бутстрап обязан молчать.
    """
    for path in package_src_dirs():
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
