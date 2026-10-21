"""
Потокобезопасный исполнитель агента.

Один мьютекс на экземпляр, держится кратко (доступ к state / pending / stop).
State создаётся заново при каждом run(). Возвращённый из get_state() state
нельзя мутировать снаружи; изменения — только через add_message() или под замком executor'а.
"""

import asyncio
import functools
import gc
import logging
import threading
from copy import copy as _shallow_copy_msg
from dataclasses import dataclass
from typing import Any, Optional

from gc_core.agent_edits.snapshots import purge_thread_snapshots
from gc_core.attachments.model import Attachment, attachments_of
from gc_core.attachments.store import (
    commit_to_run_thread,
    with_committed_attachments,
)
from gc_core.auto_routing import state_ops
from gc_core.config_modules.stored_outputs import (
    get_stored_tool_outputs_max_entries,
)
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.ports import port
from gc_core.execution_workspace import launch_root_from_config
from gc_core.intent_log.user_turn import (
    append_user_ask_deduped,
    append_user_asks_from_messages,
)
from gc_core.mcp_session_owner import reset_owner, set_owner
from gc_core.message_policy.chat_completions import repair_tool_call_pairing
from gc_core.message_policy.plan_messages import (
    PLAN_REMOVED_MESSAGE,
    accepted_plan_messages,
)
from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
    wrap_technical_user_content,
)
from gc_core.model_router import main_model_identity
from gc_core.persistence.session_state import (
    hydrate_state,
    session_from_state,
    shared_from_session,
    usage_from_state,
)
from gc_core.plan_store import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_REWORKING,
    normalize_plan_block,
    now_stamp,
)
from gc_core.processing_responses import (
    _acall_blocking_hook,
    arun_agent,
    begin_turn_main_trace,
)
from gc_core.run_context import RunContext
from gc_core.run_summary_coordinator import (
    RunSummaryCoordinator,
    areset_run_flags,
    aset_tui_finalizing,
    aset_tui_finish_gate_checking,
)
from gc_core.security.action_judge_step import lift_pause_on_user_message
from gc_core.security.permission_mode import (
    PLAN_EXECUTION_MODES,
    Mode,
    coerce_mode,
    resolve_mode,
)
from gc_core.security.permission_store import PermissionStore
from gc_core.settings import settings_of
from gc_core.states import State
from gc_core.summarize.config import (
    resolve_summary_limits_at_level,
)
from gc_core.thread_store import (
    ThreadSession,
    make_message_key,
    resolve_threads_dir,
)
from gc_core.threads.errors import OwnershipError, ThreadBusy
from gc_core.threads.session_writer import LEASE_TTL
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry
from gc_core.tools.connect_mcp.stream_client import (
    _SESSION_MANAGER as _STREAM_SESSION_MANAGER,
)
from gc_core.tools.subagents.active_scope import (
    active_parent_call_id,
    is_child_input_active,
    try_enqueue_user_message,
)
from gc_core.tools.subagents.selection import subagents_enabled
from gc_core.tools.subagents.sidecar import (
    bind_thread_identity,
    bind_turn_identity,
    clear_thread_identity,
    mint_turn_id,
)
from gc_core.trace_id import trace_id_from_config
from gc_core.usage.account import UsageAccount
from gc_core.usage.report import build_run_usage_dict, build_run_usage_report
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)
from gc_core.utils.local_render_system_prompt import render_system_prompt
from gc_core.utils.logging_module import human_console_enabled

ORCHESTRATOR_SPECIFIC_ROLE = (
    "You are an orchestrator agent. You may execute work yourself or delegate to subagents when that clearly pays off.\n"
    "- Handle simple, local tasks yourself (small edits, one-off questions, quick checks) — do not delegate by default.\n"
    "- Delegate only when the task is genuinely complex, parallelizable, or needs a specialized role "
    "(e.g. deep review, large exploration) and delegation tools are listed in `<tools>`.\n"
    "- For complex work, decompose into subtasks and delegate appropriately; validate outcomes yourself "
    "or via a review subagent when quality or risk warrants it.\n"
    "- At the start of a large task, use a planner subagent only when planning overhead is justified.\n"
)


async def _aappend_trace_id_to_meta(
    thread_session: Any, trace_id: str, logger: Any | None = None
) -> None:
    """Дописать trace_id в ``runtime.trace_ids`` треда (без дубля).

    Отвечает на вопрос аудита «какие шаги оркестратора трогали этот тред».
    Ошибки бухгалтерии не должны ронять ран: факт и причину пишем в debug.
    """
    add = getattr(thread_session, "aadd_trace_id", None)
    if add is None:
        return
    try:
        await add(trace_id)
    except ExecutionFailure:
        raise
    except ThreadBusy:
        raise
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug(
                    "trace_id_meta_write_failed error=%s", exc, exc_info=True
                )
            except Exception:
                pass


async def _aclose_mcp_and_release(owner_reset: Any) -> None:
    """Уборка после прогона: stdio-сессии, stream-сессии, владение.

    Отмену КОПИМ, а не пробрасываем сразу. Раньше `CancelledError` из первого
    шага уносил управление мимо всего остального: не закрывались
    stream-сессии, не снималось владение. Отмена приходит ровно тогда, когда
    прибираться нужнее всего — по стопу пользователя или дедлайну, — и именно
    тогда браузер оставался жить. `except Exception` её не ловит: она
    `BaseException`.

    Вынесено из `finally` отдельной функцией не ради красоты: внутри `finally`
    это свойство проверялось бы только копией блока в тесте, то есть никак.
    """
    cancelled: Optional[BaseException] = None
    try:
        # Долгоживущие stdio (напр. @playwright/mcp + Chromium): владелец
        # берётся из contextvar здесь же — закроется только наше, чужой
        # браузер продолжит работать.
        await PersistentStdioRegistry.instance().aclose_all()
    except asyncio.CancelledError as exc:
        cancelled = exc
    except Exception:
        pass
    try:
        # НЕ через to_thread: сессии ключуются по id(loop) и живут на ЭТОМ
        # loop — из чужого потока их не видно и закрыть нечем.
        await _STREAM_SESSION_MANAGER.aclose_all()
    except asyncio.CancelledError as exc:
        cancelled = cancelled or exc
    except Exception:
        pass
    # C2: после закрытия MCP/stdio — подбираем циклический мусор
    # (pipes, ClientSession, loop buffers).
    gc.collect()
    # Владение снимаем последним: всё выше его требует.
    reset_owner(owner_reset)
    if cancelled is not None:
        raise cancelled


def undelivered_as_arun_args(messages: list[dict]) -> tuple[list[str], dict]:
    """Недоставленные сообщения → аргументы `arun` для отдельного хода.

    Владелец доигрывает хвост (`take_undelivered_messages`) новым ходом, а
    `arun` принимает тексты и параллельные списки полей, тогда как в очередь
    кладут готовые словари. Поля переносятся те, из которых `arun` и строит
    сообщение: id (по нему TUI точечно откатывает реплику), display_content,
    skill_loaded и attachments (картинки из кармана процесса — в тред их
    переложит ход, который их понесёт) — других в очередь не кладут ни
    сервер, ни TUI, ни SDK.
    """
    texts = [str(m.get("content", "")) for m in messages]
    return texts, {
        "message_ids": [m.get("id") for m in messages],
        "display_contents": [m.get("display_content") for m in messages],
        "skills_loaded": [m.get("skill_loaded") for m in messages],
        "attachments": [attachments_of(m) or None for m in messages],
    }


def _put_attachments(msg: dict, attachments, i: int, commit) -> None:
    """Вложения ``i``-го сообщения прогона — в ``msg["attachments"]`` словарями: каждое
    проходит через ``commit`` (копия в тред прогона), ``commit=None`` — как есть, в
    кармане. Нет вложений — ключа нет."""
    atts = attachments[i] if attachments and i < len(attachments) else None
    if atts:
        msg["attachments"] = [
            (commit(a) if commit is not None else a).to_dict() for a in atts
        ]


#: Как часто продлевать аренду треда во время хода: треть срока — два
#: сбоя продления подряд ещё не отдают тред другому инстансу.
LEASE_RENEW_SECONDS = LEASE_TTL.total_seconds() / 3


def _start_lease_keeper(
    thread_session: Any, on_lost: Any = None
) -> asyncio.Task | None:
    """Фоновое продление аренды треда, пока идёт цикл. Аренда бессрочная
    (workflow) или треда нет — ``None``. Аренду перехватили — ход
    останавливается сразу (``on_lost``), не дожидаясь следующей записи."""
    if thread_session is None or not getattr(
        thread_session, "renews_lease", False
    ):
        return None

    async def keep() -> None:
        while True:
            await asyncio.sleep(LEASE_RENEW_SECONDS)
            try:
                await thread_session.arenew_lease()
            except ThreadBusy:
                logging.getLogger(__name__).warning(
                    "thread_lease_lost: тред ведёт другой процесс, ход "
                    "останавливается",
                    exc_info=True,
                )
                if on_lost is not None:
                    on_lost()
                return
            except Exception:
                # Временный сбой продления: следующая попытка — по таймеру.
                logging.getLogger(__name__).warning(
                    "thread lease renewal failed", exc_info=True
                )

    return asyncio.create_task(keep())


