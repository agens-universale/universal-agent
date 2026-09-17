import os
from typing import Any, Dict, Optional

from gc_core.config_modules import env_vars


def resolve_path_from_config_dir(path_value: str, config_dir: str) -> str:
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(config_dir, path_value)


def maybe_override_and_resolve(
    maybe_env_value: Optional[str], key: str, config_dict: Dict[str, Any], config_dir: str
) -> None:
    if not isinstance(maybe_env_value, str):
        return
    env_value = maybe_env_value.strip()
    if not env_value:
        return
    config_dict[key] = resolve_path_from_config_dir(env_value, config_dir)


def apply_swarm_env_overrides(config_dict: Dict[str, Any], config_dir: str) -> None:
    if env_vars.env.swarm_agentmd_path is not None:
        maybe_override_and_resolve(
            str(env_vars.env.swarm_agentmd_path),
            "agent_md_path", config_dict, config_dir,
        )
    if env_vars.env.swarm_threads_dir is not None:
        maybe_override_and_resolve(
            str(env_vars.env.swarm_threads_dir),
            "threads_data_path", config_dict, config_dir,
        )
    if env_vars.env.swarm_skills_dir is not None:
        maybe_override_and_resolve(
            str(env_vars.env.swarm_skills_dir),
            "skills_data_path", config_dict, config_dir,
        )
