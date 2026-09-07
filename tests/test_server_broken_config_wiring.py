"""server.py грузит конфиг НА ИМПОРТЕ (module-level SERVER_CONFIG/BASE_AGENT_CONFIG) —
сломанный конфиг не должен ронять импорт голым traceback'ом, а должен дать
понятный SystemExit, тем же приёмом, что config_modules/env_vars.py уже
применяет к битым GC_*-переменным окружения.

Изоляция от хостового config.yml — тем же приёмом, что tests/test_swarm_endpoints.py
(сброс sys.modules['server'] + патч rest_api.server_config.* ДО импорта).
"""
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi", reason="server-side dep, install via server_requirements.txt")
pytest.importorskip("starlette", reason="server-side dep, install via server_requirements.txt")


def _reset_server_module():
    for mod in list(sys.modules):
        if mod == "server" or mod.startswith("server."):
            del sys.modules[mod]


def _minimal_server_config():
    return {
        "server": {}, "defaults": {"system": "generic"}, "agent": {},
        "agent_overrides": {}, "logging": {}, "swarm": {}, "threads_cleanup": {},
    }


def test_import_server_exits_cleanly_when_agent_config_broken():
    """build_agent_config_for_server (rest_api/server_config.py:93) падает на
    сломанном config.yml агента — импорт server должен завершиться SystemExit
    с текстом причины, а не поднять ValueError наружу."""
    _reset_server_module()
    try:
        with patch(
            "rest_api.server_config.load_server_config",
            return_value=_minimal_server_config(),
        ), patch(
            "rest_api.server_config.build_agent_config_for_server",
            side_effect=ValueError('C:\\gc\\config.yml: невалидный YAML: boom'),
        ):
            with pytest.raises(SystemExit) as exc_info:
                import server  # noqa: F401
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
            "rest_api.server_config.load_server_config",
            side_effect=yaml.YAMLError("boom"),
        ):
            with pytest.raises(SystemExit):
                import server  # noqa: F401
    finally:
        _reset_server_module()
