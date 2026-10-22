"""AsyncAgent — тонкая обёртка над acreate_run_context + AgentExecutor.arun."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator

from gc_core.agent_executor import AgentExecutor, undelivered_as_arun_args
from gc_core.attachments.model import Attachment
from gc_core.run_context import acreate_run_context
from gc_core.stores import Stores
from gc_core.utils.async_http import acquire_clients, release_clients

logger = logging.getLogger(__name__)

#: Метка «прогон кончился» в очереди дельт. Отдельный объект, а не None:
#: None — легальное значение, которое может прийти от колбэка.
_DONE = object()


def _take_tail(executor: Any) -> list:
    """Хвост хода: отдано исполнителю, а до агента не дошло.

    Подробности — `AgentExecutor.take_undelivered_messages`. Исполнитель без
    этого метода хвоста не отдаёт — доигрывать нечего.
    """
    take = getattr(executor, "take_undelivered_messages", None)
    return list(take()) if take is not None else []


def _unpack_stores(stores: Stores | None, context_kwargs: dict) -> None:
    """``stores=`` → ``thread_store``/``agent_store`` сборки контекста."""
    if stores is None:
        return
    for name in ("thread_store", "agent_store"):
        if context_kwargs.get(name) is not None:
            raise ValueError(f"stores= и {name}= вместе: выберите одно")
    context_kwargs["thread_store"] = stores.threads
    context_kwargs["agent_store"] = stores.agent


class AsyncAgent:
    """Агент для асинхронного приложения.

    Держит один ``RunContext`` и один ``AgentExecutor``: сборка контекста —
    самая дорогая часть старта (MCP-дискавери), и переиспользовать её между
    ходами дешевле, чем собирать заново.
    """

    def __init__(
        self,
        executor: AgentExecutor | None,
        subscribers: set[asyncio.Queue],
        *,
        durable: Any = None,
    ) -> None:
        self._executor = executor
        # Durable-рантайм (``durable=`` в ``create``): ходы исполняет он, а
        # своего исполнителя и пула у агента нет.
        self._durable = durable
        # Живые подписчики stream(). Пустой набор — дельты никуда не копятся.
        self._subscribers = subscribers
        # Результат последнего stream(): сам итератор вернуть его не может.
        self._last_answer: Any = None
        # Отпустил ли уже пул: повторный aclose не должен ронять счётчик
        # владельцев ниже нуля и закрывать клиенты соседнего агента.
        self._closed = False
        # Остановка, запрошенная за время текущего `run`. Флаг фасада, а не
        # только исполнителя: стоп в сетапе хода — до того, как создан его
        # state, — `stop_run` пишет в state закончившегося хода, и по нему
        # одному `run` доигрывал бы хвост вопреки остановке.
        self._stop_requested = False

    @classmethod
    async def create(
        cls,
        settings: Any,
        *,
        auth_connector: Any = None,
        on_stream_delta: Any = None,
        durable: Any = None,
        stores: Stores | None = None,
        ask_human_suspends: bool = False,
        **context_kwargs: Any,
    ) -> AsyncAgent:
        """Собрать агента, не блокируя loop.

        ``ask_human_suspends`` — канал без живого человека (A2A): вопрос
        модели (``turn_ask_human``) не ждёт ответа, а завершает ход
        (``pending_question``); ответ — следующим ``run([], answer=…)``.

        ``stores`` — связка хранилищ (``gc_core.stores.Stores``): треды и
        ресурсы агента одним объектом; вместе с ``thread_store=`` или
        ``agent_store=`` — ``ValueError``.

        ``settings`` — ``AppConfig`` (``default_settings(...)``) или
        ``LoadedSettings`` (``load_settings(path)``): провалидированные и
        неизменяемые. Плоский словарь не принимается — правка настроек
        только явная, ``override_settings`` даёт новый объект.

        ``on_stream_delta`` — необязательный «второй слушатель»: дельты и так
        уходят в очередь для ``stream()``, но приложению бывает нужен ещё и
        свой sink (лог, метрика).

        ``tools_cache`` (через ``context_kwargs``) — ``ToolsCache``, которым
        владеет приложение. Самая дорогая часть старта — MCP-дискавери
        (866–1405 мс на реальном конфиге); один агент платит её один раз, но
        приложение, поднимающее агентов пачками, заплатит за каждого. Общий
        кэш снимает это, и он же достаётся субагентам через ``ctx``::

            backend = resolve_os_backend(settings)   # harbor: + environment, loop
            cache = ToolsCache(os_backend=backend)
            a1 = await AsyncAgent.create(settings, tools_cache=cache, os_backend=backend)
            a2 = await AsyncAgent.create(settings, tools_cache=cache, os_backend=backend)

        Бэкенд кэшу нужен ОБЯЗАТЕЛЬНО, если в конфиге есть MCP-сервер с
        ``execute_on: os_backend``: свой бэкенд кэш получает только при
        создании (передача в агента перепривязывает конфиг, но не бэкенд), а
        кэш без бэкенда считает такой сервер «своим» и проходит дискавери в
        процессе агента — для stdio это запуск сервера НА ХОСТЕ, мимо
        песочницы. Вызовы тулов при этом уходят правильно: течёт дискавери.
        """
        from gc_core.config_modules.root import AppConfig
        from gc_core.settings import LoadedSettings

        _unpack_stores(stores, context_kwargs)

        if not isinstance(settings, (AppConfig, LoadedSettings)):
            raise TypeError(
                "AsyncAgent.create принимает AppConfig или LoadedSettings, "
                f"а не {type(settings).__name__}: соберите настройки через "
                "gc_core.config.load_settings(path) или "
                "gc_core.settings.default_settings(**{'agent.model': ...})"
            )
        if durable is not None:
            # Ходы исполняет durable-рантайм (``gc_core.aio.durable``;
            # Temporal — ``gc_core_temporalio.TemporalDurability``): контекст,
            # MCP и модель — на его стороне, локально ничего не поднимается.
            if context_kwargs or auth_connector or on_stream_delta:
                raise TypeError(
                    "durable=: контекст собирает рантайм; "
                    "auth_connector, on_stream_delta и параметры контекста "
                    "здесь не принимаются"
                )
            return cls(None, set(), durable=await durable.open(settings))
        # Набор ЖИВЫХ подписчиков вместо одной очереди на агента.
        #
        # Одна общая очередь давала два бага, оба воспроизводились замером:
        #   1. после обычного run() в ней оставались дельты, и следующий
        #      stream() выдавал их первыми — «ПЕРВЫЙ-ХОДВТОРОЙ-ХОД»;
        #   2. два stream() параллельно: первый забирал всё, включая чужое,
        #      второй получал пустоту.
        # Плюс агент копил события всегда, даже когда их никто не читал.
        subscribers: set[asyncio.Queue] = set()
        loop = asyncio.get_running_loop()

        def _fanout(item: Any) -> None:
            for q in tuple(subscribers):
                q.put_nowait(item)

        def _sink(kind: str, text: str) -> None:
            # Колбэк приходит из чужого потока (синхронный тул через
            # to_thread, компакция), поэтому в очередь кладём потокобезопасно.
            if subscribers:
                loop.call_soon_threadsafe(_fanout, (kind, text))
            if on_stream_delta is not None:
                on_stream_delta(kind, text)

        # Права на пул заявляем ДО сборки: она сама ходит в сеть (MCP), и без
        # заявки соседний агент мог бы закрыть пул у неё под руками. Но если
        # сборка упадёт — а падает она в первую очередь на отказе
        # MCP-дискавери, — заявку надо снять: иначе счётчик владельцев этого
        # loop никогда не дойдёт до нуля и пул не закроется до конца процесса.
        acquire_clients()
        try:
            ctx = await acreate_run_context(
                settings,
                auth_connector=auth_connector,
                on_stream_delta=_sink,
                **context_kwargs,
            )
        except BaseException:
            await release_clients()
            raise
        ctx.ask_human_suspends = ask_human_suspends
        return cls(AgentExecutor(ctx), subscribers)

    @property
    def closed(self) -> bool:
        """Отпустил ли агент свой пул. После ``aclose`` ход не начинают."""
        return self._closed

    @property
    def executor(self) -> AgentExecutor:
        """Исполнитель под фасадом — для того, чего фасад не покрывает."""
        if self._durable is not None:
            raise RuntimeError(
                "durable-агент: исполнитель — на стороне рантайма"
            )
        return self._executor

    async def run(
        self,
        prompt: str | list[str],
        *,
        attachments: list[list[Attachment] | None] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Ход агента — и доигрывание хвоста. Возвращает ответ последнего хода.

        Хвост — отданное `send` после последнего забора очереди циклом: пока ход
        сохранял тред и сносил MCP, он ещё шёл, но читать очередь было уже
        некому. Исполнитель откладывает такое в сторону, и забрать его, кроме
        владельца, некому: следующий `run` отдал бы его позади своего запроса,
        а без следующего оно не дошло бы вовсе. Поэтому, как сервер и TUI,
        доигрываем хвост отдельным ходом того же исполнителя по тому же треду,
        пока он не опустеет.

        Возвращается ответ ПОСЛЕДНЕГО хода: он идёт по тому же треду и отвечает
        с учётом всего, что было до него, — это итог вызова. Ответы ходов
        раньше него лежат в истории треда. Без `thread_id` истории нет ни у
        какого хода, и хвостовой ход — не исключение.

        Остановленный, упавший и отменённый ход хвост не доигрывает — это
        отменило бы стоп или спрятало отказ — и дальше его не оставляет: хвост
        уходит в лог событием `run_tail_dropped`, а исключение хода — наверх.

        ``attachments`` — картинки по сообщениям, параллельно ``prompt`` (строка —
        одно сообщение: ``[[a1, a2]]``): вложения из кармана процесса, в тред их
        копирует ход. Хвост несёт свои вложения (``undelivered_as_arun_args``) —
        картинки первого хода в него не переезжают.
        """
        messages = [prompt] if isinstance(prompt, str) else list(prompt)
        kwargs.setdefault("system", "sdk")
        if self._durable is not None:
            return await self._run_durable(messages, attachments, kwargs)
        self._stop_requested = False
        # Хвост, взятый у исполнителя и ещё не доставленный: его несёт
        # следующий ход. Исполнитель его уже не вернёт — для него это аргументы
        # `arun`, а не очередь. Умрёт ход, который его несёт, — даже в сетапе, —
        # хвост потерян вместе с остальным. Доставлен он, когда этот ход
        # закончился: тогда место занимает следующий взятый хвост.
        in_flight: list = []
        try:
            result = await self._executor.arun(
                messages, attachments=attachments, **kwargs
            )
            while True:
                tail = _take_tail(self._executor)
                # С этой строки хвост — только здесь.
                in_flight = tail
                if not tail:
                    return result
                if self._turn_was_stopped():
                    self._drop_tail(tail, "stopped", kwargs)
                    return result
                texts, fields = undelivered_as_arun_args(tail)
                # Тот же тред, но не с чистого листа: `start_new_thread`
                # первого хода стёр бы то, что этот ход должен продолжить.
                # Ответ на вопрос — только первому ходу: вопроса уже нет.
                kwargs.pop("answer", None)
                result = await self._executor.arun(
                    texts, **{**kwargs, **fields, "start_new_thread": False}
                )
        except asyncio.CancelledError:
            self._drop_tail(
                [*in_flight, *_take_tail(self._executor)], "cancelled", kwargs
            )
            raise
        except BaseException:
            self._drop_tail(
                [*in_flight, *_take_tail(self._executor)], "failed", kwargs
            )
            raise

    async def _run_durable(self, messages, attachments, kwargs) -> Any:
        """Ход через durable-рантайм: отправить и дождаться ответа хода."""
        unsupported = sorted(
            set(kwargs) - {"system", "thread_id", "message_ids"}
        )
        if attachments or unsupported:
            raise TypeError(
                "durable-агент принимает thread_id, system и message_ids; "
                f"не поддержано: {['attachments'] if attachments else []}"
                f"{unsupported}"
            )
        return await self._durable.run(
            messages,
            thread_id=kwargs.get("thread_id"),
            system=kwargs["system"],
            message_ids=kwargs.get("message_ids"),
        )

    def _turn_was_stopped(self) -> bool:
        """Останавливали ли ход, который только что закончился, — или весь `run`."""
        if self._stop_requested:
            return True
        get_state = getattr(self._executor, "get_state", None)
        state = get_state() if get_state is not None else None
        return bool(getattr(state, "stop_requested", False))

    @staticmethod
    def _drop_tail(tail: list, ending: str, kwargs: dict) -> None:
        """Хвост, который доставить уже некуда: потеря — часть исхода, но не молчаливая."""
        if not tail:
            return
        logger.warning(
            "run_tail_dropped thread_id=%s ending=%s count=%d",
            kwargs.get("thread_id"),
            ending,
            len(tail),
            extra={
                "event": "run_tail_dropped",
                "thread_id": kwargs.get("thread_id"),
                "system": kwargs.get("system"),
                "ending": ending,
                "count": len(tail),
            },
        )

    async def stream(
        self, prompt: str | list[str], **kwargs: Any
    ) -> AsyncIterator[tuple]:
        """Ход агента с живыми дельтами: ``(kind, text)``.

        ``kind``: ``"answer"`` — текст ответа, ``"think"`` — рассуждение,
        ``"reset"`` — начать вывод заново (повторная попытка запроса).

        Дельты приходят только если провайдер стримит; ход всё равно
        доигрывается до конца, и исключение из прогона поднимается здесь же —
        итератор не «затихает» на ошибке.

        Финальный ответ доступен после итерации через ``last_answer`` — раньше
        он просто терялся: ``await task`` в ``finally`` отбрасывал результат, и
        потребителю приходилось склеивать ответ из дельт, что не то же самое
        (ход закрывается ``turn_finish``, и прозы перед ним может не быть).

        Очередь заводится НА ВЫЗОВ, а не на агента: иначе два параллельных
        ``stream()`` делили бы одну, и первый забирал бы чужие дельты.
        """
        if self._durable is not None:
            # Потоковых дельт durable-рантайм не передаёт: ответ — целиком
            # в ``last_answer`` по завершении хода.
            self._last_answer = await self.run(prompt, **kwargs)
            return
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        task = asyncio.create_task(self.run(prompt, **kwargs))
        task.add_done_callback(lambda _t: queue.put_nowait(_DONE))
        cancelled = False
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                yield item
        except asyncio.CancelledError:
            # Отмена и брошенный итератор — РАЗНЫЕ вещи, и лечатся по-разному.
            # Брошенный итератор (``break`` у потребителя) — ход всё равно
            # доигрываем: см. ниже. Отмена — это `wait_for`, отвалившийся
            # клиент, остановка приложения; вызывающий просит прекратить
            # СЕЙЧАС. Пока обе ветки шли через один `await task`, отмена
            # откладывалась до конца хода: `TimeoutError` приходил не через
            # заявленные 0.2 с, а через все три секунды прогона, и LLM-вызовы
            # с тулами продолжали идти.
            cancelled = True
            self._executor.stop_run()  # кооперативно: дать ходу закрыться
            task.cancel()
            raise
        finally:
            # Отписаться ПЕРВЫМ делом: если потребитель бросил итератор, дельты
            # не должны копиться в осиротевшей очереди до конца хода.
            self._subscribers.discard(queue)
            if cancelled:
                # Ход уже отменён — ждём только его завершения, результат
                # брать неоткуда. Исключение отмены здесь не поднимаем: наверх
                # уже летит своё.
                with contextlib.suppress(BaseException):
                    await task
            else:
                # Ждём ход даже если потребитель бросил итератор на полпути:
                # иначе прогон осиротеет, а его исключение всплывёт «ниоткуда».
                self._last_answer = await task

    @property
    def pending_question(self) -> dict | None:
        """Вопрос, на котором приостановлен последний ход
        (``ask_human_suspends``): ``{call_id, question}`` или ``None``."""
        if self._executor is None:
            return None
        return self._executor.last_pending_question

    @property
    def last_answer(self) -> Any:
        """Финальный ответ последнего ``stream()``; ``None`` до первого хода.

        Существует потому, что async-генератор не умеет вернуть значение
        итерирующему: ``return`` внутри него закрывает итерацию, а не отдаёт
        результат. Раньше ответ просто терялся.
        """
        return self._last_answer

    def send(self, message: dict) -> None:
        """Подкинуть сообщение в идущий ход (или в следующий, если ход не идёт).

        Опоздавшее к последнему забору очереди — пока ход сохранял тред и
        сносил MCP — тот же `run` доигрывает отдельным ходом (см. `run`).

        Картинки — ключ ``attachments`` (словари ``Attachment.to_dict()`` из
        кармана): ход копирует их в тред, когда агент принимает сообщение.
        """
        if self._durable is not None:
            self._durable.send(message)
            return
        self._executor.add_message(message)

    def stop(self) -> None:
        """Кооперативная остановка: ход завершит текущую итерацию и выйдет.

        Хвост остановленного `run` не доигрывается.
        """
        self._stop_requested = True
        if self._durable is not None:
            self._durable.stop()
            return
        self._executor.stop_run()

    async def aclose(self) -> None:
        """Отпустить сетевой пул. Идемпотентно.

        Пул общий на loop, поэтому закрываем его только когда ушёл последний
        агент: иначе ``aclose`` одного рвал бы запрос соседнего — сценарий не
        экзотический, он в приёмочном тесте и в examples/async_embed.py.

        Идемпотентность здесь не вежливость. Повторный вызов — обычное дело
        (`async with` плюс явный `aclose`, повторный выход по ошибке), а
        отпускать пул дважды значит уронить счётчик владельцев ниже нуля и
        закрыть клиенты соседа.
        """
        from gc_core.utils.async_http import release_clients
        from gc_core.utils.logging_module import release_run_logger

        if self._closed:
            return
        self._closed = True
        if self._durable is not None:
            await self._durable.aclose()
            return
        try:
            await self._aclose_owned_mcp_sessions()
        finally:
            await release_clients()
        # Логгер прогона уникален по имени и остаётся в реестре `logging` до
        # конца процесса вместе с открытым файловым хендлером. Ветка объявляет
        # сценарий «много AsyncAgent в одном процессе» — там это линейный рост
        # дескрипторов. Хендлеры, переданные вызывающим, не трогаем.
        release_run_logger(
            getattr(getattr(self._executor, "_ctx", None), "logger", None)
        )

    async def _aclose_owned_mcp_sessions(self) -> None:
        """Закрыть MCP-сессии, поднятые при ``create``.

        Дискавери в ``acreate_run_context`` идёт под токеном владельца этого
        агента, так что закрываем ровно свои — сосед на том же loop не
        заденет. Без этого долгоживущий stdio (браузер под
        ``@playwright/mcp``) гасил только ``finally`` прогона: агент без
        прогона оставлял процессы жить до выхода из процесса.
        """
        ctx = getattr(self._executor, "_ctx", None)
        owner = getattr(ctx, "mcp_session_owner", "") or None
        if not owner:
            return
        from gc_core.agent_executor import _aclose_mcp_and_release
        from gc_core.mcp_session_owner import set_owner

        await _aclose_mcp_and_release(set_owner(owner))

    async def __aenter__(self) -> AsyncAgent:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self.aclose()
        return False


__all__ = ["AsyncAgent"]
