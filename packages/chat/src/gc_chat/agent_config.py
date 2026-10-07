"""Копия конфига хода: свой каталог и своя ролевая вставка."""

from __future__ import annotations

import copy


def prepare_agent_config(
    base: dict, *, cwd: str, instructions_path: str
) -> dict:
    """Общий словарь процесса не меняется.

    У Responses и Chat Completions разные вставки, поэтому путь пишется
    на копию перед ``AsyncAgent.create``, а не один раз на процесс.
    """
    config = copy.deepcopy(base)
    config["launch_root"] = cwd
    config["chat_completions_special_instructions_path"] = instructions_path
    return config
