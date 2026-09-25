"""Набор не видит конфиг и данные того, кто его запускает.

Базовый слой конфига (`~/.gc/config.yml`) раньше читался как есть: у
разработчика, который пользуется агентом, набор краснел на его личных ключах —
конфиг от более новой сборки валил `import gc_server.server` через SystemExit и
обрывал сбор целого каталога. Рецепт «гонять с пустым GC_HOME» жил в памяти
людей; теперь его делает плагин тест-кита.

Подмена: убрать изоляцию из `pytest_load_initial_conftests` — GC_HOME вернётся
к окружению запускающего, и первая проверка покраснеет.
"""

import os
from pathlib import Path

from gc_core.config_modules import env_vars


def test_gc_home_is_an_empty_directory_of_the_session():
    home = Path(os.environ["GC_HOME"])
    assert home.name.startswith("gc-test-home-"), home
    assert env_vars.env.gc_home == home


def test_point_overrides_do_not_leak_past_gc_home():
    for name in (
        "GC_STATE_DIR",
        "GC_CONFIG_DIR",
        "GC_CACHE_DIR",
        "GC_DATA_DIR",
    ):
        assert name not in os.environ, name
