"""Публичный async-фасад SDK: агент, который не блокирует ваш event loop.

    from gc_core.aio import AsyncAgent
    from gc_core.config import load_settings

    async with await AsyncAgent.create(load_settings("config.yml")) as agent:
        async for kind, text in agent.stream("почини тесты", thread_id="t1"):
            if kind == "answer":
                print(text, end="")

Всё, что здесь есть, доступно и напрямую (``acreate_run_context`` +
``AgentExecutor.arun``) — фасад просто избавляет встраивающего от знания
внутренних имён и от ручной проводки потока дельт.

Синхронного входа у ядра больше нет: ``AgentExecutor.run`` снят. Из
синхронного кода агента ведут своим ``asyncio.run`` — так делают TUI и
Harbor-адаптер, каждый в своём рабочем потоке.
"""

from gc_core.aio.agent import AsyncAgent

__all__ = ["AsyncAgent"]
