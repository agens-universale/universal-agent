"""PATH и резолв команд MCP для subprocess агента."""

from __future__ import annotations

import os
import shutil
from typing import Mapping

from gc_core.mcp_runtime.paths import mcp_bin_dir
from gc_core.mcp_runtime.state import load_runtime_state


def _effective_bin_dir() -> os.PathLike[str] | None:
    state = load_runtime_state()
    if state is not None and state.bin_dir.is_dir():
        return state.bin_dir
    fallback = mcp_bin_dir()
    return fallback if fallback.is_dir() else None


def augment_process_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Prepend MCP bin в PATH дочернего процесса (не трогая env агента)."""
    merged = dict(os.environ if env is None else env)
    bin_dir = _effective_bin_dir()
    if bin_dir is None:
        return merged
    bin_path = os.fspath(bin_dir)
    current = merged.get("PATH", "")
    if bin_path not in current.split(os.pathsep):
        merged["PATH"] = bin_path + (os.pathsep + current if current else "")
    return merged


def mcp_process_env(
    command: str,
    overrides: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Env процесса MCP: mcp-env PATH, каталог команды и env из конфига.

    ``command`` — уже разрешённая (`resolve_mcp_command`). Заданная путём,
    она видит свой каталог первым в PATH: скрипт на ``#!/usr/bin/env node``
    (npx из Node агента) найдёт интерпретатор рядом с собой, а не системный
    другой версии. Каталог, который уже есть в PATH (в том числе через
    ссылку: `/bin` → `/usr/bin`), не переставляется.
    """
    merged = augment_process_env()
    if os.path.isabs(command):
        command_dir = os.path.dirname(command)
        current = merged.get("PATH", "")
        if not _dir_in_path(command_dir, current):
            merged["PATH"] = command_dir + (
                os.pathsep + current if current else ""
            )
    if overrides:
        merged.update({str(k): str(v) for k, v in overrides.items()})
    return merged


def _dir_in_path(directory: str, path_value: str) -> bool:
    """Сравнение по настоящему пути: ссылка на каталог из PATH, регистр и
    хвостовой разделитель (Windows) — тот же каталог. Относительные и пустые
    элементы не в счёт: ребёнок разрешит их от своего cwd, не от агента."""

    def canon(p: str) -> str:
        return os.path.normcase(os.path.realpath(p))

    target = canon(directory)
    return any(
        os.path.isabs(p) and canon(p) == target
        for p in path_value.split(os.pathsep)
    )


def resolve_mcp_command(command: str) -> str:
    """Найти MCP-команду в mcp-env; иначе вернуть command как есть."""
    command = os.path.expanduser(command)
    bin_dir = _effective_bin_dir()
    if bin_dir is not None:
        found = shutil.which(command, path=os.fspath(bin_dir))
        if found:
            return found
    if os.name != "nt":
        return command
    if os.path.splitext(command)[1]:
        return command
    resolved = shutil.which(command)
    return resolved if resolved else command
