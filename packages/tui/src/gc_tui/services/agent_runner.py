"""AgentRunner — обёртка AgentExecutor в daemon Thread (§3 спеки).

Бэкенд-агностична: принимает любой объект с методами
.arun(messages, *, thread_id, start_new_thread, system, ...),
.stop_run(), .add_message(message) и, если есть,
.take_undelivered_messages() — хвост прогона доигрывается отдельным ходом.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from gc_tui.services.stdin_bridge import redirect_stdin

# O11: без timeout агент-поток может висеть вечно, если UI не ответил на confirm.
# 10 мин — достаточно человеку; дальше fail-closed deny.
CONFIRM_WAIT_TIMEOUT_SEC = 600.0

_log = logging.getLogger(__name__)


def _run_agent(executor, a, *, thread_id, start_new_thread, system) -> None:
    """Прогон агента: свой ``asyncio.run`` в этом же рабочем потоке.

    Раньше здесь стоял синхронный ``executor.run(...)``, а тот уходил на
    ПОРТАЛЬНЫЙ loop — то есть на прогон приходились два потока и два loop:
    рабочий поток TUI.

    Поток остаётся: агент не должен делить loop с Textual, иначе любая
    непокрытая блокировка в ядре морозит UI.

    На loop самого приложения агента вести нельзя и по второй причине —
    файловый API ``FsBackend`` синхронный, и у мостящих бэкендов (Harbor,
    TrackingFsBackend) он блокирует поток; на своём же loop это дедлок.

    Клиенты httpx закрываются здесь же: ``asyncio.run`` убивает loop, а реестр
    ключуется по ``id(loop)`` — адреса переиспользуются, и оставленный клиент
    мёртвого loop мог бы достаться следующему прогону.
    """
    import asyncio

    from gc_core.utils.async_http import aclose_clients

    async def _body():
        try:
            await executor.arun(
                a["msgs"],
                thread_id=thread_id,
                start_new_thread=start_new_thread,
                system=system,
                discard_writes_after_stop=True,
                message_ids=a["message_ids"],
                display_contents=a["display_contents"],
                skills_loaded=a["skills_loaded"],
            )
        finally:
            await aclose_clients()

    asyncio.run(_body())


def _take_tail(executor) -> list:
    """Хвост прогона: отдано исполнителю, а до агента не дошло.

    Подробности — `AgentExecutor.take_undelivered_messages`. Исполнитель без
    этого метода хвоста не отдаёт — доигрывать нечего.
    """
    take = getattr(executor, "take_undelivered_messages", None)
    return list(take()) if take is not None else []


def _tail_batch(messages: list) -> dict:
    """Сообщения очереди → аргументы `_run_agent` для отдельного хода."""
    from gc_core.agent_executor import undelivered_as_arun_args

    texts, extra = undelivered_as_arun_args(messages)
    return {"msgs": texts, **extra}


async def _compact_body(executor, thread_id: str, system: str | None) -> None:
    """Форс-сжатие со своим loop — и с той же уборкой, что у прогона.

    ``/compact`` поднимает собственный ``asyncio.run``, значит заводит и
    собственный пул httpx-клиентов. Без закрытия каждое сжатие оставляло
    открытый ``AsyncClient`` в реестре ``async_http`` на всю жизнь процесса —
    ровно та утечка, от которой путь прогона выше уже защищён.
    """
    from gc_core.utils.async_http import aclose_clients

    try:
        await executor.acompact_thread(thread_id, system=system)
    finally:
        await aclose_clients()


class AgentRunner:
    """Управляет жизненным циклом одного run AgentExecutor."""

    def __init__(
        self,
        executor: Any = None,
        *,
        executor_factory: Optional[Callable[[], Any]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        on_complete: Optional[Callable[[Optional[str]], None]] = None,
    ) -> None:
        # executor задан напрямую (тесты) ИЛИ строится лениво из factory в
        # worker-потоке (прод): сборка executor включает MCP-дискавери с сетевыми
        # вызовами и не должна выполняться на UI-потоке внутри get_runner().
        self._executor = executor
        self._executor_factory = executor_factory
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._on_error = on_error
        self._on_complete = on_complete
        self._stdin_queue: queue.Queue[str] = queue.Queue()
        self._prebuild_messages: list[dict] = []
        #: Прогон ещё принимает сообщения в свою очередь. Ставится при запуске
        #: воркера и снимается им под `_lock` в том же шаге, где он убедился,
        #: что хвоста нет (`_close_or_take_tail`). Сообщение либо ляжет в
        #: очередь до этой проверки и будет доиграно, либо увидит закрытый
        #: прогон и уйдёт своим ходом (`add_message`) — третьего нет.
        self._accepting = False
        #: Тред и система последнего прогона: в них доигрывается сообщение,
        #: опоздавшее к закрытию.
        self._last_target: Optional[tuple[str, str]] = None
        self._stop_requested = False
        # TUI _stop_and_replace_runner: runner снят с active_runner, но поток ещё
        # может досидеть HTTP/ретраи. Его on_error не должен залипать ✗ Ошибка
        # поверх уже живого следующего прогона.
        self._marked_stopped = False
        # Вид последней работы воркера: "run" | "compact" | "". Falling-edge
        # стампа терминального статуса (main._poll_active) пропускает "compact",
        # чтобы ручной /compact не перетирал прежний терминал-статус на completed.
        self._thread_kind: str = ""
        self._confirm_event = threading.Event()
        self._confirm_choice: str = "deny_once"
        # Runner сняли (замена, сброс, выход приложения) — логгер его прогона
        # надо отдать (см. `release`). Живые воркеры считаются под `_lock`:
        # если поток ещё идёт, логгер отдаёт последний из них на выходе.
        self._released = False
        self._workers = 0

    def _emit_error(self, exc: BaseException) -> None:
        """Сообщить об ошибке в TUI, если runner ещё «актуальный».

        После stop/replace поток может упасть на ConnectionError в хвосте ретраев —
        это не ошибка текущего UI-прогона, и last_runner_error ставить нельзя.
        """
        if self._marked_stopped or self._stop_requested:
            return
        if self._on_error is None:
            return
        try:
            self._on_error(exc)
        except Exception:
            pass

    def _read_stdin_blocking(self, _question: str) -> str:
        """Ответ из очереди TUI. БЛОКИРУЕТ поток — звать только вне loop.

        Мост ``redirect_stdin`` потоко-локален, и ``input()`` проваливался бы
        на настоящий stdin, занятый Textual. Очередь общая на runner, поэтому
        читать её можно с любого потока.
        """
        lines: list[str] = []
        while True:
            line = self._stdin_queue.get()
            if line in ("", "\n"):
                break
            lines.append(line.rstrip("\n"))
        return "\n".join(lines).strip()

    async def _read_stdin_answer(self, question: str) -> str:
        """Порт «спросить человека» для ядра. КОРУТИННЫЙ, и это существенно.

        Ядро ждёт порт нативно, если он awaitable, и зовёт инлайн, если нет
        (``tools/turn/ask_human.py``). Синхронный порт означал, что весь loop
        агента стоит, пока человек думает: вместе с ним стоят субагенты,
        keep-alive MCP-сессий и хуки компакции. Ожидание человека ничем не
        ограничено, так что «стоит» здесь — это минуты.

        Прежний комментарий оправдывал синхронную форму тем, что диспетчер
        всё равно уводит тул в ``to_thread``. Эта ступень снята вместе с
        синхронным входом тулов, и оправдание вместе с ней.
        """
        import asyncio

        return await asyncio.to_thread(self._read_stdin_blocking, question)

    def _ensure_executor(self) -> Any:
        with self._build_lock:
            if self._executor is None:
                if self._executor_factory is None:
                    raise RuntimeError("AgentRunner: ни executor, ни executor_factory не заданы")
                self._executor = self._executor_factory()
                # Ставим порт на ctx собранного executor: ядро спросит через
                # него вместо stdin. redirect_stdin оставляем — он страхует
                # чужой код, который всё ещё зовёт input() напрямую.
                ctx = getattr(self._executor, "_ctx", None)
                if ctx is not None and getattr(ctx, "ask_human", None) is None:
                    try:
                        ctx.ask_human = self._read_stdin_answer
                    except Exception:
                        pass
            return self._executor

    def warm(self) -> None:
        """Собрать executor в фоне (off-UI), без запуска run. Идемпотентно.

        Сборка включает MCP-дискавери (сетевые вызовы) — выносим в daemon-поток,
        чтобы не блокировать UI на старте. Build-once: warm() и последующий start()
        строят executor ровно один раз (через _build_lock в _ensure_executor)."""
        if self._executor is not None:
            return

        def _target() -> None:
            try:
                self._ensure_executor()
            except Exception as exc:
                self._emit_error(exc)
            finally:
                self._worker_exit()

        with self._lock:
            self._workers += 1
        threading.Thread(target=_target, name="gc-ua-warm", daemon=True).start()

    def is_running(self) -> bool:
        with self._lock:
            t = self._thread
        return t is not None and t.is_alive()

    def pinned_level(self) -> Optional[str]:
        """Read-only: уровень активного хода, зафиксированный `turn_boundary.begin_turn`
        (`state.turn_level`) — не живой уровень сессии (тот меняется мгновенно на
        /level; посреди хода запросы всё ещё идут на прежнем, спека Task 15).

        None — executor ещё не построен / state нет / ход не начинали через
        begin_turn (легаси/тестовые двойники). Не мутирует state."""
        ex = self._executor
        if ex is None or not hasattr(ex, "get_state"):
            return None
        state = ex.get_state()
        if state is None:
            return None
        return getattr(state, "turn_level", None)

    def pinned_router(self) -> Any:
        """Read-only: Router, зафиксированный при сборке ЭТОГО executor'а
        (`AgentExecutor.get_model_router`) — не живой `app.display_router()` TUI,
        который отражает /profiles мгновенно. Футер (Task 15) резолвит
        `pinned_level()` этим Router'ом, чтобы «факт» посреди хода не
        подменился моделью уже переключившегося профиля.

        None — executor ещё не построен / у него нет Router'а (легаси)."""
        ex = self._executor
        if ex is None or not hasattr(ex, "get_model_router"):
            return None
        return ex.get_model_router()

    def usage_snapshot(self) -> Optional[tuple]:
        """Снимок usage от executor'а для футера: ``(session_meta, last_run_meta|None)``
        или None. Не строит executor (снапшот нужен и до первого прогона — тогда None)."""
        ex = self._executor
        if ex is None or not hasattr(ex, "usage_meta_snapshot"):
            return None
        return ex.usage_meta_snapshot()

    def compact_now(self, thread_id: str, system: str) -> bool:
        """Off-thread форс-сжатие треда. False если worker занят."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread_kind = "compact"

            def _target() -> None:
                try:
                    executor = self._ensure_executor()
                    asyncio.run(_compact_body(executor, thread_id, system))
                except Exception as exc:
                    self._emit_error(exc)
                finally:
                    self._worker_exit()

            t = threading.Thread(target=_target, name="gc-ua-compact", daemon=True)
            self._thread = t
            self._workers += 1
        t.start()
        return True

    @staticmethod
    def _run_args(*, query, technical, message_id, display_query, skill_loaded):
        """Построить параллельные списки аргументов для executor.run.

        Технический хвост (technical) получает None в display_contents/skills_loaded,
        чтобы executor не добавлял display-поля к служебным сообщениям."""
        if technical is None:
            msgs = [query]
            display_contents = [display_query]
            skills_loaded = [skill_loaded]
            mids = [message_id] if message_id else None
        else:
            msgs = [query, technical]
            display_contents = [display_query, None]
            skills_loaded = [skill_loaded, None]
            mids = [message_id, None] if message_id else None
        return {"msgs": msgs, "display_contents": display_contents, "skills_loaded": skills_loaded, "message_ids": mids}

    def start(
        self,
        *,
        query: str,
        thread_id: str,
        start_new_thread: bool = False,
        system: str = "cli",
        message_id: Optional[str] = None,
        technical: Optional[str] = None,
        display_query: Optional[str] = None,
        skill_loaded: Optional[str] = None,
    ) -> bool:
        """Запустить run. Возвращает False если уже работает.

        message_id — id user-сообщения (TUI задаёт свой, чтобы потом точечно
        откатить осиротевшую реплику при быстрой отмене).
        display_query / skill_loaded — чистые display-поля; передаются в executor.run
        параллельными списками (технический хвост получает None)."""
        with self._lock:
            if self._busy_locked():
                return False
            self._stop_requested = False
            t = self._spawn_run_locked(
                self._run_args(
                    query=query,
                    technical=technical,
                    message_id=message_id,
                    display_query=display_query,
                    skill_loaded=skill_loaded,
                ),
                thread_id=thread_id,
                start_new_thread=start_new_thread,
                system=system,
            )
        t.start()
        return True

    def _busy_locked(self) -> bool:
        """Занят ли воркер. Звать под `_lock`.

        Прогон занят, пока принимает сообщения: закрывшись, его поток лишь
        дорабатывает `_worker_exit`, и следующему прогону он не мешает. Иная
        работа (`/compact`) занята, пока жив её поток.
        """
        if self._accepting:
            return True
        t = self._thread
        return t is not None and t.is_alive() and self._thread_kind != "run"

    def _spawn_run_locked(
        self, batch: dict, *, thread_id: str, start_new_thread: bool, system: str
    ) -> threading.Thread:
        """Завести воркер прогона. Звать под `_lock`; запускает вызывающий."""
        self._thread_kind = "run"
        self._accepting = True
        self._last_target = (thread_id, system)
        t = threading.Thread(
            target=self._run_worker,
            args=(batch, thread_id, start_new_thread, system),
            name="gc-ua-agent",
            daemon=True,
        )
        self._thread = t
        self._workers += 1
        return t

    def _run_worker(self, batch: dict, thread_id: str, start_new_thread: bool, system: str) -> None:
        executor = None
        # Закрыл ли ЭТОТ воркер свой прогон. Закрытый прогон трогать больше
        # нельзя: сразу после закрытия `add_message` вправе завести следующий
        # воркер, и `_accepting` уже его.
        closed = False
        # Хвост, взятый у исполнителя и ещё не доставленный: его несёт
        # следующий ход. Исполнитель его уже не вернёт — для него это аргументы
        # хода, а не очередь. Умрёт ход, который его несёт, — даже в сетапе, до
        # цикла, — хвост потерян вместе с остальным. Доставлен он, когда этот
        # ход закончился: тогда место занимает следующий взятый хвост.
        in_flight: list = []
        # Чем кончится прогон, если мимо закрытия. Непредвиденное — отказ.
        ending = "failed"
        with redirect_stdin(self._stdin_queue):
            try:
                # Сборка executor (incl. MCP-дискавери) — здесь, в worker-потоке,
                # чтобы не блокировать UI-поток (см. get_runner). Буфер льём под
                # `_lock`: `add_message` решает «в буфер или в executor» под ним
                # же, и сообщение не проскочит между переливом и очисткой.
                executor = self._ensure_executor()
                with self._lock:
                    for m in self._prebuild_messages:
                        executor.add_message(m)
                    self._prebuild_messages.clear()
                if self._stop_requested:
                    ending = "stopped"
                    return
                _run_agent(executor, batch, thread_id=thread_id, start_new_thread=start_new_thread, system=system)
                while True:
                    outcome = getattr(executor, "last_terminal_outcome", None)
                    if self._on_complete is not None:
                        try:
                            self._on_complete(outcome)
                        except Exception:
                            pass
                    tail = self._close_or_take_tail(executor)
                    # С этой строки хвост — только здесь.
                    in_flight = tail
                    if not tail:
                        closed = True
                        break
                    # Хвост — набранное после последнего забора очереди циклом,
                    # пока прогон сохранял тред и сносил MCP. Доигрываем его
                    # отдельным ходом того же треда: иначе executor (он в TUI
                    # один на сессию) отдал бы его СЛЕДУЮЩЕМУ запросу
                    # пользователя, позади этого запроса и одним с ним батчем.
                    _run_agent(executor, _tail_batch(tail), thread_id=thread_id, start_new_thread=False, system=system)
            except Exception as exc:
                self._emit_error(exc)
            except asyncio.CancelledError:
                # Отмена выходит из `asyncio.run` хода и завершает поток, как и
                # раньше; здесь только помечаем исход для хвоста.
                ending = "cancelled"
                raise
            finally:
                if not closed:
                    # Мимо закрытия — ошибка, отмена или стоп до старта прогона.
                    # Хвост не доигрываем: реплики упавшего хода UI уже пометил
                    # неотправленными (`_mark_pending_failed`).
                    self._close_or_take_tail(executor, ending=ending, lost=in_flight)
                self._worker_exit()

    def _close_or_take_tail(self, executor, *, ending: str = "", lost: list | tuple = ()) -> list:
        """Хвост, который надо доиграть, — или закрыть прогон, если доигрывать нечего.

        Проверка и закрытие — один шаг под `_lock`, как и решение в
        `add_message`: сообщение не может ни проскочить между ними, ни лечь в
        очередь, которую уже никто не прочтёт. Остановленный прогон хвост не
        доигрывает (это отменило бы стоп), но и дальше его не отдаёт: иначе
        хвост уехал бы в следующий запрос пользователя.

        ``ending`` — прогон кончился мимо закрытия (``failed``, ``cancelled``,
        ``stopped``): тогда хвост не доигрывается. ``lost`` — батч хвоста, с
        которым умер ход; `_take_tail` его уже не вернёт, поэтому в потерю он
        идёт отсюда.
        """
        with self._lock:
            tail = _take_tail(executor)
            stopped = self._stop_requested or self._marked_stopped
            if not ending and tail and not stopped:
                return tail
            self._accepting = False
            target = self._last_target
        dropped = [*lost, *tail]
        if dropped:
            # Потеря — часть исхода прогона, но не молчаливая: то же событие,
            # что у сервера.
            thread_id, system = target or (None, None)
            _log.warning(
                "run_tail_dropped thread_id=%s ending=%s count=%d",
                thread_id,
                ending or "stopped",
                len(dropped),
                extra={
                    "event": "run_tail_dropped",
                    "thread_id": thread_id,
                    "system": system,
                    "ending": ending or "stopped",
                    "count": len(dropped),
                },
            )
        return []

    def stop(self) -> None:
        """Кооперативная остановка — флаг на executor; run сам выйдет.

        Если executor ещё строится (фабрика не отработала) — ставим флаг, и
        worker-поток не запустит run после сборки.
        Также разблокирует агент-поток, зависший в confirm (fail-closed → False)."""
        self._stop_requested = True
        ex = self._executor
        if ex is not None:
            ex.stop_run()
        self.cancel_confirm()
        # И ask_human тоже. Без этого поток оставался в `queue.get()` навсегда,
        # `arun` не доходил до своего `finally`, и не выполнялись ни закрытие
        # MCP-сессий, ни закрытие пула httpx — то есть Chromium переживал
        # агента, а сокеты жили до конца процесса. Симметрия с
        # `cancel_confirm` здесь не косметика: оба ожидания блокирующие.
        self.cancel_ask_human()

    def release(self) -> None:
        """Runner больше не нужен — отдать логгер его прогона.

        ``acreate_run_context`` заводит на ран свой логгер
        (``agent_run_<kind>_<run_id>``) с файлом и хендлером на stderr, а
        ``logging`` держит его в ``loggerDict`` до конца процесса. TUI его не
        отдавал никогда: каждый снятый runner оставлял логгер, открытый
        дескриптор и всё, до чего дотягивается stderr-хендлер. В тестах stderr —
        перехват Textual, поэтому логгер держал приложение целиком (экран,
        runner, executor, кэши отрисовки): около 0,6 МБ на тест, и полный
        прогон TUI в одном процессе упирался в память.

        Если поток ещё работает (стоп посреди HTTP), логгер отдаёт он сам на
        выходе: отнимать хендлеры у пишущего прогона нельзя.
        """
        with self._lock:
            self._released = True
            idle = self._workers == 0
        if idle:
            self._release_logger()

    def _worker_exit(self) -> None:
        with self._lock:
            self._workers -= 1
            last = self._workers == 0 and self._released
        if last:
            self._release_logger()

    def _release_logger(self) -> None:
        from gc_core.utils.logging_module import release_run_logger

        ctx = getattr(self._executor, "_ctx", None)
        release_run_logger(getattr(ctx, "logger", None))

    def add_message(self, message: dict) -> None:
        with self._lock:
            ex = self._executor
            if ex is None:
                # executor может ещё строиться — тогда буферизуем; worker-поток
                # дольёт сообщения сразу после сборки, до запуска run.
                self._prebuild_messages.append(message)
                return
            target = self._last_target
            # Занятый воркер — это и идущий прогон (сообщение уйдёт в его
            # очередь), и `/compact`: ход рядом с ним шёл бы параллельно сжатию
            # на том же executor и треде.
            if self._busy_locked() or target is None or self._stop_requested or self._marked_stopped:
                ex.add_message(message)
                return
            # Прогон уже закрылся: UI спросил `is_running()` до закрытия, а
            # сообщение отдал после. В очереди executor'а оно дождалось бы
            # только следующего запроса пользователя и ушло бы позади него —
            # поэтому отдельный ход в том же треде, сразу.
            thread_id, system = target
            t = self._spawn_run_locked(
                _tail_batch([message]),
                thread_id=thread_id,
                start_new_thread=False,
                system=system,
            )
        t.start()

    def is_child_input_active(self) -> bool:
        """Ждёт ли ввод субагент ЭТОГО прогона.

        Спрашиваем СВОЙ executor, а не модуль ядра: модульная функция отвечала
        за весь процесс, и в многосессионном сервере это был ответ про чужой
        прогон. Executor ещё не собран — субагента нет и подавно.
        """
        ex = self._executor
        if ex is None:
            return False
        try:
            return bool(ex.is_child_input_active())
        except Exception:
            return False

    def submit_ask_human_reply(self, text: str) -> None:
        """TUI кладёт ответ юзера в queue. ask_human читает строки до пустой,
        поэтому шлём ответ + завершающую пустую строку (иначе read-loop виснет)."""
        self._stdin_queue.put(text + "\n")
        self._stdin_queue.put("\n")  # терминатор read-loop в ask_human

    def make_tui_confirm(
        self,
        present: Callable[..., bool | None],
        *,
        timeout_sec: Optional[float] = None,
    ) -> Callable[..., str]:
        """Вернуть замыкание tui_confirm для передачи в ctx.

        Замыкание вызывается на агент-потоке и блокирует его до ответа юзера
        только после sync ack mounted. present(...) → True|False|None; при
        False — сразу deny без wait() (I2). None — legacy present без ack;
        поддерживается опциональный ``via`` для метки child HITL.
        Возвращает ConfirmChoice: 'allow_remember'|'allow_once'|'deny_remember'|'deny_once'.

        ``timeout_sec`` — макс. ожидание ответа (default ``CONFIRM_WAIT_TIMEOUT_SEC``).
        По таймауту / stop → fail-closed ``deny_once`` (O11).
        """
        wait_timeout = CONFIRM_WAIT_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)

        def _tui_confirm(
            *,
            title: str,
            question: str,
            fields: list,
            via: str | None = None,
        ) -> str:
            self._confirm_event.clear()
            self._confirm_choice = "deny_once"
            try:
                mounted = present(title, question, fields, via=via)
            except TypeError:
                try:
                    mounted = present(title, question, fields)
                except Exception:
                    return "deny_once"
            except Exception:
                return "deny_once"
            # False = explicit fail; None/True — mounted (None = legacy present без ack).
            if mounted is False:
                return "deny_once"
            if self._stop_requested or self._marked_stopped:
                return "deny_once"
            started = time.monotonic()
            # Короткие wait-слойсы: реагируем на stop/cancel без полного timeout.
            deadline = started + max(0.0, wait_timeout)
            while True:
                if self._stop_requested or self._marked_stopped:
                    self._confirm_choice = "deny_once"
                    return "deny_once"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    elapsed = time.monotonic() - started
                    _log.warning(
                        "tui_confirm_timeout title=%r elapsed_sec=%.1f timeout_sec=%.1f → deny_once",
                        title,
                        elapsed,
                        wait_timeout,
                    )
                    self._confirm_choice = "deny_once"
                    return "deny_once"
                if self._confirm_event.wait(timeout=min(remaining, 1.0)):
                    return self._confirm_choice

        return _tui_confirm

    def submit_confirm_reply(self, choice: str) -> None:
        """UI кладёт выбор: allow_remember|allow_once|deny_remember|deny_once.
        Разблокирует агент-поток."""
        self._confirm_choice = choice
        self._confirm_event.set()

    def cancel_confirm(self) -> None:
        """Fail-closed разблокировка confirm (выбор 'deny_once').

        Используется в stop() и в тестах; симметрично cancel_ask_human."""
        self._confirm_choice = "deny_once"
        self._confirm_event.set()

    def cancel_ask_human(self) -> None:
        """Положить пустую строку в stdin-queue — разблокирует agent-thread,
        зависший в input() внутри ask_human-тула.

        Используется как:
        1) часть _stop_for_ask_human (чтобы agent-loop мог дойти до проверки
           stop_requested и корректно завершиться);
        2) для тестов (быстрый unblock без полного pipeline).

        НЕ путать с «cancel» в семантике юзера — у юзера Esc делает clean stop
        всего треда через TraceScreen._stop_for_ask_human(); этот метод —
        только механический stdin-unblock."""
        self._stdin_queue.put("\n")

    def wait(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout=timeout)
