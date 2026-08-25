"""AsyncAgent — тонкая обёртка над acreate_run_context + AgentExecutor.arun."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional

from gc_core.agent_executor import AgentExecutor
from gc_core.run_context import acreate_run_context
from gc_core.utils.async_http import acquire_clients

#: Метка «прогон кончился» в очереди дельт. Отдельный объект, а не None:
#: None — легальное значение, которое может прийти от колбэка.
_DONE = object()


class AsyncAgent:
    """Агент для асинхронного приложения.

    Держит один ``RunContext`` и один ``AgentExecutor``: сборка контекста —
    самая дорогая часть старта (MCP-дискавери), и переиспользовать её между
    ходами дешевле, чем собирать заново.
    """

    def __init__(self, executor: AgentExecutor, queue: "asyncio.Queue") -> None:
        self._executor = executor
        self._queue = queue

    @classmethod
    async def create(
        cls,
        config_dict: dict,
        *,
        auth_connector: Any = None,
        on_stream_delta: Any = None,
        **context_kwargs: Any,
    ) -> "AsyncAgent":
        """Собрать агента, не блокируя loop.

        ``on_stream_delta`` — необязательный «второй слушатель»: дельты и так
        уходят в очередь для ``stream()``, но приложению бывает нужен ещё и
        свой sink (лог, метрика).

        ``tools_cache`` (через ``context_kwargs``) — ``ToolsCache``, которым
        владеет приложение. Самая дорогая часть старта — MCP-дискавери
        (866–1405 мс на реальном конфиге); один агент платит её один раз, но
        приложение, поднимающее агентов пачками, заплатит за каждого. Общий
        кэш снимает это, и он же достаётся субагентам через ``ctx``::

            cache = ToolsCache(config_dict)
            a1 = await AsyncAgent.create(cfg, tools_cache=cache)
            a2 = await AsyncAgent.create(cfg, tools_cache=cache)   # уже бесплатно
        """
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _sink(kind: str, text: str) -> None:
            # Колбэк приходит из чужого потока (синхронный тул через
            # to_thread, компакция), поэтому в очередь кладём потокобезопасно.
            loop.call_soon_threadsafe(queue.put_nowait, (kind, text))
            if on_stream_delta is not None:
                on_stream_delta(kind, text)

        acquire_clients()
        ctx = await acreate_run_context(
            config_dict,
            auth_connector=auth_connector,
            on_stream_delta=_sink,
            **context_kwargs,
        )
        return cls(AgentExecutor(ctx), queue)

    @property
    def executor(self) -> AgentExecutor:
        """Исполнитель под фасадом — для того, чего фасад не покрывает."""
        return self._executor

    async def run(self, prompt: "str | list[str]", **kwargs: Any) -> Any:
        """Один ход агента. Возвращает финальный ответ."""
        messages = [prompt] if isinstance(prompt, str) else list(prompt)
        kwargs.setdefault("system", "sdk")
        return await self._executor.arun(messages, **kwargs)

    async def stream(
        self, prompt: "str | list[str]", **kwargs: Any
    ) -> AsyncIterator[tuple]:
        """Ход агента с живыми дельтами: ``(kind, text)``.

        ``kind``: ``"answer"`` — текст ответа, ``"think"`` — рассуждение,
        ``"reset"`` — начать вывод заново (повторная попытка запроса).

        Дельты приходят только если провайдер стримит; ход всё равно
        доигрывается до конца, и исключение из прогона поднимается здесь же —
        итератор не «затихает» на ошибке.
        """
        task = asyncio.create_task(self.run(prompt, **kwargs))
        task.add_done_callback(lambda _t: self._queue.put_nowait(_DONE))
        try:
            while True:
                item = await self._queue.get()
                if item is _DONE:
                    break
                yield item
        finally:
            # Ждём ход даже если потребитель бросил итератор на полпути:
            # иначе прогон осиротеет, а его исключение всплывёт «ниоткуда».
            await task

    def send(self, message: dict) -> None:
        """Подкинуть сообщение в идущий ход (или в следующий, если ход не идёт)."""
        self._executor.add_message(message)

    def stop(self) -> None:
        """Кооперативная остановка: ход завершит текущую итерацию и выйдет."""
        self._executor.stop_run()

    async def aclose(self) -> None:
        """Отпустить сетевой пул.

        Пул общий на loop, поэтому закрываем его только когда ушёл последний
        агент: иначе ``aclose`` одного рвал бы запрос соседнего — сценарий не
        экзотический, он в приёмочном тесте и в examples/async_embed.py.
        """
        from gc_core.utils.async_http import release_clients

        await release_clients()

    async def __aenter__(self) -> "AsyncAgent":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self.aclose()
        return False


__all__ = ["AsyncAgent"]
