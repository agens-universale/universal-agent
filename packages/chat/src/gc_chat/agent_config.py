"""Настройки хода: свой каталог и своя ролевая вставка."""

from __future__ import annotations

from gc_core.settings import LoadedSettings


def prepare_agent_settings(
    base: LoadedSettings, *, cwd: str, instructions_path: str
) -> LoadedSettings:
    """Общие настройки процесса не меняются: правка даёт новый объект.

    У Responses и Chat Completions разные вставки, поэтому путь ставится
    на копию перед ``AsyncAgent.create``, а не один раз на процесс.
    """
    return base.with_overrides(
        {
            "paths.chat_completions_special_instructions_path": instructions_path
        },
        layer="runtime",
    ).with_runtime(launch_root=cwd)