async def _stop_lease_keeper(keeper: asyncio.Task | None) -> None:
    if keeper is None:
        return
    keeper.cancel()
    await asyncio.gather(keeper, return_exceptions=True)


async def _arelease_lease(thread_session: Any, logger: Any) -> None:
    """Отпустить аренду треда после хода; сбой — предупреждение (аренда
    истечёт сама)."""
    release = getattr(thread_session, "arelease_lease", None)
    if release is None:
        return
    try:
        await release()
    except Exception:
        if logger is not None:
            logger.warning("thread lease release failed", exc_info=True)


#: Метка сообщения входящего потока в очереди хода: его ``seq`` в потоке.
_INBOX_MARK = "_inbox_seq"


class InboxDrained(ValueError):
    """Ход без своих сообщений, а входящий поток пуст: играть нечего."""


async def _arefuse_reset_of_a_busy_thread(store: Any, key: Any) -> None:
    """Сброс треда (``-n``) при живой чужой аренде — ``ThreadBusy``:
    удалить тред, который ведёт другой процесс, значило бы стереть его ход."""
    from gc_core.execution.environment import utc_now
    from gc_core.threads.rules import lease_expired

    record = await store.get_thread(key)
    lease = record.lease if record is not None else None
    if lease is not None and not lease_expired(lease, utc_now()):
        raise ThreadBusy(
            f"тред {key.system}/{key.thread} занят: его ведёт {lease.owner}"
            " — сброс невозможен",
            owner=lease.owner,
        )


def _cancel_boundary(control: list, *, first: int) -> int | None:
    """Граница отмены, касающейся хода, который доставляет входящие с номера
    ``first``: наибольший ``upto_inbox_seq`` отмен не раньше ``first``.
    Отмена прошлого хода (граница меньше ``first``) и запись без границы
    ход не касаются."""
    bounds = [
        entry.data.get("upto_inbox_seq")
        for entry in control
        if entry.data.get("type") == "cancel"
    ]
    applicable = [b for b in bounds if type(b) is int and b >= first]
    return max(applicable) if applicable else None


def _inbox_reader(ctx: Any, thread_session: Any) -> Any:
    """Чтение входящего потока треда хода или ``None``: треда нет, хранилище
    потоков не знает, или ход durable (Temporal: сообщения и отмена
    приходят сигналами, а опрос через activity засорял бы историю
    workflow). Тот же признак включает и управляющий поток."""
    if thread_session is None or getattr(ctx, "durable_execution", False):
        return None
    return getattr(thread_session, "aread_inbox", None)


def _inbox_message(entry: Any) -> dict:
    """Запись входящего потока → сообщение пользователя для хода."""
    message = {
        key: value
        for key, value in entry.data.items()
        if key in ("role", "content", "id", "display_content")
    }
    message.setdefault("role", "user")
    message.setdefault("id", f"m_inbox_{entry.seq}")
    return message


def _take_inbox_marks(messages: list[dict], state: Any) -> list[dict]:
    """Снять метки входящего потока с забранных ходом сообщений и поднять
    ``state.inbox_seq``: следующая запись шага унесёт отметку вместе с ними."""
    out = []
    for message in messages:
        seq = message.get(_INBOX_MARK)
        if seq is None:
            out.append(message)
            continue
        message = {k: v for k, v in message.items() if k != _INBOX_MARK}
        if state is not None:
            state.inbox_seq = max(int(state.inbox_seq or 0), int(seq))
        out.append(message)
    return out


async def _amark_turn(
    ctx: Any, thread_session: Any, *, running: bool, logger: Any
) -> None:
    """Отметка хода в треде (для восстановления после падения процесса).
    Под Temporal не ставится: там ход защищён историей workflow. Сбой —
    предупреждение: ход от отметки не зависит."""
    if thread_session is None or getattr(ctx, "durable_execution", False):
        return
    mark = getattr(thread_session, "amark_turn", None)
    if mark is None:
        return
    try:
        await mark(running=running)
    except ThreadBusy:
        raise
    except Exception:
        (logger or logging.getLogger(__name__)).warning(
            "thread turn mark failed", exc_info=True
        )


async def _apersist_state(
    thread_session: Any,
    state: Any,
    step_messages: list,
    *,
    rendered_system_prompt: str | None,
) -> None:
    """Шаг в тред: сообщения и состояние хода (части записи v2)."""
    thread_session.backend_api_mode = state.backend_api_mode
    session = session_from_state(
        thread_session.state,
        state,
        notice=thread_session.notice,
        rendered_system_prompt=rendered_system_prompt,
        backend_api_mode=state.backend_api_mode,
        previous_response_id=state.previous_response_id,
    )
    await thread_session.apersist_batch(
        step_messages,
        previous_response_id=state.previous_response_id,
        session=session,
        usage=usage_from_state(state),
    )


def _require_plan_execution_mode(mode: str) -> str:
    """План исполняется только в режиме исполнения (edit, auto, manual).

    Возвращает нормализованное имя режима (``"Auto"`` → ``"auto"``)."""
    resolved = coerce_mode(mode)
    if resolved not in PLAN_EXECUTION_MODES:
        raise ValueError(f"not a plan execution mode: {mode!r}")
    return resolved.value


def _require_supported_persist_mode(settings: Any, ctx: Any) -> None:
    """``persist: turn`` — только у durable-исполнителя (Temporal).

    Без истории durable-исполнителя несохранённые шаги пропали бы при падении
    процесса, а UI не видел бы хода."""
    if settings.thread_store.persist == "turn" and not getattr(
        ctx, "durable_execution", False
    ):
        raise ValueError(
            "thread_store.persist: turn is supported only by a durable "
            "executor (Temporal); local runs use persist: step"
        )


def _extend_with_thread_history(
    state: Any, thread_session: Any, backend_api_mode: str
) -> None:
    """chat_completions: история треда — копиями сообщений в ``state.messages``."""
    if (
        thread_session is not None
        and backend_api_mode == "chat_completions"
        and thread_session.history_messages
    ):
        state.messages.extend(
            [
                _shallow_copy_msg(m)
                for m in thread_session.history_messages
                if isinstance(m, dict)
            ]
        )


@dataclass(frozen=True)
class _RunRequest:
    """Аргументы хода ``arun`` для его части на взятом треде."""

    messages: list[str]
    thread_id: str | None
    verbose: bool
    skills_list: Any
    backend_api_mode: str
    logger: Any
    discard_writes_after_stop: bool
    message_ids: list[str] | None
    display_contents: list[str | None] | None
    skills_loaded: list[str | None] | None
    attachments: list[list[Attachment] | None] | None
    account_run: bool


