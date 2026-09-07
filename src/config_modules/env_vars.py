"""Реестр env-переменных агента: один источник правды для конфигурации из окружения.

Singleton `env = EnvVars()` читает os.environ при импорте модуля.
Потребители делают `from config_modules import env_vars` + `env_vars.env.gc_home`
(не `from config_modules.env_vars import env`), чтобы тесты могли подменять
`env_vars.env` через monkeypatch.setattr.

Реактивность к изменениям os.environ:
- `GC_*` поля захвачены в singleton при импорте → тесты подменяют через
  `monkeypatch.setattr(env_vars, "env", EnvVars(...))`.
- `XDG_*_HOME` читаются на каждый вызов `gc_paths()` через `_xdg_or` →
  тесты могут использовать `monkeypatch.setenv` напрямую.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvVars(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Корневой override — схлопывает все 4 категории путей под один корень.
    # Удобно на пром-стенде: GC_HOME=/tmp/gc и всё внутри.
    gc_home: Path | None = None

    # Точечные overrides — переопределяют конкретную категорию.
    gc_state_dir: Path | None = None
    gc_config_dir: Path | None = None
    gc_cache_dir: Path | None = None
    gc_data_dir: Path | None = None

    @field_validator(
        "gc_home", "gc_state_dir", "gc_config_dir", "gc_cache_dir", "gc_data_dir",
        mode="before",
    )
    @classmethod
    def _empty_str_means_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "gc_home", "gc_state_dir", "gc_config_dir", "gc_cache_dir", "gc_data_dir",
        mode="after",
    )
    @classmethod
    def _require_absolute(cls, v):
        if v is not None and not v.is_absolute():
            raise ValueError(f"path must be absolute; got {str(v)!r}")
        return v

    mcp_debug: bool = False

    @field_validator("mcp_debug", mode="before")
    @classmethod
    def _coerce_mcp_debug(cls, v):
        # Сохраняем семантику текущего src/tools/connect_mcp/transport.py:
        # truthy = {"1","true","yes","y","on"}, всё остальное = False.
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "y", "on")
        return v

    server_config_path: Path | None = None

    # Legacy SWARM_* — backward-compat для серверного режима, переопределяют
    # paths.* в config_dict через apply_swarm_env_overrides (config_modules/common.py).
    swarm_agentmd_path: Path | None = None
    swarm_threads_dir: Path | None = None
    swarm_skills_dir: Path | None = None

    @field_validator(
        "server_config_path",
        "swarm_agentmd_path",
        "swarm_threads_dir",
        "swarm_skills_dir",
        mode="before",
    )
    @classmethod
    def _optional_path_empty_means_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


try:
    env = EnvVars()
except ValidationError as exc:
    # Дать пользователю читаемое сообщение вместо обскурного traceback'а
    # из глубины импорта. Чаще всего сюда приходит, когда GC_HOME или
    # GC_*_DIR заданы относительным путём.
    raise SystemExit(
        "Некорректная конфигурация env-переменных gc-cli:\n"
        f"{exc}\n"
        "Проверь GC_HOME и GC_*_DIR — они должны быть абсолютными путями."
    ) from exc


_SEP_RE = re.compile(r"[\\/:]")  # БЕЗ +, без collapse подряд идущих


def project_slug(path: str | None = None) -> str:
    """cwd → 'd--dev-sber-background-agent' (как у Claude Code).

    Каждый разделитель пути (`/`, `\\`, `:`) → один `-`. Без collapse.
    Lowercase для cross-platform consistency.
    Если slug пустой или состоит только из дефисов — возвращает `"default"`.
    """
    raw = (path if path is not None else os.getcwd()).lower()
    slug = _SEP_RE.sub("-", raw)
    if not slug.strip("-"):
        return "default"
    return slug


def _xdg_or(env_name: str, fallback: Path) -> Path:
    """XDG-helper: вернуть Path из env, или fallback. Whitespace-only → fallback."""
    raw = os.environ.get(env_name, "").strip()
    return Path(raw) if raw else fallback


def _config_dir_usable(p: Path) -> bool:
    """True, если в config-каталог можно писать (без создания каталога — чтобы не плодить
    побочные эффекты на каждом вызове).

    Корп-Mac/Linux иногда лочат ~/.config (Permission denied, либо каталог root-owned).
    Идём вверх до первого существующего предка и проверяем его на запись. Если ни один
    не writable → False, и effective_config_dir уводит config в ~/.gc.
    """
    probe = p
    while not probe.exists():
        if probe.parent == probe:   # дошли до корня — дальше некуда
            return False
        probe = probe.parent
    return os.access(probe, os.W_OK)


def gc_paths() -> dict[str, Path]:
    """Вернуть {'state': ..., 'config': ..., 'cache': ..., 'data': ...} с per-OS логикой.

    Приоритет (от сильного к слабому):
      1. Точечный GC_<TYPE>_DIR.
      2. GC_HOME (схлопывает все 4 в один корень + 'cache' подпапка).
      3. XDG_<TYPE>_HOME (только Linux/macOS).
      4. Дефолт: XDG-default на Linux/macOS, ~/.gc/ на Windows.
    """
    home = Path.home()
    is_unix = sys.platform in ("linux", "darwin")

    if env.gc_home is not None:
        root = env.gc_home
        defaults = {
            "state": root,
            "config": root,
            "cache": root / "cache",
            "data": root,
        }
    elif is_unix:
        defaults = {
            "state": _xdg_or("XDG_STATE_HOME", home / ".local" / "state") / "gc",
            "config": _xdg_or("XDG_CONFIG_HOME", home / ".config") / "gc",
            "cache": _xdg_or("XDG_CACHE_HOME", home / ".cache") / "gc",
            "data": _xdg_or("XDG_DATA_HOME", home / ".local" / "share") / "gc",
        }
    else:
        gc = home / ".gc"
        defaults = {
            "state": gc,
            "config": gc,
            "cache": gc / "cache",
            "data": gc,
        }

    return {
        "state": env.gc_state_dir or defaults["state"],
        "config": env.gc_config_dir or defaults["config"],
        "cache": env.gc_cache_dir or defaults["cache"],
        "data": env.gc_data_dir or defaults["data"],
    }


def effective_config_dir() -> Path:
    """Writable config-каталог для пользовательского config.yml и managed config.local.yml.

    Обычно = gc_paths()['config']. Но корп-Mac/Linux иногда лочат ~/.config
    (mkdir → Permission denied). Тогда, если нет явного override (GC_HOME/GC_CONFIG_DIR),
    уводим config в ~/.gc (домашняя папка writable). Единый источник для bootstrap и
    MCP-wiring → оба согласованно уезжают в ~/.gc при залоченном ~/.config.
    """
    config_dir = gc_paths()["config"]
    if env.gc_config_dir is None and env.gc_home is None and not _config_dir_usable(config_dir):
        return Path.home() / ".gc"
    return config_dir


def project_gc_dir(cwd: str | None = None) -> Path:
    """Папка project-local артефактов: <cwd>/.gc/.

    Единый source of truth для всех проектных `.gc/`-путей (config overlay,
    project skills, long memory). Резолвит cwd на каждый вызов — симметрично
    с инлайн `os.path.join(os.getcwd(), '.gc', ...)`, который этот хелпер заменяет.
    """
    return Path(cwd or os.getcwd()) / ".gc"


def ensure_project_gc_dir(cwd: str | None = None) -> None:
    """Создать <cwd>/.gc/ (идемпотентно, exist_ok=True). Подпапку skills/ НЕ
    создаёт — её добавляет пользователь по необходимости. На PermissionError/
    OSError тихо пропускает: не блокируем запуск из-за прав на запись в cwd.

    Общий стартовый хелпер для gc-cli и gc-ua (project-local слой: config overlay,
    project skills, long memory)."""
    try:
        os.makedirs(project_gc_dir(cwd), exist_ok=True)
    except (PermissionError, OSError):
        pass


def project_local_config_path() -> Optional[str]:
    """Абсолютный путь к `<cwd>/.gc/config.yml`, если файл есть, иначе None.

    Живёт рядом с `project_gc_dir`, чей ответ и проверяет: это вопрос раскладки
    каталогов, а не загрузки конфига. Слой наложения (`config_modules.layers`)
    и потребители снаружи спрашивают отсюда."""
    candidate = str((project_gc_dir() / "config.yml").resolve())
    return candidate if os.path.isfile(candidate) else None

