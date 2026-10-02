import os
from typing import Dict

from gc_core.config_modules import env_vars


def resolve_path_from_config_dir(path_value: str, config_dir: str) -> str:
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(config_dir, path_value)


#: SWARM_* окружения → путь настройки (паритет с raw-YAML веткой swarm runner'а).
_SWARM_ENV_PATHS = (
    ("swarm_agentmd_path", "paths.agent_md_path"),
    ("swarm_threads_dir", "paths.threads_data_path"),
    ("swarm_skills_dir", "paths.skills_data_path"),
)


def swarm_env_updates(config_dir: str) -> Dict[str, str]:
    """Правки настроек из SWARM_* окружения: ``{"paths.threads_data_path": abs}``.

    Пустое значение не переопределяет; относительный путь — от каталога конфига.
    """
    updates: Dict[str, str] = {}
    for attr, dotted in _SWARM_ENV_PATHS:
        value = getattr(env_vars.env, attr)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            updates[dotted] = resolve_path_from_config_dir(value, config_dir)
    return updates