class AgentExecutor:
    """
    Потокобезопасный запуск агента с возможностью добавлять сообщения во время run
    и останавливать выполнение через stop_run().
    """

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        lock_factory = getattr(ctx, "lock_factory", None) or threading.Lock
        self._lock = lock_factory()
        self._pending_messages: list[dict] = []
        #: То, что прогон не успел съесть, снятое с очереди на выходе из цикла.
        #: Держим отдельно, а не выбрасываем: клиенту на эти сообщения уже
        #: ответили «принято», и молча терять их нельзя. Забирает владелец —
        #: `take_undelivered_messages`, вместе с пришедшим после.
        self._undelivered_messages: list[dict] = []
        #: Последний ``seq`` входящего потока, уже положенный в очередь хода.
        self._inbox_seen = 0
        #: Последний ``seq`` управляющего потока на старте хода: отмены до
        #: него к этому ходу не относятся.
        self._control_seen = 0
        self._state: State | None = None
        # Сессия последнего arun. Её мету пишет aclose_run_accounting, один раз.
        self._thread_session: Any = None
        self._accounting_closed: bool = False
        # stop_run() до создания state (run() ещё в setup-фазе): запоминаем, и
        # run() применит флаг сразу при создании state, до первого persist.
        self._stop_pending: bool = False
        # Явный выбор режима разрешений (хоткей фронтенда, флаг запуска). Живёт
        # весь срок исполнителя и ложится на состояние каждого прогона: выбор
        # человека сильнее и сохранённого в сессии, и конфига.
        self._permission_mode_choice: Optional[str] = None
        # Отложенный переход статуса плана (одобрение из фронтенда):
        # (статус, режим, тред). Применяется в начале следующего прогона и
        # только к своему треду — см. _apply_plan_changes.
        self._plan_transition_pending: Optional[
            tuple[str, Optional[str], Optional[str]]
        ] = None
        # Человек нажал «Доработать план» в этом треде: статус сменится, когда
        # он реально отправит сообщение.
        self._plan_rework_thread: Optional[str] = None
        self.last_terminal_outcome: Optional[str] = None

    def take_undelivered_messages(self) -> list[dict]:
        """Забрать всё, что отдано прогону, но до агента не дошло, — в порядке прихода.

        Это хвост, снятый с очереди на выходе из цикла, и то, что легло в
        очередь уже после: цикл вычерпывает её в последний раз при закрытии
        хода, а прогон ещё сохраняет тред и сносит MCP-сессии — через `await`,
        и не быстро. Звать ПОСЛЕ `arun`: из идущего прогона это украло бы
        очередь у цикла.

        Владелец (сервер, TUI) доигрывает хвост отдельным ходом того же треда
        (`undelivered_as_arun_args`) — пока он этого не сделал, прогон нельзя
        объявлять законченным. Если не забрать, пришедшее после `finally`
        уедет следующему прогону вслед за его собственным запросом, а снятое
        `finally` так и останется лежать.
        """
        with self._lock:
            out = [*self._undelivered_messages, *self._pending_messages]
            self._undelivered_messages = []
            self._pending_messages = []
            return out

    def _take_pending_messages(self) -> list[dict]:
        with self._lock:
            out = self._pending_messages[:]
            self._pending_messages.clear()
            state = self._state
        out = _take_inbox_marks(out, state)
        # Сообщение из очереди — такое же новое слово человека, как
        # стартовое: снимает паузу судьи и посреди прогона.
        if state is not None:
            self._lift_judge_pause(state, out)
        # Вне замка: копия картинок в тред — файловая операция, а замок ждут
        # поток TUI и обработчик сервера.
        return self._commit_queued(out)

    def _is_stop_requested(self) -> bool:
        with self._lock:
            return (
                self._state.stop_requested
                if self._state is not None
                else False
            )

    async def _alist_skills(self):
        loader = port(self._ctx, "skills_loader")
        if loader is None:
            loader = self._ctx.skills_registry.list_skills
        return await loader()

    async def _aopen_thread_session(
        self,
        thread_id,
        *,
        start_new_thread: bool,
        system,
        backend_api_mode: str,
        logger,
    ):
        """Тред-сессия прогона → (thread_session, thread_shared, previous_response_id, system_id)."""
        system_id = system or "cli"
        if thread_id is None:
            clear_thread_identity(self._ctx)
            return None, None, None, system_id
        if start_new_thread:
            # Сессии ещё нет, поэтому хранилище берём из контекста; нет и
            # там — файловое, как раньше.
            _reset_store = getattr(self._ctx, "thread_store", None)
            if _reset_store is None:
                from gc_core.threads.file_store import FileThreadStore

                _reset_store = FileThreadStore(resolve_threads_dir(self._ctx))
            from gc_core.thread_paths import sanitize_system_id
            from gc_core.threads.models import ThreadKey
            from gc_core.utils.generate_message_id import (
                generate_msg_id_sha256,
            )

            reset_key = ThreadKey(
                system=sanitize_system_id(system_id), thread=thread_id
            )
            await _arefuse_reset_of_a_busy_thread(_reset_store, reset_key)
            try:
                await _reset_store.delete(
                    reset_key, op_id=generate_msg_id_sha256("reset")
                )
            except OwnershipError as exc:
                # Тред ведёт durable-исполнитель — не сбрасывается.
                raise ThreadBusy(str(exc)) from exc
            # Снимки баз — не забота хранилища: тем же ``reset`` ходит
            # запасной путь компакции, где сносить базы отката нельзя.
            # Пользовательский сброс знает, что чистит, и чистит здесь.
            # Вне цикла событий: снимков у долгого треда тысячи, и снос
            # каталога прямо в корутине подвешивал бы старт нового треда.
            await asyncio.to_thread(
                purge_thread_snapshots,
                resolve_threads_dir(self._ctx),
                system_id,
                thread_id,
                launch_root=launch_root_from_config(self._ctx),
            )
        thread_session = await ThreadSession.afrom_config(
            settings=self._ctx,
            system_id=system_id,
            thread_id=thread_id,
            # Хранилище встраивающего, если он его передал. None — файловое
            # по умолчанию, ровно прежнее поведение.
            store=getattr(self._ctx, "thread_store", None),
            # Тред берётся до чтения истории: чужой живой ход — ThreadBusy
            # до любой работы, а прочитанное не устареет под чужой записью.
            claim=True,
        )
        await self._aon_thread_claimed(thread_session, logger)
        if logger is not None:
            # Единственная связка «этот лог-файл ↔ этот тред»: на момент
            # создания логгера тред ещё не разрешён, поэтому в agent_start
            # его нет, а RunContextFilter ставит только run_id. Без этой
            # записи тред виден в логе лишь косвенно — через сообщения
            # суммаризации, и только если она вообще срабатывала.
            #
            # Значения продублированы в тексте сообщения намеренно: поля из
            # extra= рендерит только JsonlFormatter, а при logging.format:
            # text (дефолт) в строке осталось бы голое имя события.
            logger.info(
                "thread_bound thread_id=%s system_id=%s",
                thread_session.thread_id,
                thread_session.system_id,
                extra={
                    "event": "thread_bound",
                    "thread_id": thread_session.thread_id,
                    "system_id": thread_session.system_id,
                },
            )
        previous_response_id = None
        if backend_api_mode == "chat_completions":
            thread_session.history_messages = repair_tool_call_pairing(
                thread_session.history_messages,
                keep_latest_unanswered=False,
            )
        if backend_api_mode == "responses":
            previous_response_id = thread_session.previous_response_id
        thread_shared = shared_from_session(
            thread_session.state, thread_session.extra
        )
        # Sidecar nested-trace: identity для child ctx (независимо от TUI sink).
        bind_thread_identity(
            self._ctx,
            threads_dir=thread_session.threads_dir,
            system_id=thread_session.system_id,
            thread_id=thread_session.thread_id,
        )
        return thread_session, thread_shared, previous_response_id, system_id

    async def _aon_thread_claimed(self, thread_session, logger) -> None:
        """Тред взят: отметить ход и сказать фронтенду (статус задачи
        вебхука пишет только взявший тред ход). Сбой хука — предупреждение."""
        await _amark_turn(
            self._ctx, thread_session, running=True, logger=logger
        )
        hook = getattr(self._ctx, "on_thread_claimed", None)
        if hook is None:
            return
        try:
            await hook(thread_session.system_id, thread_session.thread_id)
        except Exception:
            (logger or logging.getLogger(__name__)).warning(
                "on_thread_claimed hook failed", exc_info=True
            )

    async def _amcp_snapshot(
        self, thread_session, thread_id, system_id: str
    ) -> None:
        # Снимок MCP «сервер→тулы» для TUI-экрана /mcp (необязателен — не валим run).
        if thread_id is None or not settings_of(self._ctx).mcp.servers:
            return
        try:
            from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot

            snap = build_mcp_tools_snapshot(
                self._ctx, self._ctx.tools_data, self._ctx.mcp_status
            )
            await thread_session.aset_mcp_tools(snap)
        except Exception:
            pass

    async def _aload_chat_completions_prompts(
        self, *, effective_verbose: bool, logger
    ) -> tuple:
        """(system prompt, special instructions) для chat/completions из файлов конфига.

        Без `prompt_loader` в контексте файлы читаются здесь же, как в develop;
        с ним (Temporal native) чтение уходит в загрузчик рантайма.
        """
        from gc_core.agent_store import agent_store_of
        from gc_core.run_preparation import (
            PromptPreparationRequest,
            aload_run_prompts,
        )

        request = PromptPreparationRequest.from_config(
            self._ctx, verbose=effective_verbose
        )
        loader = port(self._ctx, "prompt_loader")
        if loader is None:
            prepared = await aload_run_prompts(
                request, logger=logger, store=agent_store_of(self._ctx)
            )
        else:
            prepared = await loader(request)
        return prepared.system_prompt, prepared.special_instructions

    async def _abuild_run_state(
        self,
        *,
        effective_verbose,
        skills_list,
        chat_completions_system_prompt,
        special_instructions,
        thread_id,
        thread_shared,
        thread_session,
        backend_api_mode: str,
        logger,
    ) -> tuple:
        """State прогона и отрендеренный system prompt (chat/completions) либо None."""
        settings = settings_of(self._ctx)
        state_data = {
            "limit_project_metadata": settings.agent.limit_project_metadata,
            "parent_state_id": None,
            "verbose": effective_verbose,
            "model": settings.agent.model,
            "tools_spec": self._ctx.tools_spec,
            "tools_descr": self._ctx.tools_description,
            "max_steps": settings.agent.max_steps,
            "skills_list": skills_list,
            "system_prompt_id": settings.agent.system_prompt_id,
            "chat_completions_system_prompt": chat_completions_system_prompt,
            "use_server_subagents": settings.agent.use_server_subagents,
            "agent_md_path": settings.paths.agent_md_path,
            "is_tui": bool(getattr(self._ctx, "is_tui", False)),
            "agentsmd_override": settings.tui.agentsmd_override,
            "thread_id": thread_id,
            "shared": thread_shared,
            "workspace_config": self._ctx,
            "logger": logger,
            "workspace": getattr(self._ctx, "workspace", None),
        }
        if subagents_enabled(settings.agent.use_local_subagents):
            state_data["specific_role"] = ORCHESTRATOR_SPECIFIC_ROLE
        preparer = port(self._ctx, "state_preparer")
        if preparer is None:
            state = await State.acreate(**state_data)
        else:
            from gc_core.state_preparation import StatePreparationRequest

            prepared = await preparer(
                StatePreparationRequest(
                    is_tui=state_data["is_tui"],
                    agentsmd_override=state_data["agentsmd_override"],
                    verbose=effective_verbose,
                )
            )
            state = State.from_prepared(prepared, **state_data)
        attach = getattr(self._ctx, "attach_state", None)
        if callable(attach):
            attach(state)
        # Граница хода сбрасывает счётчики; модель выбирается перед вызовом.
        begin_turn_main_trace(state, self._ctx)
        state.tool_call_outputs_store.set_max_entries(
            get_stored_tool_outputs_max_entries(settings)
        )
        state.backend_api_mode = backend_api_mode
        state.on_messages_evicted = getattr(
            self._ctx, "on_messages_evicted", None
        )
        state.dialog_steps_offset = 0
        if thread_session is not None:
            hydrate_state(
                state,
                thread_session.state,
                thread_session.usage,
                thread_session.notice,
            )
        rendered_system_prompt: str | None = None
        if (
            backend_api_mode == "chat_completions"
            and isinstance(chat_completions_system_prompt, str)
            and chat_completions_system_prompt.strip()
        ):
            # Для chat/completions backend'а system prompt должен быть в истории сообщений.
            # Делаем это на уровне state-сессии, чтобы transport/formatting не знал
            # про источники/файлы системного промпта.
            rendered_system_prompt = render_system_prompt(
                system_prompt=chat_completions_system_prompt.strip(),
                tools_spec=state.tools,
                project_info=state.initial_project_data,
                specific_role=state.specific_role,
                special_instructions=special_instructions,
            )
            state.messages = [
                {
                    "role": "system",
                    "content": rendered_system_prompt,
                    "id": generate_message_id_func("m_sys"),
                }
            ]
        return state, rendered_system_prompt

    def _bind_state(self, state, *, discard_writes_after_stop: bool) -> None:
        with self._lock:
            self._state = state
            self._state.discard_writes_after_stop = bool(
                discard_writes_after_stop
            )
            # Применить stop, пришедший пока state ещё не существовал (setup-фаза),
            # чтобы первый же check_stop вышел ДО persist user-сообщения.
            if self._stop_pending:
                self._state.stop_requested = True
                self._stop_pending = False
            # В shared уже лежит выбор, восстановленный из сессии; явный выбор
            # этого запуска — поверх. Под тем же замком, что публикует state:
            # иначе между публикацией и наложением вклинился бы
            # set_permission_mode и его значение было бы затёрто.
            if self._permission_mode_choice is not None:
                self._state.shared.permission_mode = (
                    self._permission_mode_choice
                )
            # Очередь здесь НЕ сбрасываем. Сброс стоял тут с тех пор, когда до
            # него не было setup-фазы и класть сообщения заранее было некому.
            # Теперь кладут: сервер, пока прогон ещё не дошёл до цикла,
            # `_prebuild_messages` в TUI, `AsyncAgent.send` — и всё это
            # стиралось, не доходя до агента. Пустой очередь между прогонами
            # НЕ бывает по построению: сообщение может лечь в неё и после
            # последнего забора циклом — пока прогон сохраняет тред и сносит
            # MCP. Такой хвост владелец забирает `take_undelivered_messages` и
            # доигрывает отдельным ходом. Не забранное из очереди уходит этому
            # прогону вслед за его собственными сообщениями (см. ниже), а
            # отложенное `finally` прошлого прогона ждёт владельца.

    def accept_plan(self, mode: str, thread_id: Optional[str]) -> None:
        """Человек принял план и выбрал режим исполнения.

        Фронтенд зовёт это ДО запуска продолжения; статус плана сменится в
        начале следующего прогона. Переход одноразовый и привязан к треду:
        если продолжение не стартовало, он не должен примениться к прогону
        другой сессии.
        """
        with self._lock:
            self._plan_transition_pending = (
                PLAN_STATUS_APPROVED,
                mode,
                thread_id,
            )

    def arm_plan_rework(self, thread_id: Optional[str]) -> None:
        """Человек выбрал «Доработать план»: следующее его сообщение в этом
        треде переведёт план в доработку."""
        with self._lock:
            self._plan_rework_thread = thread_id

    def prepare_accepted_plan(
        self, mode: str, thread_id: Optional[str], *, extra_text: str = ""
    ) -> list[str]:
        """Человек одобрил предложенный план: режим исполнения, одобрение и
        сообщения продолжения — без прогона.

        Для фронтенда, который ведёт прогон своим путём (ACP — потоком дельт
        ``AsyncAgent.stream``); остальные зовут ``aexecute_accepted_plan``.
        Не режим исполнения — ``ValueError`` до любых изменений.
        ``extra_text`` — уточнение человека к исполнению, отдельным
        сообщением после служебного «план одобрен».
        """
        mode = _require_plan_execution_mode(mode)
        self.set_permission_mode(mode)
        self.accept_plan(mode, thread_id)
        return accepted_plan_messages(mode, extra_text=extra_text)

    async def aexecute_accepted_plan(
        self,
        mode: str,
        thread_id: Optional[str],
        *,
        extra_text: str = "",
        verbose: Optional[bool] = None,
        account_run: bool = True,
        system: Optional[str] = None,
    ) -> Any:
        """Человек одобрил предложенный план: исполнить его в выбранном режиме.

        Одна последовательность на фронтенды — консольный вопрос и
        ``--approve-plan`` gc-cli (ACP — через ``prepare_accepted_plan``).
        Негодный режим — ``ValueError`` до любых действий, и учёт прошлого
        прогона тоже не трогается. Учёт закрывается ДО смены режима: его
        мета пишется такой, какой была на его конце; закрытый учёт и
        прогон, которого в этом процессе не было, — no-op.
        """
        _require_plan_execution_mode(mode)
        await self.aclose_run_accounting()
        messages = self.prepare_accepted_plan(
            mode, thread_id, extra_text=extra_text
        )
        return await self.arun(
            messages,
            thread_id=thread_id,
            system=system,
            verbose=verbose,
            account_run=account_run,
        )

    def plan_snapshot(
        self, expected_thread_id: Optional[str]
    ) -> Optional[dict]:
        """План указанного треда из живого состояния; None — другой тред,
        состояния нет или плана нет. Пустой id — всегда несовпадение."""
        with self._lock:
            state = self._state
            if (
                state is None
                or not expected_thread_id
                or getattr(state, "thread_id", None) != expected_thread_id
            ):
                return None
            return normalize_plan_block(getattr(state.shared, "plan", None))

    def _apply_plan_changes(self, state, new_user_messages) -> None:
        """Применить к плану то, что человек решил между прогонами.

        Оба намерения гасятся в начале ЛЮБОГО прогона и действуют только на
        свой тред: невидимое взведённое состояние не должно пережить момент,
        когда его создали, и выстрелить в чужой сессии.
        """
        with self._lock:
            pending = self._plan_transition_pending
            rework_thread = self._plan_rework_thread
            self._plan_transition_pending = None
            self._plan_rework_thread = None
        thread_id = getattr(state, "thread_id", None)
        plan = getattr(state.shared, "plan", None)
        if not thread_id or not isinstance(plan, dict):
            return
        if pending is not None and pending[2] == thread_id:
            status, approved_mode, _thread = pending
            plan["status"] = status
            plan["approved_mode"] = approved_mode
            plan["updated_at"] = now_stamp()
            # Выход из plan через одобрение: «план не одобрен» слать нельзя
            # — продолжение несёт своё «план одобрен».
            if (
                status == PLAN_STATUS_APPROVED
                and state.mode_announced == Mode.PLAN.value
            ):
                state.mode_announced = None
            return
        # «В доработке» — только по двум шагам сразу: человек нажал «Доработать»
        # И отправил обычное сообщение, оставаясь в plan. Иначе вопрос не по
        # теме после закрытого баннера уводил бы план в доработку.
        first = new_user_messages[0] if new_user_messages else None
        if (
            rework_thread == thread_id
            and first is not None
            and extract_real_user_message_text(first)
            and resolve_mode(self._ctx) is Mode.PLAN
        ):
            plan["status"] = PLAN_STATUS_REWORKING
            plan["approved_mode"] = None
            plan["updated_at"] = now_stamp()

    async def _aexport_plan_file(self, state, logger) -> None:
        """Файл плана для человека — раз в конце прогона, после записи меты.

        Пишет хранилище, если умеет (файловое); сбой экспорта прогон не
        роняет: файл — удобство, канон плана уже сохранён в состоянии треда.
        """
        session = self._thread_session
        plan = getattr(state.shared, "plan", None)
        if session is None or not plan:
            return
        try:
            await session.asave_plan_file(plan)
        except Exception as exc:
            if logger is not None:
                logger.warning("plan file export failed: %s", exc)

    def clear_plan(self, thread_id: Optional[str]) -> None:
        """Человек убрал план этой сессии: взведённые решения по нему (утвердить,
        доработать) больше не к чему применять."""
        with self._lock:
            pending = self._plan_transition_pending
            if pending is not None and pending[2] == thread_id:
                self._plan_transition_pending = None
            if self._plan_rework_thread == thread_id:
                self._plan_rework_thread = None

    @staticmethod
    def _lift_judge_pause(state, new_user_messages) -> None:
        """Новое сообщение человека снимает паузу судьи
        (`lift_pause_on_user_message`). Служебные сообщения человеком не
        считаются."""
        if any(extract_real_user_message_text(m) for m in new_user_messages):
            lift_pause_on_user_message(state.shared)

    def _plan_notices(self, state) -> list[str]:
        """Служебное сообщение в начале прогона, один раз: «план убран
        человеком». О режиме агенту говорит цикл перед каждым запросом к
        модели (``processing_responses._mode_notices``)."""
        shared = state.shared
        if not shared.plan_removed_notice:
            return []
        shared.plan_removed_notice = False
        return [PLAN_REMOVED_MESSAGE]

    @staticmethod
    def _seed_current_user_intent(state, new_user_messages) -> None:
        # На старте user turn дописываем текущий CLI-запрос в current_user_intent
        # (только реальные user-сообщения, не technical). user_intent_log — только
        # user_intent_log — только после flush при accept turn_finish.
        for new_user_message in new_user_messages:
            state.finish_gate_rejection_count = 0
            text = extract_real_user_message_text(new_user_message)
            if text:
                state.current_user_intent = append_user_ask_deduped(
                    state.current_user_intent,
                    text,
                    user_turn_seq=state.user_turn_seq,
                )

    def _thread_persist_callback(
        self,
        *,
        thread_session,
        state,
        backend_api_mode: str,
        rendered_system_prompt,
    ):
        async def _persist_thread_batch(step_messages: list[dict]) -> None:
            if is_persist_discarded(state):
                return
            known_before = set(thread_session.known_message_keys)
            step_messages_for_user_scope = [
                m
                for m in step_messages
                if make_message_key(m) not in known_before
            ]
            state.current_user_intent = append_user_asks_from_messages(
                state.current_user_intent,
                step_messages_for_user_scope,
                user_turn_seq=state.user_turn_seq,
            )
            await _apersist_state(
                thread_session,
                state,
                step_messages,
                rendered_system_prompt=(
                    rendered_system_prompt
                    if backend_api_mode == "chat_completions"
                    else None
                ),
            )

        return _persist_thread_batch

    def _persistence_for_run(
        self,
        *,
        thread_session,
        state,
        backend_api_mode: str,
        rendered_system_prompt,
        previous_response_id,
        new_user_messages,
        summary_trigger_chars: int,
        summary_trigger_tokens: int,
    ) -> tuple:
        """(messages для run_agent, persist_messages_callback, get_full_history_messages)."""
        messages = list(new_user_messages)
        if thread_session is not None:
            state.previous_response_id = previous_response_id
            thread_session.set_runtime_messages(state.messages)

            # chat/completions: история уже в state.messages (extend выше).
            # В run_agent передаём только новый user-батч — backend собирает payload
            # из state + batch без дублей (см. create_call_data_completions).
            if backend_api_mode == "chat_completions":
                base_messages = list(thread_session.history_messages)
                state.skip_initial_user_ask_step_when_history = (
                    len(base_messages) > 0
                )
                messages = list(new_user_messages)
            else:
                base_messages = (
                    list(thread_session.history_messages)
                    if previous_response_id is None
                    else []
                )
                state.skip_initial_user_ask_step_when_history = (
                    len(base_messages) > 0
                )
                messages = [*base_messages, *new_user_messages]
            persist = self._thread_persist_callback(
                thread_session=thread_session,
                state=state,
                backend_api_mode=backend_api_mode,
                rendered_system_prompt=rendered_system_prompt,
            )
            return messages, persist, thread_session.get_full_history_messages
        if backend_api_mode == "chat_completions" and (
            summary_trigger_chars > 0 or summary_trigger_tokens > 0
        ):

            def _persist_runtime_batch(step_messages: list[dict]) -> None:
                if is_persist_discarded(state):
                    return
                state.current_user_intent = append_user_asks_from_messages(
                    state.current_user_intent,
                    step_messages,
                    user_turn_seq=state.user_turn_seq,
                )

            return messages, _persist_runtime_batch, None
        return messages, None, None

    async def _afinalize_thread(self, thread_session, state, logger) -> None:
        if thread_session is None or (
            state.discard_writes_after_stop and state.stop_requested
        ):
            return
        await _apersist_state(
            thread_session, state, [], rendered_system_prompt=None
        )
        # persist: turn — конец хода и есть точка сброса.
        flush = getattr(thread_session, "aflush_deferred", None)
        if flush is not None:
            await flush()
        _run_trace_id = trace_id_from_config(self._ctx)
        if _run_trace_id:
            # После persist_batch: он пересобирает мету с диска, поэтому
            # запись должна быть последней, иначе её затрёт.
            await _aappend_trace_id_to_meta(
                thread_session, _run_trace_id, logger
            )

    def _report_run_usage(
        self, state, run_start_usage, logger, *, console: bool = True
    ) -> None:
        # Отчёт по токенам/стоимости за этот прогон (дельта от снимка до старта).
        # ``console=False`` — тот же отчёт только в лог, без таблицы в stdout.
        settings = settings_of(self._ctx)
        try:
            # Тариф — по имени модели, валюта — по адресу: модель main из
            # реестра models, а не легаси agent.model/backend.completions_url.
            model_name, endpoint_url = main_model_identity(
                settings, self.get_model_router()
            )
            run_usage = state.usage.delta_from(run_start_usage)
            # Доступно in-memory: get_state().usage (кумулятив) и .last_run_usage (этот прогон).
            state.last_run_usage = run_usage
            usage_text = build_run_usage_report(
                run_usage,
                settings.pricing,
                model_name,
                endpoint_url,
                provider_currency=getattr(
                    self._ctx, "provider_currency", None
                ),
            )
            if usage_text:
                if logger is not None:
                    logger.info(
                        "\n<run_usage>\n%s\n</run_usage>\n", usage_text
                    )
                if console and human_console_enabled(settings):
                    print(f"\n{usage_text}\n")
            if logger is not None:
                usage_dict = build_run_usage_dict(
                    run_usage,
                    settings.pricing,
                    model_name,
                    endpoint_url,
                    provider_currency=getattr(
                        self._ctx, "provider_currency", None
                    ),
                )
                if usage_dict:
                    logger.info(
                        "run_usage",
                        extra={"event": "run_usage", "usage": usage_dict},
                    )
        except Exception as usage_exc:  # отчёт не должен ронять прогон
            if logger is not None:
                logger.warning(
                    "run usage report failed: %s", usage_exc, exc_info=True
                )

    async def aclose_run_accounting(self, *, console: bool = True) -> None:
        """Закрыть прогон: один раз записать мету треда и отчёт usage.

        К этому моменту цикл и все шаги после него уже прошли. Повторный
        вызов ничего не пишет. ``arun`` зовёт метод сам. Кто после цикла ещё
        работает, передаёт ``account_run=False`` и зовёт его после своих шагов.

        Ошибка записи меты уходит вызывающему: прогон без сохранённого
        состояния не выглядит успешным. Сбой отчёта usage только пишется
        в лог.
        """
        if self._accounting_closed:
            return
        state = self.get_state()
        if state is None:
            return
        logger = getattr(self._ctx, "logger", None)
        await self._afinalize_thread(self._thread_session, state, logger)
        self._accounting_closed = True
        await self._aexport_plan_file(state, logger)
        # Последняя запись хода — тред свободен для другого инстанса.
        await _amark_turn(
            self._ctx, self._thread_session, running=False, logger=logger
        )
        await _arelease_lease(self._thread_session, logger)
        try:
            baseline = UsageAccount.from_meta(
                getattr(state, "run_start_usage_meta", None)
            )
            self._report_run_usage(state, baseline, logger, console=console)
        except Exception as usage_exc:
            if logger is not None:
                logger.warning(
                    "run usage report failed: %s",
                    usage_exc,
                    exc_info=True,
                )

    async def _apersist_finished_loop_during_cancel(self) -> None:
        """Записать учёт уже завершённого цикла, хотя уборку MCP сняли.

        В снятой задаче следующий ``await`` сразу получает ``CancelledError``,
        и запись не начинается. Снимаем отмену и пишем. ``arun`` затем бросает
        ``CancelledError`` заново — вызывающий её видит. Счётчик не
        восстанавливаем: иначе задача останется снятой даже после того,
        как вызывающий эту ошибку поймает.
        """
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        await self.aclose_run_accounting()

    async def _ateardown_run(self, owner_reset: Any) -> BaseException | None:
        # Keep the unconsumed queue before the await; messages arriving during
        # MCP cleanup remain pending and are returned by take_undelivered_messages.
        with self._lock:
            # Сообщения входящего потока остаются в потоке: их доиграет
            # рукопожатие или следующий ход, отметка доставки их не покрыла.
            self._undelivered_messages.extend(
                m for m in self._pending_messages if _INBOX_MARK not in m
            )
            self._pending_messages = []
        try:
            cleanup = port(self._ctx, "mcp_cleanup")
            if cleanup is None:
                await _aclose_mcp_and_release(owner_reset)
            else:
                try:
                    await cleanup()
                except Exception:
                    # Как локальная уборка: сбой закрытия MCP не отменяет
                    # готовый ответ хода.
                    logger = getattr(self._ctx, "logger", None)
                    if logger is not None:
                        logger.warning("mcp_cleanup_failed", exc_info=True)
                finally:
                    reset_owner(owner_reset)
        except asyncio.CancelledError as exc:
            # arun persists accounting for a completed loop before re-raising.
            return exc
        return None

    def _report_log_path(self, logger: Any) -> None:
        if human_console_enabled(self._ctx):
            print(f"\nПуть к файлу с логами: {self._ctx.logging_path}\n")
        if logger is not None:
            logger.info(
                "log_path",
                extra={
                    "event": "log_path",
                    "path": getattr(self._ctx, "logging_path", None),
                },
            )

    async def arun(
        self,
        messages: list[str],
        *,
        thread_id: str | None = None,
        start_new_thread: bool = False,
        system: str | None = None,
        verbose: bool | None = None,
        discard_writes_after_stop: bool = False,
        message_ids: list[str] | None = None,
        display_contents: list[str | None] | None = None,
        skills_loaded: list[str | None] | None = None,
        attachments: list[list[Attachment] | None] | None = None,
        account_run: bool = True,
    ) -> Any:
        """Прогон агента, не блокирующий вызывающий loop.

        При каждом вызове state создаётся заново. messages — непустой список
        текстов, каждый уходит отдельным user-сообщением. Пустой — ход из
        входящего потока треда (``Threads.submit``); пуст и он —
        ``InboxDrained`` (``ValueError``).

        attachments — вложения по сообщениям, параллельно messages (как
        display_contents): элемент — картинки своего сообщения из кармана
        процесса (``ImageStore.stage``) или None. В тред их копирует сам ход
        (``commit_to_run_thread``); текст сообщения остаётся строкой с
        плашками ``[Изображение #N]``.

        Форма ОДНА. Синхронного ``run`` нет: из синхронного кода агента
        ведут своим ``asyncio.run`` в рабочем потоке — так делают TUI и
        harbor-адаптер.

        ``account_run=False`` — не закрывать учёт внутри. Вызывающий ещё
        добавит шаги после цикла и сам вызовет ``aclose_run_accounting``.
        Отмена закрытия MCP после уже завершённого цикла учёт всё равно
        записывает и затем пробрасывается: дополнительного шага уже не будет.
        """
        _require_supported_persist_mode(settings_of(self._ctx), self._ctx)
        response = await self._arun_turn(
            messages,
            thread_id=thread_id,
            start_new_thread=start_new_thread,
            system=system,
            verbose=verbose,
            discard_writes_after_stop=discard_writes_after_stop,
            message_ids=message_ids,
            display_contents=display_contents,
            skills_loaded=skills_loaded,
            attachments=attachments,
            account_run=account_run,
        )
        # Рукопожатие: тред отпущен, а во входящий поток успело лечь
        # сообщение после последнего забора — ещё ход тем же исполнителем.
        # Не взяли тред — его уже ведёт другой, он и заберёт.
        while account_run and await self.ainbox_left():
            try:
                response = await self._arun_turn(
                    [],
                    thread_id=thread_id,
                    system=system,
                    verbose=verbose,
                    discard_writes_after_stop=discard_writes_after_stop,
                )
            except (ThreadBusy, InboxDrained):
                break
        return response

    async def _arun_turn(
        self,
        messages: list[str],
        *,
        thread_id: str | None = None,
        start_new_thread: bool = False,
        system: str | None = None,
        verbose: bool | None = None,
        discard_writes_after_stop: bool = False,
        message_ids: list[str] | None = None,
        display_contents: list[str | None] | None = None,
        skills_loaded: list[str | None] | None = None,
        attachments: list[list[Attachment] | None] | None = None,
        account_run: bool = True,
    ) -> Any:
        """Один ход ``arun``: тред, подготовка, цикл, учёт."""
        with self._lock:
            # State прошлого хода снимаем ПЕРВЫМ делом. Пока он жил до конца
            # объекта, `stop_run` в сетапе этого хода (чтение треда, сборка
            # состояния, остановка uvicorn) писал флаг в мёртвый state: новый
            # стартовал чистым, и ход доигрывался целиком — с вызовами модели и
            # тулов, уже после нажатия стопа. Второй ход у владельца исполнителя
            # обычное дело: хвост прогона у сервера и SDK, следующий запрос в
            # TUI. Теперь такой стоп ложится в `_stop_pending` и применяется при
            # создании state (ниже по функции).
            #
            # Именно в НАЧАЛЕ хода, а не в конце прошлого: стоп по остывшему
            # прогону (ход кончился, ничего не идёт) обязан пропасть, иначе он
            # молча погасил бы следующий запрос человека.
            self._state = None
            self._accounting_closed = False
        settings = settings_of(self._ctx)
        logger = getattr(self._ctx, "logger", None)
        effective_verbose = (
            verbose if verbose is not None else settings.agent.verbose
        )
        skills_list = await self._alist_skills()
        backend_api_mode = str(settings.backend.api_mode).strip().lower()
        opened = await self._aopen_thread_session(
            thread_id,
            start_new_thread=start_new_thread,
            system=system,
            backend_api_mode=backend_api_mode,
            logger=logger,
        )
        self._thread_session = opened[0]
        request = _RunRequest(
            messages=messages,
            thread_id=thread_id,
            verbose=effective_verbose,
            skills_list=skills_list,
            backend_api_mode=backend_api_mode,
            logger=logger,
            discard_writes_after_stop=discard_writes_after_stop,
            message_ids=message_ids,
            display_contents=display_contents,
            skills_loaded=skills_loaded,
            attachments=attachments,
            account_run=account_run,
        )
        try:
            return await self._arun_on_thread(opened, request)
        except Exception:
            # Отказ хода — не падение процесса: восстанавливать нечего.
            await _amark_turn(
                self._ctx, opened[0], running=False, logger=logger
            )
            await _arelease_lease(opened[0], logger)
            raise
        except BaseException:
            # Тред взят в начале хода; ход, не дошедший до учёта, не держит
            # его до срока аренды (повторное отпускание безвредно). Снятая
            # задача (остановка сервера) оставляет отметку хода: его
            # продолжит восстановление после перезапуска.
            await _arelease_lease(opened[0], logger)
            raise

    async def _arun_on_thread(
        self, opened: tuple, request: _RunRequest
    ) -> Any:
        """Ход на взятом треде: подготовка состояния, цикл агента, учёт."""
        thread_session, thread_shared, previous_response_id, system_id = opened
        messages = request.messages
        thread_id = request.thread_id
        effective_verbose = request.verbose
        skills_list = request.skills_list
        backend_api_mode = request.backend_api_mode
        logger = request.logger
        # Turn identity: свежий id на каждый run(), независимо от треда — без
        # него нечем будет пометить записи журнала правок текущего хода.
        bind_turn_identity(self._ctx, mint_turn_id())
        is_cli_mode = system_id == "cli"

        # Снять протухшие runtime-индикаторы одним атомарным store batch,
        # сохраняя прежний путь у пользовательских хранилищ без capability.
        await areset_run_flags(thread_session)
        await self._amcp_snapshot(thread_session, thread_id, system_id)

        # Начальные значения для координатора; проверка истории перечитывает
        # пороги подготовленного вызова через limits_provider.
        limits_provider = self._history_summary_limits
        initial_limits = limits_provider()
        summary_trigger_chars = initial_limits.chars_during
        summary_trigger_tokens = initial_limits.tokens_during
        turn_end_trigger_chars = initial_limits.chars_after
        turn_end_trigger_tokens = initial_limits.tokens_after

        chat_completions_system_prompt: str | None = None
        special_instructions: str | None = None
        if backend_api_mode == "chat_completions":
            (
                chat_completions_system_prompt,
                special_instructions,
            ) = await self._aload_chat_completions_prompts(
                effective_verbose=effective_verbose,
                logger=logger,
            )

        state, rendered_system_prompt = await self._abuild_run_state(
            effective_verbose=effective_verbose,
            skills_list=skills_list,
            chat_completions_system_prompt=chat_completions_system_prompt,
            special_instructions=special_instructions,
            thread_id=thread_id,
            thread_shared=thread_shared,
            thread_session=thread_session,
            backend_api_mode=backend_api_mode,
            logger=logger,
        )
        self._bind_state(
            state, discard_writes_after_stop=request.discard_writes_after_stop
        )

        # Permission memory: store оборачивает state.shared.confirm_rules ПО ССЫЛКЕ —
        # session-правила пишутся прямо в shared и сохраняются обычной персистентностью.
        self._ctx.permission_store = PermissionStore(
            session_rules=state.shared.confirm_rules, shared=state.shared
        )
        _extend_with_thread_history(state, thread_session, backend_api_mode)

        execution_patch = port(self._ctx, "execution_patch")
        deferred_startup_compaction = (
            execution_patch is None
            or execution_patch("startup-compaction-call-kit-v1")
        )
        summary = RunSummaryCoordinator(
            ctx=self._ctx,
            state=state,
            thread_session=thread_session,
            summary_trigger_chars=summary_trigger_chars,
            turn_end_trigger_chars=turn_end_trigger_chars,
            summary_trigger_tokens=summary_trigger_tokens,
            turn_end_trigger_tokens=turn_end_trigger_tokens,
            is_cli_mode=is_cli_mode,
            pre_backend_trigger_reason="pre_backend",
            logger=logger,
            limits_provider=limits_provider,
            deferred_startup_compaction=deferred_startup_compaction,
            startup_progress_guard=(
                deferred_startup_compaction
                and (
                    execution_patch is None
                    or execution_patch("startup-compaction-progress-v1")
                )
            ),
        )
        self._ctx.on_summary_applied = summary.on_summary_applied
        self._ctx.is_cli_mode = is_cli_mode

        # Старые истории сохраняют порядок команд. Новые исполнения проверяют
        # стартовую историю в цикле после выбора первого комплекта модели.
        if not deferred_startup_compaction:
            await _acall_blocking_hook(
                summary.apre_run_compact_if_needed, trigger_reason="pre_run"
            )

        inbox_tail = await self._aread_inbox_tail(thread_session, state)
        if not messages and not inbox_tail and not state.stop_requested:
            raise InboxDrained(
                "Список пользовательских сообщений не должен быть пустым."
            )
        user_bodies = [str(x) for x in messages]
        # Копия вложений в тред — здесь и при приёме очереди, и только после
        # сброса треда и привязки его адреса к контексту выше: копия, сделанная
        # до сброса, пропала бы вместе с тредом.
        commit_attachment = functools.partial(commit_to_run_thread, self._ctx)
        # id можно задать снаружи (TUI пробрасывает свой, чтобы потом точечно
        # откатить осиротевшую реплику при быстрой отмене); иначе генерим.
        new_user_messages = self._build_user_messages(
            user_bodies,
            message_ids=request.message_ids,
            display_contents=request.display_contents,
            skills_loaded=request.skills_loaded,
            gen_id=generate_message_id_func,
            attachments=request.attachments,
            commit=commit_attachment,
        )
        new_user_messages.extend(inbox_tail)
        self._seed_current_user_intent(state, new_user_messages)
        self._apply_plan_changes(state, new_user_messages)
        self._lift_judge_pause(state, new_user_messages)
        # «План убран» — до сообщений человека: сначала контекст, потом
        # запрос. О режиме цикл скажет перед первым запросом к модели.
        for text in reversed(self._plan_notices(state)):
            new_user_messages.insert(
                0,
                {
                    "role": "user",
                    "content": wrap_technical_user_content(text),
                    "id": generate_message_id_func("m"),
                },
            )

        messages, persist_messages_callback, get_full_history_messages = (
            self._persistence_for_run(
                thread_session=thread_session,
                state=state,
                backend_api_mode=backend_api_mode,
                rendered_system_prompt=rendered_system_prompt,
                previous_response_id=previous_response_id,
                new_user_messages=new_user_messages,
                summary_trigger_chars=summary_trigger_chars,
                summary_trigger_tokens=summary_trigger_tokens,
            )
        )

        # Снимок usage до прогона: для resume треда state.usage гидрирован кумулятивом,
        # отчёт же показывает токены именно этого прогона (дельту). Тот же baseline
        # кладём на state — по нему TUI-футер считает live-«запрос» текущего хода.
        run_start_usage = UsageAccount.from_meta(state.usage.to_meta())
        state.run_start_usage_meta = run_start_usage.to_meta()
        # Входим во владение MCP-сессиями этого контекста. Всё, что поднимут
        # тулы внутри прогона, ляжет под этот токен — и на выходе мы закроем
        # ровно своё, не тронув сессии параллельных агентов на том же loop.
        _owner_reset = set_owner(
            getattr(self._ctx, "mcp_session_owner", "") or None
        )
        loop_finished = False
        teardown_cancel: BaseException | None = None
        keeper = _start_lease_keeper(thread_session, on_lost=self.stop_run)
        poller = self._start_inbox_poller(thread_session)
        # Накопленное ДО старта уходит в ПЕРВЫЙ батч, после собственных
        # сообщений прогона: клиент, которому ответили `queued`, должен попасть
        # к модели в том же запросе, а не ждать следующего хода.
        queued_before_loop = self._take_pending_messages()
        if queued_before_loop:
            messages = [*messages, *queued_before_loop]
        try:
            response = await arun_agent(
                messages,
                state,
                self._ctx,
                get_full_history_messages=get_full_history_messages,
                persist_messages_callback=persist_messages_callback,
                # Корутинные двойники, а не синхронные имена — и это несущая
                # деталь, а не стиль. _acall_blocking_hook корутинную функцию
                # ждёт НАТИВНО, а обычную уводит в asyncio.to_thread. Передать
                # сюда синхронное имя значит вернуть поток из пула, занятый на
                # всё время суммаризации, ради которого всё и переводилось.
                pre_backend_compaction_hook=summary.apre_backend_compaction_hook,
                turn_end_compaction_hook=summary.aturn_end_compact_if_needed,
                force_compact_hook=summary.aforce_compact_once,
                get_pending_messages=self._take_pending_messages,
                check_stop=self._is_stop_requested,
                session_meta=(
                    thread_session.notice
                    if thread_session is not None
                    else None
                ),
                mark_finalizing=(
                    lambda active: aset_tui_finalizing(thread_session, active)
                ),
                mark_finish_gate_checking=(
                    lambda active: aset_tui_finish_gate_checking(
                        thread_session, active
                    )
                ),
            )
            loop_finished = True
            self.last_terminal_outcome = getattr(
                state, "terminal_outcome", None
            )
        finally:
            await _stop_lease_keeper(poller)
            await _stop_lease_keeper(keeper)
            state_ops.leave(state)
            state.turn_level = None
            teardown_cancel = await self._ateardown_run(_owner_reset)
            if not loop_finished:
                # Ход упал — учёт не закроется, аренду не держать до срока.
                await _arelease_lease(thread_session, logger)

        if teardown_cancel is not None:
            # Незавершённый цикл сюда не попадает: его исключение выходит из
            # try/finally само, и чужой итог мы не записываем.
            if loop_finished:
                await self._apersist_finished_loop_during_cancel()
            raise teardown_cancel

        # Учёт — одна точка. Кто продолжит прогон после возврата, закроет её сам.
        # Отмена уборки сюда не доходит: завершённый цикл уже записан выше.
        if request.account_run:
            await self.aclose_run_accounting()

        self._report_log_path(logger)
        return response

    def set_permission_mode(self, mode: str) -> None:
        """Сменить режим разрешений сессии (хоткей фронтенда, флаг запуска).

        Резолвер читает режим на каждом решении, поэтому следующий вызов тула
        пойдёт уже по новому режиму — и посреди прогона. Выбор запоминается
        ВСЕГДА, а не только при живом state: между прогонами ``self._state``
        принадлежит завершённому прогону, запись в него никуда не поедет, а
        следующий прогон соберёт shared заново из сессии.
        """
        resolved = coerce_mode(mode)
        if resolved is None:
            raise ValueError(f"unknown permission mode: {mode!r}")
        with self._lock:
            self._permission_mode_choice = resolved.value
            if self._state is not None:
                self._state.shared.permission_mode = resolved.value

    def clear_permission_mode(self) -> None:
        """Снять явный выбор режима: следующий прогон возьмёт режим из
        сессии, а нет его там — из конфига.

        Живое состояние не трогается: между прогонами оно принадлежит
        завершённому прогону, а следующий соберёт shared из сессии заново.
        До следующего прогона ``permission_mode()`` может вернуть выбор
        прошлого прогона.
        """
        with self._lock:
            self._permission_mode_choice = None

    def permission_mode(self) -> Optional[str]:
        """Режим, выбранный явно или сохранённый в сессии; None — действует конфиг."""
        with self._lock:
            if self._permission_mode_choice is not None:
                return self._permission_mode_choice
            if self._state is not None:
                return getattr(self._state.shared, "permission_mode", None)
            return None

    async def _aread_inbox_tail(self, thread_session, state) -> list[dict]:
        """Недоставленный хвост входящего потока — в стартовые сообщения хода
        (отметку доставки унесёт первая запись шага)."""
        read = _inbox_reader(self._ctx, thread_session)
        if read is None:
            return []
        delivered = int(state.inbox_seq or 0)
        entries = await read(after=delivered)
        control = await thread_session.aread_control(after=0)
        self._control_seen = control[-1].seq if control else 0
        cut = _cancel_boundary(control, first=delivered + 1)
        if cut is not None:
            # Отмена пришла, пока ход собирался: сообщения до её границы
            # сняты (в историю не идут), ход останавливается до первого
            # запроса к модели; отметку доставки унесёт запись остановки.
            state.inbox_seq = cut
            self._inbox_seen = cut
            state.stop_requested = True
            self.stop_run()
            return []
        if entries:
            state.inbox_seq = entries[-1].seq
        self._inbox_seen = int(state.inbox_seq or 0)
        return [_inbox_message(entry) for entry in entries]

    def _start_inbox_poller(self, thread_session) -> asyncio.Task | None:
        """Фоновый опрос входящего потока, пока идёт цикл: новое — в очередь
        хода с меткой ``seq``; цикл заберёт его на следующем шаге."""
        read = _inbox_reader(self._ctx, thread_session)
        if read is None:
            return None
        interval = settings_of(self._ctx).thread_store.inbox_poll_seconds
        read_control = thread_session.aread_control

        async def poll() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    entries = await read(after=self._inbox_seen)
                    control = await read_control(after=self._control_seen)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "thread inbox poll failed", exc_info=True
                    )
                    continue
                self._enqueue_inbox(entries)
                self._apply_control(control)

        return asyncio.create_task(poll())

    def _apply_control(self, entries: list) -> None:
        """Отмена из управляющего потока — остановка хода на следующем шаге."""
        if not entries:
            return
        self._control_seen = entries[-1].seq
        if any(entry.data.get("type") == "cancel" for entry in entries):
            self.stop_run()

    def _enqueue_inbox(self, entries: list) -> None:
        """Записи потока — прямо в очередь хода (не субагенту: отметку
        доставки ведёт ход)."""
        with self._lock:
            for entry in entries:
                if entry.seq <= self._inbox_seen:
                    continue
                self._pending_messages.append(
                    {**_inbox_message(entry), _INBOX_MARK: entry.seq}
                )
                self._inbox_seen = entry.seq

    async def ainbox_left(self) -> bool:
        """После хода: во входящем потоке треда есть недоставленное, а ход
        кончился не остановкой — значит нужен ещё ход (рукопожатие ``arun``
        и реестр сервера, закрывший прогон)."""
        thread_session = self._thread_session
        state = self.get_state()
        if state is None or state.stop_requested:
            return False
        if _inbox_reader(self._ctx, thread_session) is None:
            return False
        entries = await thread_session.aread_inbox(
            after=int(state.inbox_seq or 0), limit=1
        )
        return bool(entries)

    def add_message(self, message: dict) -> None:
        """Потокобезопасно добавляет сообщение в очередь активного скоупа.

        Executing subagent → child pending; иначе → parent pending.
        """
        # Адресат берётся из СВОЕГО контекста: сообщение этого прогона не может
        # уехать субагенту чужой сессии — до её контекста отсюда нет ссылки.
        if try_enqueue_user_message(self._ctx, message):
            return
        with self._lock:
            self._pending_messages.append(message)

    def is_child_input_active(self) -> bool:
        """Работает ли сейчас субагент, которому уйдёт ввод пользователя.

        Фронтенду нужно спрашивать СВОЕГО исполнителя, а не модуль ядра:
        модульная функция отвечала за весь процесс и в многосессионном сервере
        говорила о чужом прогоне.
        """
        return is_child_input_active(self._ctx)

    def active_parent_call_id(self) -> str | None:
        """``call_id`` вызова субагента, который сейчас принимает ввод."""
        return active_parent_call_id(self._ctx)

    async def aflush_thread(self) -> None:
        """Записать отложенные шаги хода (persist: turn); иначе — ничего."""
        flush = getattr(self._thread_session, "aflush_deferred", None)
        if flush is not None:
            await flush()

    def get_state(self) -> State | None:
        """
        Возвращает текущий state (под замком).
        Не мутировать снаружи; изменения — только через add_message() или под замком executor'а.
        """
        with self._lock:
            return self._state

    def get_model_router(self):
        """Router, зафиксированный при сборке ЭТОГО executor'а (``RunContext.model_router``,
        см. ``src/run_context.py``) — снимок ``models``/``slots``/``assign`` неизменен всю
        его жизнь (пересборка — только следующим ``get_runner()`` после ``invalidate_runner``).

        Футеру TUI нужен именно этот Router для резолва подготовленного вызова по
        его уровню: живой ``display_router()`` отражает /profiles мгновенно и подменил бы
        факт моделью уже переключившегося профиля (резолв — перед каждым вызовом по
        живому уровню, ``processing_responses.resolve_main_for_call``). ``None`` — у
        ``ctx`` нет ``model_router`` (тестовые двойники).
        """
        return getattr(self._ctx, "model_router", None)

    def _history_summary_limits(self):
        """Пороги сжатия модели текущего вызова (не снимок старта run):
        по уровню, выбранному перед подготовкой запроса, — тому же, по которому идут
        запросы. До сборки state уровня ещё нет — обычный резолв `main`."""
        return resolve_summary_limits_at_level(
            self._ctx,
            self.get_model_router(),
            getattr(self._state, "_call_level", None),
        )

    def summary_limits(self):
        """Пороги сжатия, по которым работает ЭТОТ executor: его снимок конфига,
        его Router, уровень текущего вызова. Читает индикатор контекста TUI — новый
        выбор пользователя не меняет уже подготовленный вызов."""
        return self._history_summary_limits()

    def usage_meta_snapshot(self) -> Optional[tuple[dict, Optional[dict]]]:
        """Снимок usage для TUI-футера: ``(session_meta, request_meta | None)``.

        ``session_meta`` — кумулятив треда (``state.usage``), тикает вживую.
        ``request_meta`` — «запрос»:
        - ход идёт (``last_run_usage`` ещё None) → **live-дельта** ``usage − baseline``,
          где baseline (``run_start_usage_meta``) снят на старте хода: число
          сбрасывается с новым вопросом и растёт по мере работы агента;
        - ход завершён (``last_run_usage`` выставлен) → итог этого хода;
        - прогонов ещё не было → None.
        Всё через ``to_meta()`` под замком — против гонки «dictionary changed size»
        с живым ``UsageAccount.add()`` в agent-потоке. None, если state не создан.
        """
        with self._lock:
            state = self._state
            if state is None:
                return None
            usage = getattr(state, "usage", None)
            session_meta = usage.to_meta() if usage is not None else {}
            last_run = getattr(state, "last_run_usage", None)
            if last_run is not None:
                request_meta: Optional[dict] = last_run.to_meta()
            else:
                baseline_meta = getattr(state, "run_start_usage_meta", None)
                if baseline_meta is not None and usage is not None:
                    baseline = UsageAccount.from_meta(baseline_meta)
                    request_meta = usage.delta_from(baseline).to_meta()
                else:
                    request_meta = None
            return (session_meta, request_meta)

    @staticmethod
    def _build_user_messages(
        user_bodies,
        *,
        message_ids,
        display_contents,
        skills_loaded,
        gen_id,
        attachments=None,
        commit=None,
    ):
        """Собрать user-сообщения с опциональными display_content/skill_loaded/attachments.
        display_content ставится только при отличии от content; skill_loaded — если задан;
        attachments — если у сообщения есть вложения: каждое проходит через ``commit``
        (копия в тред прогона), ``commit=None`` — вложения как есть, в кармане."""
        out = []
        for i, body in enumerate(user_bodies):
            body = str(body)
            msg = {
                "role": "user",
                "content": body,
                "id": (
                    message_ids[i]
                    if message_ids and i < len(message_ids) and message_ids[i]
                    else gen_id("m")
                ),
            }
            disp = (
                display_contents[i]
                if display_contents and i < len(display_contents)
                else None
            )
            if disp is not None and disp != body:
                msg["display_content"] = disp
            sk = (
                skills_loaded[i]
                if skills_loaded and i < len(skills_loaded)
                else None
            )
            if sk:
                msg["skill_loaded"] = sk
            _put_attachments(msg, attachments, i, commit)
            out.append(msg)
        return out

    def _commit_queued(self, messages: list[dict]) -> list[dict]:
        """Сообщения очереди с вложениями, переложенными в тред прогона: сообщение
        получает тред, когда его принимает агент, — здесь и копируем его картинки."""
        return [with_committed_attachments(m, self._ctx) for m in messages]

    def stop_run(self) -> None:
        """Выставляет флаг остановки; агент завершит текущую итерацию и выйдет.

        Если state ещё не создан (run() в setup-фазе, напр. сборка в worker-
        потоке) — запоминаем в _stop_pending, run() применит при создании state."""
        with self._lock:
            if self._state is not None:
                self._state.stop_requested = True
            else:
                self._stop_pending = True

    async def acompact_thread(
        self, thread_id: str, system: str | None = None
    ) -> bool:
        """Форс-сжатие треда (ручной /compact) — делегат в manual_compaction
        (оркестрация суммаризации, вне run-loop)."""
        from gc_core.manual_compaction import (
            acompact_thread as _acompact_thread,
        )

        return await _acompact_thread(
            ctx=self._ctx,
            _lock=self._lock,
            self_state=self._state,
            thread_id=thread_id,
            system=system,
        )
