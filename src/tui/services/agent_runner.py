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
        self._stdin_queue: "queue.Queue[str]" = queue.Queue()
        self._prebuild_messages: list[dict] = []
        self._stop_requested = False
        # Вид последней работы воркера: "run" | "compact" | "". Falling-edge
        # стампа терминального статуса (main._poll_active) пропускает "compact",
        # чтобы ручной /compact не перетирал прежний терминал-статус на completed.
        self._thread_kind: str = ""
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
                    executor.compact_thread(thread_id, system=system)
                except Exception as exc:
                    if self._on_error is not None:
                        try:
                            self._on_error(exc)
                        except Exception:
                            pass

            t = threading.Thread(target=_target, name="gc-ua-compact", daemon=True)
            self._thread = t
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
        return {"msgs": msgs, "display_contents": display_contents,
                "skills_loaded": skills_loaded, "message_ids": mids}

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
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_requested = False
            self._thread_kind = "run"

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
                        a = self._run_args(
                            query=query, technical=technical, message_id=message_id,
                            display_query=display_query, skill_loaded=skill_loaded,
                        )
                        executor.run(
                            a["msgs"],
                            thread_id=thread_id,
                            start_new_thread=start_new_thread,
                            system=system,
                            discard_writes_after_stop=True,
                            message_ids=a["message_ids"],
                            display_contents=a["display_contents"],
                            skills_loaded=a["skills_loaded"],
                        )
                        outcome = getattr(executor, "last_terminal_outcome", None)
                        if self._on_complete is not None:
                            try:
                                self._on_complete(outcome)
                            except Exception:
                                pass
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
        present: Callable[..., bool | None],
    ) -> Callable[..., str]:
        """Вернуть замыкание tui_confirm для передачи в ctx.

        Замыкание вызывается на агент-потоке и блокирует его до ответа юзера
        только после sync ack mounted. present(...) → True|False|None; при
        False — сразу deny без wait() (I2). None — legacy present без ack;
        поддерживается опциональный ``via`` для метки child HITL.
        Возвращает ConfirmChoice: 'once'|'session'|'deny'.
        """

        def _tui_confirm(
            *,
            title: str,
            question: str,
            fields: list,
            via: str | None = None,
        ) -> str:
            self._confirm_event.clear()
            self._confirm_choice = "deny"
            try:
                mounted = present(title, question, fields, via=via)
            except TypeError:
                try:
                    mounted = present(title, question, fields)
                except Exception:
                    return "deny"
            except Exception:
                return "deny"
            # False = explicit fail; None/True — mounted (None = legacy present без ack).
            if mounted is False:
                return "deny"
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
