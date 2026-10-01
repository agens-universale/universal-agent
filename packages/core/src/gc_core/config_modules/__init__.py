"""Public exports loaded on demand; pure configuration/policy stays I/O-free."""

from importlib import import_module

_BACKEND = "gc_core.config_modules.backend"
_ENV = "gc_core.config_modules.env_vars"

_EXPORTS = {
    "build_auth_connector": (
        "gc_core.config_modules.auth",
        "build_auth_connector",
    ),
    "DEFAULT_REQUEST_TIMEOUT_SEC": (
        _BACKEND,
        "DEFAULT_REQUEST_TIMEOUT_SEC",
    ),
    "BackendConfig": (_BACKEND, "BackendConfig"),
    "parse_chat_completions_max_tokens": (
        _BACKEND,
        "parse_chat_completions_max_tokens",
    ),
    "EnvVars": (_ENV, "EnvVars"),
    "env": (_ENV, "env"),
    "gc_paths": (_ENV, "gc_paths"),
    "project_slug": (_ENV, "project_slug"),
    "AppConfig": ("gc_core.config_modules.root", "AppConfig"),
    "resolve_paths": ("gc_core.config_modules.root", "resolve_paths"),
    "SecurityConfig": ("gc_core.config_modules.security", "SecurityConfig"),
    "apply_cli_command_moderation_overrides": (
        "gc_core.config_modules.security",
        "apply_cli_command_moderation_overrides",
    ),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
