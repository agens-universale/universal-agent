"""Поиск YAML-манифеста MCP без привязки к cwd агента."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _manifest_errors():
    from recommended_mcp.manifest import ManifestError

    return ManifestError


def recipe_root() -> Path:
    """Каталог рецептов — он же каталог пакета.

    Жила в ``gc_core.mcp_runtime.paths`` и ходила туда через охранник импорта:
    ядро аккуратно спрашивало, установлен ли пакет рецептов. Теперь функция
    внутри самого пакета, и вопрос отпал — считаем от ``__file__``.
    """
    import recommended_mcp

    return Path(recommended_mcp.__file__).resolve().parent


def _default_manifest_path(root: Path) -> Path:
    from recommended_mcp.manifest import default_manifest_path

    return default_manifest_path(root)


def _gc_config_recommended_dir() -> Path:
    from gc_core.config_modules.env_vars import effective_config_dir

    return effective_config_dir() / "recommended_mcp"


def resolve_agent_manifest_path(
    config_dict: dict[str, Any],
    *,
    cwd: Path | None = None,
) -> Path | None:
    """
    Манифест для gc-cli -a и install по умолчанию.

    Приоритет:
      1. mcp-meta.recommended-mcp-manifest (абсолютный или относительно gc config)
      2. <gc_config>/recommended_mcp/config.local.yml
      3. <gc_config>/recommended_mcp/config.yml
      4. встроенный recommended_mcp/config.yml из пакета
    """
    del cwd  # cwd больше не используется — агент может работать из любой папки

    from gc_core.settings import settings_of

    raw = settings_of(config_dict).mcp.meta.recommended_mcp_manifest
    if raw:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            from gc_core.config_modules.env_vars import effective_config_dir

            path = effective_config_dir() / path
        resolved = path.resolve()
        return resolved if resolved.is_file() else None

    for candidate in (
        _gc_config_recommended_dir() / "config.local.yml",
        _gc_config_recommended_dir() / "config.yml",
    ):
        if candidate.is_file():
            return candidate.resolve()

    bundled = _default_manifest_path(recipe_root())
    return bundled if bundled.is_file() else None


def resolve_install_manifest_path(config: Path | str | None) -> Path:
    """Манифест для install.py / gc-mcp-install."""
    ManifestError = _manifest_errors()
    if config is not None:
        path = Path(config).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if not path.is_file():
            raise ManifestError(f"manifest not found: {path}")
        return path

    resolved = resolve_agent_manifest_path({})
    if resolved is None:
        raise ManifestError(
            f"manifest not found: задай --config PATH или положи config.local.yml в {_gc_config_recommended_dir()}"
        )
    return resolved
