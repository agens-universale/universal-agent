"""Модель config_server.yml: поверхности сервера, их адреса и слои агента.

Файл описывает процесс сервера: какие поверхности он поднимает (webhook, chat,
a2a, swarm), где они слушают, их токены и правки настроек агента поверх
``config.yml``. Схема строгая: неизвестный ключ — отказ старта, прежний ключ —
отказ с указанием нового места.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import yaml
from gc_core.bundled_assets import bundled_root, repo_root
from gc_core.config import apply_cli_overrides
from gc_core.config_modules.env_interpolation import (
    resolve_env_placeholders_strict,
)
from gc_core.config_modules.path_hints import UnknownSettingError
from gc_core.config_modules.root import schema_error_details
from gc_core.settings import LoadedSettings, expand_section_updates
from gc_core.utils.user_text import read_user_text
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

logger = logging.getLogger(__name__)

CONFIG_NAME = "config_server.yml"
SURFACES = ("webhook", "chat", "a2a", "swarm")
SurfaceName = Literal["webhook", "chat", "a2a", "swarm"]
#: system других поверхностей и интерактивных сессий (gc-cli, TUI): вебхук и
#: swarm их треды не читают и в них не пишут.
RESERVED_SYSTEMS = frozenset(
    {"a2a", "responses", "completions", "acp", "cli", "tui"}
)
#: Адрес по умолчанию у фасадов, собранных без конфига (тесты, встраивание).
LOOPBACK = "127.0.0.1"

#: Прежний ключ → где он теперь (полный путь в config_server.yml).
_MOVED_TOP = {
    "defaults": "webhook.default_system",
    "agent": "webhook.agent_md_path",
    "threads_cleanup": "webhook.threads_cleanup",
    "logging": "server.logging",
}
_MOVED_NESTED = {
    ("server", "context_build_wait_seconds"): (
        "webhook.context_build_wait_seconds"
    ),
    ("swarm", "enabled"): "server.apis",
}

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ServerConfigError(ValueError):
    """config_server.yml не годится: текст называет ключ и что сделать."""


class SurfaceConfig(BaseModel):
    """Общие ключи поверхности; ``host`` и ``auth_token`` — по умолчанию из
    секции ``server``."""

    model_config = _STRICT

    host: str | None = None
    port: int = Field(default=0, ge=0, le=65535)
    auth_token: str | None = None
    agent_overrides: dict[str, Any] = Field(default_factory=dict)
    agent_md_path: str | None = None

    @field_validator("host")
    @classmethod
    def _host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if text == "":
            raise ValueError("host должен быть непустой строкой")
        return text


class ThreadsCleanupConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = False
    interval_seconds: int = Field(default=3600, gt=0)
    ttl_days: float = Field(default=7, gt=0)


class WebhookConfig(SurfaceConfig):
    port: int = Field(default=8000, ge=1, le=65535)
    default_system: str = "generic"
    #: Роль агента вебхука: относительный путь — рядом с этим файлом, затем
    #: среди ассетов gc-core.
    agent_md_path: str | None = "./prompts/server_agent.md"
    context_build_wait_seconds: float = Field(
        default=120, gt=0, allow_inf_nan=False
    )
    threads_cleanup: ThreadsCleanupConfig = Field(
        default_factory=ThreadsCleanupConfig
    )

    @field_validator("context_build_wait_seconds", mode="before")
    @classmethod
    def _wait(cls, value: object) -> object:
        # `true` в YAML иначе стал бы потолком в секунду.
        if isinstance(value, bool):
            raise ValueError("нужно положительное конечное число секунд")
        return value


class _ChannelModes(SurfaceConfig):
    launch_root: str | None = None
    text_answer_mode: Literal["deny", "judge", "allow"] = "allow"
    finish_gate_mode: Literal["on", "off", "smart"] = "off"
    todo_reminder_steps: int = Field(default=0, ge=0)

    @field_validator("launch_root", mode="before")
    @classmethod
    def _launch_root(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("launch_root должен быть строкой или null")
        return value.strip() or None


class ChatConfig(_ChannelModes):
    port: int = Field(default=8090, ge=1, le=65535)
    #: Явный список origin; ``*`` отбрасывается — чужой сайт в браузере.
    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("cors_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        return [item for item in value if item != "*"]


class A2aConfig(_ChannelModes):
    port: int = Field(default=8091, ge=1, le=65535)
    #: URL в карточке; нужен, когда сервер слушает 0.0.0.0/::.
    public_url: str | None = None
    name: str = "GigaCode"
    description: str = "Универсальный агент GigaCode"

    @field_validator("public_url", mode="before")
    @classmethod
    def _public_url(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("public_url должен быть строкой или null")
        text = value.strip()
        if text == "":
            return None
        if not text.startswith(("http://", "https://")):
            raise ValueError(
                "public_url должен начинаться с http:// или https://"
            )
        return text

    @field_validator("name", "description")
    @classmethod
    def _text(cls, value: str) -> str:
        if value.strip() == "":
            raise ValueError("name и description не бывают пустыми")
        return value


class SwarmConfig(SurfaceConfig):
    """Ключи песочницы (лимиты, каталоги, обёртка…) проверяет
    ``swarm.config.build_swarm_config``; роль агента в песочнице — свой ключ
    ``agentmd_path``."""

    model_config = ConfigDict(extra="allow", frozen=True)

    port: int = Field(default=8092, ge=1, le=65535)


class ServerSection(BaseModel):
    model_config = _STRICT

    host: str = "127.0.0.1"
    auth_token: str | None = None
    apis: list[SurfaceName] = Field(default_factory=lambda: ["webhook"])
    log_level: str = "info"
    #: Правки ``logging.*`` агента.
    logging: dict[str, Any] = Field(default_factory=dict)


class ServerConfig(BaseModel):
    model_config = _STRICT

    server: ServerSection = Field(default_factory=ServerSection)
    agent_overrides: dict[str, Any] = Field(default_factory=dict)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    a2a: A2aConfig = Field(default_factory=A2aConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    #: Откуда прочитан ("" — файла нет, взяты умолчания); в файле не задаётся.
    config_path: str = ""

    def surface(self, name: str) -> SurfaceConfig:
        """Секция поверхности с унаследованными ``host`` и ``auth_token``."""
        section: SurfaceConfig = getattr(self, _known(name))
        return section.model_copy(
            update={
                "host": section.host or self.server.host,
                "auth_token": (
                    section.auth_token
                    if section.auth_token is not None
                    else self.server.auth_token
                ),
            }
        )

    def address(self, name: str) -> tuple[str, int]:
        section = self.surface(name)
        return str(section.host), section.port

    def config_dir(self) -> str:
        if self.config_path:
            return os.path.dirname(os.path.abspath(self.config_path))
        return os.getcwd()

    def swarm_section(self) -> dict[str, Any]:
        """Секция swarm в форме, которую читает ``build_swarm_config``:
        правки агента — общие, затем свои."""
        raw = self.swarm.model_dump(exclude={"host", "port", "auth_token"})
        own = raw.pop("agent_overrides", {}) or {}
        raw.pop("agent_md_path", None)
        return {
            "swarm": {**raw, "enabled": True},
            "agent_overrides": {
                **expand_section_updates(self.agent_overrides),
                **expand_section_updates(own),
            },
            "config_path": self.config_path,
        }


def _known(name: str) -> str:
    if name not in SURFACES:
        raise ServerConfigError(
            f"неизвестная поверхность {name!r}: допустимы {', '.join(SURFACES)}"
        )
    return name


def _candidates() -> list[str]:
    """Где искать config_server.yml без явного пути: каталог запуска, корень
    рабочей копии, ассеты (исторический путь)."""
    found = [os.path.join(os.getcwd(), CONFIG_NAME)]
    root = repo_root()
    if root is not None:
        found.append(str(root / CONFIG_NAME))
    found.append(os.path.join(str(bundled_root()), CONFIG_NAME))
    seen: set[str] = set()
    return [c for c in found if not (c in seen or seen.add(c))]


def _explicit_path(path: str | None) -> str | None:
    if path:
        return path
    from gc_core.config_modules import env_vars

    env_path = env_vars.env.server_config_path
    return None if env_path is None else str(env_path)


def load_server_config(path: str | None = None) -> ServerConfig:
    """Прочитать и проверить config_server.yml.

    ``path`` (флаг ``--server-config``) или ``SERVER_CONFIG_PATH`` — явный
    файл: его отсутствие — отказ. Без явного пути — поиск по кандидатам; не
    нашёлся ни один — умолчания (``config_path == ""``).
    """
    explicit = _explicit_path(path)
    if explicit is not None:
        if not os.path.exists(explicit):
            raise ServerConfigError(f"{CONFIG_NAME} не найден: {explicit}")
        cfg_path = explicit
    else:
        searched = _candidates()
        cfg_path = next((c for c in searched if os.path.exists(c)), "")
        if not cfg_path:
            logger.info(
                "%s не найден (просмотрены: %s) — беру умолчания",
                CONFIG_NAME,
                ", ".join(searched),
            )
            return ServerConfig()
    logger.info("%s: читаю %s", CONFIG_NAME, cfg_path)
    try:
        raw = yaml.safe_load(read_user_text(cfg_path)) or {}
        raw = resolve_env_placeholders_strict(raw, source=str(cfg_path))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ServerConfigError(f"{cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ServerConfigError(f"{cfg_path}: ожидался YAML-словарь")
    _reject_moved_keys(raw, cfg_path)
    try:
        cfg = ServerConfig.model_validate(raw)
    except ValidationError as exc:
        raise ServerConfigError(
            f"{cfg_path}: {schema_error_details(exc)}"
        ) from None
    return cfg.model_copy(update={"config_path": cfg_path})


def _reject_moved_keys(raw: dict[str, Any], cfg_path: str) -> None:
    for key, new in _MOVED_TOP.items():
        if key in raw:
            raise ServerConfigError(
                f"{cfg_path}: ключ «{key}» переехал — теперь {new}"
            )
    for (section, key), new in _MOVED_NESTED.items():
        block = raw.get(section)
        if isinstance(block, dict) and key in block:
            raise ServerConfigError(
                f"{cfg_path}: ключ «{section}.{key}» переехал — теперь {new}"
            )


# --- настройки агента поверхности -------------------------------------------


#: Секции config.yml, переехавшие в config_server.yml: поверхность → секция.
_MOVED_SECTIONS = {"chat": "chat_api", "a2a": "a2a"}


def reject_moved_section(base: LoadedSettings, name: str) -> None:
    """Старая секция поверхности в config.yml больше не читается: молча
    подняться без неё значило бы подняться без её ``auth_token`` — отказ."""
    section = _MOVED_SECTIONS.get(name)
    if section is None or not base.provenance.explicit(section):
        return
    where = base.provenance.source(section).path or "config.yml"
    raise ServerConfigError(
        f"{where}: секция {section} больше не читается (в том числе её "
        f"auth_token) — перенесите её в {CONFIG_NAME}, секция {name}, и "
        "удалите из config.yml"
    )


def surface_settings(
    base: LoadedSettings, cfg: ServerConfig, name: str
) -> LoadedSettings:
    """Настройки агента поверхности: ``base`` (config.yml, профиль, env,
    флаги) → ``server.logging`` → роль поверхности → общий
    ``agent_overrides`` → ``agent_overrides`` поверхности."""
    section = getattr(cfg, _known(name))
    updates = {**_logging_updates(cfg.server.logging)}
    if name != "swarm" and section.agent_md_path:
        updates["paths.agent_md_path"] = _resolve_role(
            section.agent_md_path, cfg, f"{name}.agent_md_path"
        )
    loaded = base.with_overrides(updates, layer="server") if updates else base
    loaded = _apply_overrides(loaded, cfg.agent_overrides, "agent_overrides")
    return _apply_overrides(
        loaded, section.agent_overrides, f"{name}.agent_overrides"
    )


def _apply_overrides(
    loaded: LoadedSettings, overrides: dict[str, Any], label: str
) -> LoadedSettings:
    """Правки тем же путём, что флаги CLI: их ``agent.model`` видит
    легаси-форс как заданный явно."""
    if not overrides:
        return loaded
    try:
        return apply_cli_overrides(
            loaded,
            extra_overrides=expand_section_updates(overrides),
            param_prefix="",
        )
    except UnknownSettingError as exc:
        raise ServerConfigError(f"{label}: {exc}") from None
    except ValidationError as exc:
        # Координата и причина без значения: в нём может быть токен.
        raise ServerConfigError(
            f"{label}: {schema_error_details(exc)}"
        ) from None


def _logging_updates(block: dict[str, Any]) -> dict[str, Any]:
    if not block:
        return {}
    from gc_core.config_modules.logging_config import LoggingConfig
    from gc_core.config_modules.unknown_keys import (
        drop_unknown_keys,
        unknown_key_notice,
    )

    # Лишний ключ не роняет старт (как в YAML агента): отбрасывается с
    # предупреждением.
    block, unknown = drop_unknown_keys(block, LoggingConfig)
    for item in unknown:
        logger.warning(
            unknown_key_notice(item, f"{CONFIG_NAME}: server.logging")
        )
    return {f"logging.{key}": value for key, value in block.items()}


def _resolve_role(raw: str, cfg: ServerConfig, key: str) -> str:
    """Относительный путь роли — СНАЧАЛА рядом с config_server.yml, затем
    среди ассетов gc-core. Не нашёлся нигде — громкое предупреждение: иначе
    агент молча работал бы без системных инструкций."""
    path = raw.strip()
    if os.path.isabs(path):
        return path
    local = os.path.normpath(os.path.join(cfg.config_dir(), path))
    if os.path.isfile(local):
        return local
    bundled = os.path.normpath(os.path.join(str(bundled_root()), path))
    if os.path.isfile(bundled):
        return bundled
    logger.warning(
        "%s=%r не найден (искал %s и %s) — агент будет работать БЕЗ "
        "системных инструкций",
        key,
        raw,
        local,
        bundled,
    )
    return local


__all__ = [
    "CONFIG_NAME",
    "LOOPBACK",
    "RESERVED_SYSTEMS",
    "SURFACES",
    "A2aConfig",
    "ChatConfig",
    "ServerConfig",
    "ServerConfigError",
    "SurfaceConfig",
    "SwarmConfig",
    "WebhookConfig",
    "load_server_config",
    "reject_moved_section",
    "surface_settings",
]
