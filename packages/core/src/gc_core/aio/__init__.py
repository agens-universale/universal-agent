"""Публичный async-фасад SDK: агент, который не блокирует ваш event loop.

    from gc_core.aio import AsyncAgent

    async with await AsyncAgent.create(config_dict) as agent:
        async for kind, text in agent.stream("почини тесты", thread_id="t1"):
            if kind == "answer":
                print(text, end="")

Всё, что здесь есть, доступно и напрямую (``acreate_run_context`` +
``AgentExecutor.arun``) — фасад просто избавляет встраивающего от знания
внутренних имён и от ручной проводки потока дельт.

Синхронный ``AgentExecutor.run`` никуда не делся и не депрекейтится: это
первоклассный вход, на который опираются CLI, TUI и сервер.
"""
from gc_core.aio.agent import AsyncAgent

__all__ = ["AsyncAgent"]
