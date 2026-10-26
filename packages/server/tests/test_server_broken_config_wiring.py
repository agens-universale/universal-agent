"""server.py грузит конфиг НА ИМПОРТЕ (module-level SERVER_CONFIG/BASE_AGENT_SETTINGS) —
сломанный конфиг не должен ронять импорт голым traceback'ом, а должен дать
понятный SystemExit, тем же приёмом, что config_modules/env_vars.py уже
применяет к битым GC_*-переменным окружения.

Изоляция от хостового config.yml — тем же приёмом, что tests/test_swarm_endpoints.py
(сброс sys.modules['server'] + патч rest_api.server_config.* ДО импорта).
"""

import sys
from unittest.mock import patch

import pytest

pytest.importorskip(
    "fastapi", reason="server-side dep, install via server_requirements.txt"
)
pytest.importorskip(
    "starlette", reason="server-side dep, install via server_requirements.txt"
)


def _reset_server_module():
    # Полное имя И сам ключ: `startswith("gc_server.server.")` с точкой на конце
    # ловит только подмодули, которых нет, а `"server"` — дореформенное плоское
    # имя. С таким условием сброс был no-op: модуль, импортированный соседним
    # тестом, переживал его, патчи ниже не срабатывали и импорт SystemExit не
    # давал. Обособленно файл при этом зеленел — тот же промах чинился в
    # tests/test_swarm_endpoints.py (девятый раунд ревью).
    for mod in list(sys.modules):
        if mod == "gc_server.server" or mod.startswith("gc_server.server."):
            sys.modules.pop(mod, None)

    # Одного sys.modules мало: `from gc_server import server` отдаёт АТРИБУТ
    # пакета, а он выставляется при первом импорте и переживает удаление ключа.
    # Пока атрибут на месте, import ниже возвращает старый модуль, конфиг на
    # импорте не перечитывается, и SystemExit не случается — тест падал
    # «DID NOT RAISE», причём только когда сосед по каталогу успел импортировать
    # server раньше. До разбиения на пакеты вопроса не было: `server` был
    # модулем верхнего уровня, без родителя.
    parent = sys.modules.get("gc_server")
    if parent is not None and hasattr(parent, "server"):
        delattr(parent, "server")


def _minimal_server_config():
    return {
        "server": {},
        "defaults": {"system": "generic"},
        "agent": {},
        "agent_overrides": {},
        "logging": {},
        "swarm": {},
        "threads_cleanup": {},
    }


def test_import_server_exits_cleanly_when_agent_config_broken():
    """build_agent_settings_for_server (rest_api/server_config.py) падает на
    сломанном config.yml агента — импорт server должен завершиться SystemExit
    с текстом причины, а не поднять ValueError наружу."""
    _reset_server_module()
    try:
        with (
            patch(
                "gc_server.rest_api.server_config.load_server_config",
                return_value=_minimal_server_config(),
            ),
            patch(
                "gc_server.rest_api.server_config.build_agent_settings_for_server",
                side_effect=ValueError(
                    "C:\\gc\\config.yml: невалидный YAML: boom"
                ),
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                from gc_server import server  # noqa: F401  # сам импорт и есть предмет проверки
        assert "невалидный YAML" in str(exc_info.value)
    finally:
        _reset_server_module()


def test_import_server_exits_cleanly_when_server_config_broken():
    """load_server_config (config_server.yml) сам не защищён (голый open/yaml.
    safe_load) и может дать сырой yaml.YAMLError — импорт server всё равно
    обязан завершиться SystemExit, не трейсбеком."""
    import yaml

    _reset_server_module()
    try:
        with patch(
            "gc_server.rest_api.server_config.load_server_config",
            side_effect=yaml.YAMLError("boom"),
        ):
            with pytest.raises(SystemExit):
                from gc_server import server  # noqa: F401  # сам импорт и есть предмет проверки
    finally:
        _reset_server_module()
