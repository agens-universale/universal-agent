"""Встраивание агента в асинхронное приложение.

Запуск на живом бэкенде:

    python -m examples.async_embed ~/.gc/config.yml "привет" "как дела"

Показывает ровно то, ради чего делался переезд: пока агенты работают,
приложение продолжает крутить свои задачи на том же event loop.
"""

from __future__ import annotations

import asyncio
import sys

from gc_core.aio import AsyncAgent
from gc_core.config import load_config


async def _ticker(stop: asyncio.Event) -> int:
    """Полезная работа приложения, которая не имеет права встать."""
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(0.1)
        ticks += 1
    return ticks


async def main(config_path: str, prompts: list[str]) -> list[str]:
    config_dict = load_config(config_path)
    stop = asyncio.Event()
    ticker = asyncio.create_task(_ticker(stop))

    async def ask(i: int, prompt: str) -> str:
        async with await AsyncAgent.create(dict(config_dict)) as agent:
            return await agent.run(prompt, thread_id=f"embed-{i}")

    try:
        answers = await asyncio.gather(*(ask(i, p) for i, p in enumerate(prompts)))
    finally:
        stop.set()
        ticks = await ticker
        print(f"[приложение] тиков за время работы агентов: {ticks}", file=sys.stderr)
    return list(answers)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    for answer in asyncio.run(main(sys.argv[1], sys.argv[2:])):
        print(answer)
