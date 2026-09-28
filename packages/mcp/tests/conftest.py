"""Фикстуры тестов дистрибутива gc-mcp.

Константы раскладки живут в ``gc_testkit.paths`` (RECIPES, VENDOR), а НЕ здесь.
Импортировать их из conftest нельзя: каталогов ``tests`` в репозитории шесть,
модуль ``conftest`` у них общий по имени, и в полном прогоне
``from conftest import ...`` цепляет чужой файл — наблюдалось 18.08.2026,
сборка падала с ImportError из conftest пакета TUI.
"""

import pytest
from gc_testkit.paths import RECIPES


@pytest.fixture
def recipes_dir():
    return RECIPES
