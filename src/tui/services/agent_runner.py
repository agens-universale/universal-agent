"""AgentRunner — обёртка AgentExecutor в daemon Thread (§3 спеки).

Бэкенд-агностична: принимает любой объект с методами
.run(messages, *, thread_id, start_new_thread, system, ...),
.stop_run(), .add_message(message).
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

from tui.services.stdin_bridge import redirect_stdin


class AgentRunner:
    """Управляет жизненным циклом одного run AgentExecutor."""

    def __init__(
        self,
        executor: Any = None,
        *,
        executor_factory: Optional[Callable[[], Any]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
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
        self._stdin_queue: "queue.Queue[str]" = queue.Queue()
        self._prebuild_messages: list[dict] = []
        self._stop_requested = False
        self._confirm_event = threading.Event()
        self._confirm_choice: str = "deny"

    def _ensure_executor(self) -> Any:
        with self._build_lock:
            if self._executor is None:
                if self._executor_factory is None:
                    raise RuntimeError(
                        "AgentRunner: ни executor, ни executor_factory не заданы"
                    )
                self._executor = self._executor_factory()
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
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass

        threading.Thread(target=_target, name="gc-ua-warm", daemon=True).start()

    def is_running(self) -> bool:
        with self._lock:
            t = self._thread
        return t is not None and t.is_alive()

    def start(
        self,
        *,
        query: str,
        thread_id: str,
        start_new_thread: bool = False,
        system: str = "cli",
        message_id: Optional[str] = None,
        technical: Optional[str] = None,
    ) -> bool:
        """Запустить run. Возвращает False если уже работает.

        message_id — id user-сообщения (TUI задаёт свой, чтобы потом точечно
        откатить осиротевшую реплику при быстрой отмене)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_requested = False

            def _target() -> None:
                with redirect_stdin(self._stdin_queue):
                    try:
                        # Сборка executor (incl. MCP-дискавери) — здесь, в worker-потоке,
                        # чтобы не блокировать UI-поток (см. get_runner).
                        executor = self._ensure_executor()
                        for m in self._prebuild_messages:
                            executor.add_message(m)
                        self._prebuild_messages.clear()
                        if self._stop_requested:
                            return
                        msgs = [query] if technical is None else [query, technical]
                        if message_id:
                            mids = [message_id] if technical is None else [message_id, None]
                        else:
                            mids = None
                        executor.run(
                            msgs,
                            thread_id=thread_id,
                            start_new_thread=start_new_thread,
                            system=system,
                            discard_writes_after_stop=True,
                            message_ids=mids,
                        )
                    except Exception as exc:
                        if self._on_error is not None:
                            try:
                                self._on_error(exc)
                            except Exception:
                                pass

            t = threading.Thread(target=_target, name="gc-ua-agent", daemon=True)
            self._thread = t
        t.start()
        return True

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

    def add_message(self, message: dict) -> None:
        # executor может ещё строиться — тогда буферизуем; worker-поток дольёт
        # сообщения сразу после сборки, до запуска run.
        ex = self._executor
        if ex is not None:
            ex.add_message(message)
        else:
            self._prebuild_messages.append(message)

    def submit_ask_human_reply(self, text: str) -> None:
        """TUI кладёт ответ юзера в queue. ask_human читает строки до пустой,
        поэтому шлём ответ + завершающую пустую строку (иначе read-loop виснет)."""
        self._stdin_queue.put(text + "\n")
        self._stdin_queue.put("\n")  # терминатор read-loop в ask_human

    def make_tui_confirm(
        self,
        present: Callable[[str, str, list], None],
    ) -> Callable[..., str]:
        """Вернуть замыкание tui_confirm для передачи в ctx.

        Замыкание вызывается на агент-потоке и блокирует его до ответа юзера.
        present(title, question, fields) — инжектированный UI-presenter; он
        отвечает за маршаллинг в UI-поток и показ баннера.
        Возвращает ConfirmChoice: 'once'|'session'|'deny'.
        """

        def _tui_confirm(*, title: str, question: str, fields: list) -> str:
            self._confirm_event.clear()
            self._confirm_choice = "deny"
            present(title, question, fields)
            self._confirm_event.wait()
            return self._confirm_choice

        return _tui_confirm

    def submit_confirm_reply(self, choice: str) -> None:
        """UI кладёт выбор: once|session|deny. Разблокирует агент-поток."""
        self._confirm_choice = choice
        self._confirm_event.set()

    def cancel_confirm(self) -> None:
        """Fail-closed разблокировка confirm (выбор 'deny').

        Используется в stop() и в тестах; симметрично cancel_ask_human."""
        self._confirm_choice = "deny"
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
