"""Парсер recommended_mcp/config.yml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    """Ошибка в структуре config.yml."""


@dataclass(frozen=True)
class InstallSpec:
    type: str
    platforms: tuple[str, ...] = tuple()
    # Для type=npx: команды post-ensure для прогрева npm-кэша/расширений.
    # Каждая запись — argv-список; вызывается как subprocess.run(cmd, check=True).
    commands: tuple[tuple[str, ...], ...] = tuple()
    # Вопреки типу — ИМЯ каталога в external/vendor/, а не путь: см. докстринг
    # `resolve_install_manifest_path` и `install._source_dir_name`. Тип оставлен
    # `Path` ради дореформенных манифестов, где здесь и правда лежал путь.
    source: Path | None = None
    # Для type=pypi: pip-спецификация, например ddgs-mcp==0.1.0
    package: str | None = None
    # Для type=npx: имя npm-пакета для `npm uninstall -g` (деинсталл). Деривировать
    # из install-команд ненадёжно (scoped-имена + версия), поэтому объявляется явно.
    npm_package: str | None = None
    # Имя python-дистрибутива (для проверки установки/деинсталла), source-dir-независимо.
    # Для pypi имя берётся из package; для source — объявляется здесь явно.
    dist: str | None = None
    # Версия MCP в ЭТОМ релизе агента (манифест едет вместе с агентом). Расхождение с
    # установленной = «есть что обновлять» — локальный признак, без сети. Для source/wheel
    # задаётся явно и держится в синке с external/<name>/pyproject.toml (тест это стережёт).
    version: str | None = None


@dataclass(frozen=True)
class RunSpec:
    type: str
    detach: bool = False
    # Для type=command: argv daemon-команды (science-mcp --transport http и т.п.)
    command: str | None = None
    args: tuple[str, ...] = tuple()
    env: tuple[tuple[str, str], ...] = tuple()
    cwd: Path | None = None


@dataclass(frozen=True)
class MCPEntry:
    name: str
    path: Path
    install: InstallSpec
    run: RunSpec | None = None
    optional: bool = False
    description: str = ""


def default_manifest_path(root: Path) -> Path:
    """Путь до дефолтного манифеста `recommended_mcp/config.yml`."""
    return root / "config.yml"


def resolve_manifest_path(root: Path, config: Path | str | None) -> Path:
    """Разрешить путь до YAML-манифеста.

    Если `config` не задан — `root/config.yml`.
    Относительные пути считаются от текущей рабочей директории (cwd), не от `root`.
    `path:` внутри манифеста по-прежнему относителен `root`. А `install.source` —
    НЕ путь: это ИМЯ каталога в `external/vendor/`, которое ищет
    `install._vendor_candidates`. Относительная форма (`../external/<имя>`)
    принимается ради дореформенных манифестов, но с одноразовым `[warn]`: путь
    резолвился от каталога рецептов, а после их переезда в `packages/mcp/src`
    стал вести в никуда.
    """
    if config is None:
        return default_manifest_path(root)
    path = Path(config).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    return path


def load_manifest(path: Path) -> list[MCPEntry]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_mcps = data.get("mcps", [])
    if not isinstance(raw_mcps, list):
        raise ManifestError(f"'mcps' must be a list in {path}")
    return [_parse_entry(m, path) for m in raw_mcps]


def default_agent_dep_dir() -> Path:
    """Корень для Node/npx: `gc_paths()['data']` (XDG на Linux, `~/.gc` на Windows)."""
    from gc_core.config_modules.env_vars import gc_paths

    return gc_paths()["data"]


def load_agent_dep_dir(path: Path) -> Path:
    """
    Прочитать top-level `agent_dep_dir` из config.yml.

    Если ключ не задан — `default_agent_dep_dir()` (`gc_paths()['data']`).
    Куда install.py распакует Node под `<agent_dep_dir>/node/vX.Y.Z/`.
    Принимает `~`, относительные и абсолютные пути; возвращает абсолютный resolved.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("agent_dep_dir")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default_agent_dep_dir()
    if not isinstance(raw, str):
        raise ManifestError(
            f"'agent_dep_dir' must be a string in {path}, got {type(raw).__name__}"
        )
    return Path(raw).expanduser().resolve()


