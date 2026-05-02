import os
from typing import Any, Dict, Optional


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
    maybe_override_and_resolve(
        os.environ.get("SWARM_AGENTMD_PATH"), "agent_md_path", config_dict, config_dir
    )
    maybe_override_and_resolve(
        os.environ.get("SWARM_THREADS_DIR"), "threads_data_path", config_dict, config_dir
    )
    maybe_override_and_resolve(
        os.environ.get("SWARM_SKILLS_DIR"), "skills_data_path", config_dict, config_dir
    )
