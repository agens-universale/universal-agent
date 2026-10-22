"""Настройки хода: свой каталог, общие настройки процесса не меняются."""

from __future__ import annotations

from gc_core.settings import LoadedSettings


def prepare_agent_settings(
    base: LoadedSettings, *, cwd: str
) -> LoadedSettings:
    return base.with_runtime(launch_root=cwd)