def load_repo_url(path: Path) -> tuple[str | None, str | None]:
    """Прочитать top-level repo_url / repo_ref из манифеста (для git-установки
    source-MCP в method-2). Отсутствуют → (None, None)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_url = data.get("repo_url")
    raw_ref = data.get("repo_ref")
    url = str(raw_url).strip() if raw_url and str(raw_url).strip() else None
    ref = str(raw_ref).strip() if raw_ref and str(raw_ref).strip() else None
    return url, ref


def _parse_entry(raw: dict[str, Any], source: Path) -> MCPEntry:
    name = raw.get("name")
    if not name:
        raise ManifestError(f"mcp entry missing 'name' in {source}")
    path_str = raw.get("path")
    if not path_str:
        raise ManifestError(f"mcp '{name}' missing 'path' in {source}")
    install_raw = raw.get("install")
    if not isinstance(install_raw, dict):
        raise ManifestError(f"mcp '{name}' missing 'install' block in {source}")
    source_raw = install_raw.get("source")
    install_source = Path(source_raw) if source_raw else None
    package_raw = install_raw.get("package")
    install_package = str(package_raw).strip() if package_raw else None
    npm_package_raw = install_raw.get("npm_package")
    install_npm_package = str(npm_package_raw).strip() if npm_package_raw else None
    dist_raw = install_raw.get("dist")
    install_dist = str(dist_raw).strip() if dist_raw else None
    version_raw = install_raw.get("version")
    install_version = str(version_raw).strip() if version_raw else None
    raw_commands = install_raw.get("commands", ()) or ()
    commands = tuple(tuple(str(arg) for arg in cmd) for cmd in raw_commands)

    install = InstallSpec(
        type=install_raw.get("type", ""),
        platforms=tuple(install_raw.get("platforms", ()) or ()),
        commands=commands,
        source=install_source,
        package=install_package,
        npm_package=install_npm_package,
        dist=install_dist,
        version=install_version,
    )
    if not install.type:
        raise ManifestError(f"mcp '{name}' missing 'install.type' in {source}")
    if install.type == "source" and install.source is None:
        raise ManifestError(
            f"mcp '{name}' install.type=source requires install.source in {source}"
        )
    if install.type == "pypi" and not install.package:
        raise ManifestError(
            f"mcp '{name}' install.type=pypi requires install.package in {source}"
        )

    run_raw = raw.get("run")
    run: RunSpec | None = None
    if run_raw is not None:
        if not isinstance(run_raw, dict):
            raise ManifestError(f"mcp '{name}' 'run' must be a dict in {source}")
        run_type = run_raw.get("type")
        if not run_type:
            raise ManifestError(f"mcp '{name}' missing 'run.type' in {source}")
        run_cwd_raw = run_raw.get("cwd")
        run_cwd = Path(run_cwd_raw) if run_cwd_raw else None
        raw_args = run_raw.get("args", ()) or ()
        run_args = tuple(str(arg) for arg in raw_args)
        raw_env = run_raw.get("env", {}) or {}
        if not isinstance(raw_env, dict):
            raise ManifestError(f"mcp '{name}' run.env must be a dict in {source}")
        run_env = tuple((str(k), str(v)) for k, v in raw_env.items())
        run_command = run_raw.get("command")
        run_command_str = str(run_command).strip() if run_command else None
        run = RunSpec(
            type=run_type,
            detach=bool(run_raw.get("detach", False)),
            command=run_command_str,
            args=run_args,
            env=run_env,
            cwd=run_cwd,
        )
        if run.type == "command" and not run.command:
            raise ManifestError(
                f"mcp '{name}' run.type=command requires run.command in {source}"
            )

    description = str(raw.get("description", "")).strip()

    return MCPEntry(
        name=name, path=Path(path_str), install=install, run=run,
        optional=bool(raw.get("optional", False)),
        description=description,
    )
