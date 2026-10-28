import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set

from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.execution.message_keys import make_message_key
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.persistence.session_state import (
    extra_for_write,
    notice_state,
    session_after_compaction,
)
from gc_core.persistence.turn_outcome import (
    TurnOutcome,
    last_turn_of,
    turn_entry,
)
from gc_core.settings import settings_of
from gc_core.thread_paths import (
    resolve_threads_dir,
    sanitize_system_id,
    validate_thread_id,
)
from gc_core.threads import file_layout
from gc_core.threads import runtime_rules as rt_rules
from gc_core.threads.history import messages_and_keys
from gc_core.threads.instance import session_owner
from gc_core.threads.models import (
    CONTROL,
    INBOX,
    TURNS,
    Compaction,
    RuntimeStatus,
    SessionState,
    SideChange,
    ThreadInfo,
    ThreadKey,
    ThreadRecord,
    Usage,
)
from gc_core.threads.session_writer import RetryPolicy, SessionWriter
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)


def ensure_message_id(
    message: Dict[str, Any], prefix: str = "m"
) -> Dict[str, Any]:
    if not isinstance(message, dict):
        return message
    msg_id = message.get("id")
    if isinstance(msg_id, str) and msg_id:
        return message
    patched = dict(message)
    patched["id"] = generate_message_id_func(prefix)
    return patched


_HISTORY = "messages"
_SUBAGENTS = "subagent_events"


# Раскладка файлов — общая с FileThreadStore v2 (gc_core.threads.file_layout).
_thread_paths = file_layout.thread_paths


subagent_trace_path = file_layout.subagent_trace_path
plan_file_path = file_layout.plan_file_path


edits_journal_path = file_layout.edits_journal_path


def _batches_by_bytes(messages, limit):
    """Сообщения частями не больше ``limit`` байт JSON (хотя бы по одному);
    без потолка или без сообщений — одной частью (запись меты тоже нужна).

    Размер — в ASCII-JSON (не-ASCII символ — ``\\uXXXX``, 6 байт): так пишут
    durable-рантаймы с лимитом записи (JSON-конвертер Temporal); оценка с
    запасом для любого другого хранилища."""
    if not limit or not messages:
        return [messages]

    batches, batch, size = [], [], 0
    for message in messages:
        length = len(json.dumps(message, ensure_ascii=True, default=str))
        if batch and size + length > limit:
            batches.append(batch)
            batch, size = [], 0
        batch.append(message)
        size += length
    if batch:
        batches.append(batch)
    return batches


#: Имя чтения треда в логах повторов записи сессии.
_READ = "thread read"


class ThreadState(NamedTuple):
    """Сохранённая история треда: сообщения, их ключи, последний номер."""

    messages: List[Dict[str, Any]]
    known_keys: Set[str]
    max_seq: int


def _api_mode(info: ThreadInfo) -> str:
    raw = info.api_mode if info.api_mode is not None else "responses"
    mode = str(raw).strip().lower()
    return mode if mode in {"responses", "chat_completions"} else "responses"


def _own_info(info: ThreadInfo, api_mode: str) -> ThreadInfo:
    """Карточка для записи хода: только поля сессии. ``title`` ставит
    фронтенд, а штампы и счётчик — хранилище: их запись хода не задаёт."""
    values: Dict[str, Any] = {"api_mode": api_mode}
    if "workspace" in info.model_fields_set:
        values["workspace"] = info.workspace
    return ThreadInfo(**values)


@dataclass
class SessionParts:
    """Снимок частей записи треда, с которым работает сессия."""

    state: SessionState = field(default_factory=SessionState)
    usage: Usage = field(default_factory=Usage)
    info: ThreadInfo = field(default_factory=ThreadInfo)
    runtime: RuntimeStatus = field(default_factory=RuntimeStatus)
    extra: Dict[str, Any] = field(default_factory=dict)
    #: Секции фронтендов на момент чтения: сессия их не пишет.
    frontend: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, record: Optional[ThreadRecord]) -> "SessionParts":
        if record is None:
            return cls()
        return cls(
            state=record.state,
            usage=record.usage,
            info=record.info,
            runtime=record.runtime,
            extra=dict(record.extra),
            frontend=dict(record.frontend),
        )


class ThreadSession:
    """Сессия одного треда: снимок частей записи v2, своя аренда и
    ревизия. Шаг хода — одна запись (``commit``) без чтения перед ней,
    живой статус хода — запись сбоку (``append_side``) из памяти."""

    def __init__(
        self,
        *,
        threads_dir: str,
        system_id: str,
        store: Any = None,
        thread_id: str,
        history_messages: List[Dict[str, Any]],
        known_message_keys: Set[str],
        persisted_messages_count: int,
        previous_response_id: Optional[str],
        backend_api_mode: str,
        parts: Optional[SessionParts] = None,
        writer: Optional[SessionWriter] = None,
        tags: Sequence[str] = (),
        strip_think: bool = True,
    ):
        self.threads_dir = threads_dir
        # Хранилище сессии. None — файловое по умолчанию: сессию собирают и
        # напрямую (тесты, ручные сценарии), и ломать их не за что.
        if store is None:
            from gc_core.threads.file_store import FileThreadStore

            store = FileThreadStore(threads_dir)
        self.store = store
        self.system_id = sanitize_system_id(system_id)
        self.thread_id = thread_id
        self.key = ThreadKey(system=self.system_id, thread=thread_id)
        self.history_messages = history_messages
        self.known_message_keys = known_message_keys
        self.persisted_messages_count = persisted_messages_count
        self.previous_response_id = previous_response_id
        self.backend_api_mode = backend_api_mode
        parts = parts or SessionParts()
        self.state = parts.state
        self.usage = parts.usage
        self.info = parts.info
        self.runtime = parts.runtime
        self.extra: Dict[str, Any] = dict(parts.extra)
        #: Секции фронтендов на момент чтения: сессия их не пишет.
        self.frontend: Dict[str, Any] = dict(parts.frontend)
        #: Счётчики политики напоминаний — её изменяемый словарь.
        self.notice: Dict[str, Any] = notice_state(self.state, self.extra)
        self.writer = writer or SessionWriter(
            store, self.key, owner=session_owner()
        )
        self._proj_tags = tuple(tags or ())
        self._proj_strip_think = bool(strip_think)
        self.runtime_messages: List[Dict[str, Any]] = []
        # Снимок known_message_keys до записи последнего step_messages (для compaction user_ask).
        self.known_keys_snapshot_before_last_batch: Optional[
            frozenset[str]
        ] = None
        # persist: turn — шаги хода копятся здесь до точки сброса.
        self._defer_persist = False
        self._deferred_messages: List[Dict[str, Any]] = []
        self._deferred_response_id: Optional[str] = None
        self._deferred_parts: Optional[tuple[SessionState, Usage]] = None
        self._deferred_pending = False
        #: Счётчик отложенных батчей: сброс не снимает флаг с пришедших во время записи.
        self._deferred_mark = 0

    @classmethod
    async def afrom_config(
        cls,
        *,
        settings: Any,
        system_id: str,
        thread_id: str,
        store: Any = None,
        claim: bool = False,
    ) -> "ThreadSession":
        """Загрузить тред. ``store`` не передан — файловое хранилище.

        ``claim`` — взять тред для хода до чтения (аренда сессии): чужая
        живая аренда — ``ThreadBusy``. Чтение упало — аренда отпускается.
        Политику аренды (владелец, срок, уже взятая аренда) задаёт само
        хранилище возможностью ``session_lease_policy`` (workflow
        Temporal); иначе — своя сессия со сроком ``LEASE_TTL``.
        """
        sys_norm = sanitize_system_id(system_id)
        normalized_thread_id = validate_thread_id(thread_id)
        store, threads_dir = cls._resolve_store(settings, store)
        cls._check_file_owner(
            store, threads_dir, sys_norm, normalized_thread_id
        )
        key = ThreadKey(system=sys_norm, thread=normalized_thread_id)
        writer = _writer_for(store, key, settings)
        if claim:
            await writer.aclaim()
        try:
            record, state = await _aread_thread(store, key, writer)
        except BaseException:
            if claim:
                await writer.arelease()
            raise
        session = cls._build(
            settings=settings,
            threads_dir=threads_dir,
            store=store,
            key=key,
            record=record,
            state=state,
            writer=writer,
        )
        session._defer_persist = (
            settings_of(settings).thread_store.persist == "turn"
        )
        return session

    @staticmethod
    def _check_file_owner(store, threads_dir, system_id, thread_id) -> None:
        """Обычный файловый писатель не пишет в тред, занятый durable-владельцем.

        Без каталога тредов (своё хранилище, прокси Temporal в коде workflow)
        проверять нечего — и импортировать файловые хранилища тоже: их журнал
        создаёт блокировки, запрещённые в песочнице workflow."""
        if not threads_dir:
            return
        from gc_core.threads.file_store import FileThreadStore

        if type(store) is not FileThreadStore:
            return
        from gc_core.thread_store_journal import durable_owner_id
        from gc_core.threads.errors import OwnershipError

        if durable_owner_id(threads_dir, system_id, thread_id):
            raise OwnershipError(
                "Thread is owned by a durable executor (Temporal); "
                "local writes are forbidden"
            )

    @staticmethod
    def _resolve_store(settings: Any, store: Any) -> "tuple[Any, str]":
        """Хранилище и каталог."""
        if store is not None:
            return store, getattr(store, "threads_dir", "")
        # Путь к каталогу нужен ТОЛЬКО файловому хранилищу. Резолвить его
        # безусловно значило бы требовать `threads_data_path` от того, кто
        # держит историю в базе и файлов не заводит вовсе.
        from gc_core.threads.file_store import FileThreadStore

        threads_dir = resolve_threads_dir(settings)
        return FileThreadStore(threads_dir), threads_dir

    @classmethod
    def _build(
        cls,
        *,
        settings: Any,
        threads_dir: str,
        store: Any,
        key: ThreadKey,
        record: Optional[ThreadRecord],
        state: ThreadState,
        writer: SessionWriter,
    ) -> "ThreadSession":
        """Собрать сессию из прочитанного."""
        parts = SessionParts.of(record)
        previous = parts.state.previous_response_id
        if not isinstance(previous, str) or not previous:
            previous = None
        return cls(
            threads_dir=threads_dir,
            store=store,
            system_id=key.system,
            thread_id=key.thread,
            history_messages=state.messages,
            known_message_keys=state.known_keys,
            persisted_messages_count=state.max_seq,
            previous_response_id=previous,
            backend_api_mode=_api_mode(parts.info),
            parts=parts,
            writer=writer,
            tags=projection_tags_from_config(settings),
            strip_think=True,
        )

    def set_runtime_messages(
        self, runtime_messages: List[Dict[str, Any]]
    ) -> None:
        self.runtime_messages = runtime_messages

    def get_full_history_messages(self) -> List[Dict[str, Any]]:
        combined: List[Dict[str, Any]] = []
        seen_keys: Set[str] = set()
        for item in [
            *self.history_messages,
            *self._deferred_messages,
            *self.runtime_messages,
        ]:
            key = make_message_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            combined.append(item)
        return combined

    # --- шаг хода -------------------------------------------------------------

    async def apersist_batch(
        self,
        step_messages: List[Dict[str, Any]],
        *,
        previous_response_id: Optional[str],
        session: Optional[SessionState] = None,
        usage: Optional[Usage] = None,
    ) -> None:
        """Записать шаг: новые сообщения и состояние треда (``session``,
        ``usage`` — от исполнителя; не переданы — прежние). Самый горячий
        шов: раз за итерацию цикла и раз за тул-колл."""
        parts = (
            session if session is not None else self.state,
            usage if usage is not None else self.usage,
        )
        if self._defer_persist:
            # Шаг остаётся в памяти до точки сброса (aflush_deferred): его
            # защищает история durable-исполнителя, а не хранилище.
            from copy import deepcopy

            self._deferred_messages.extend(deepcopy(list(step_messages)))
            self._deferred_response_id = previous_response_id
            self._deferred_parts = parts
            self._deferred_pending = True
            self._deferred_mark += 1
            return
        await self._apersist_now(
            step_messages,
            previous_response_id=previous_response_id,
            parts=parts,
        )

    async def aflush_deferred(self) -> None:
        """Записать накопленные шаги (persist: turn) — частями по потолку
        записи хранилища (``max_operation_bytes``), по порядку."""
        if not self._deferred_pending:
            return
        # Буфер очищается только после записи: при сбое шаги остаются для
        # повторного сброса (дубли отсекают ключи сообщений).
        messages = list(self._deferred_messages)
        mark = self._deferred_mark
        parts = self._deferred_parts or (self.state, self.usage)
        limit = getattr(self.store, "max_operation_bytes", None)
        for batch in _batches_by_bytes(messages, limit):
            await self._apersist_now(
                batch,
                previous_response_id=self._deferred_response_id,
                parts=parts,
            )
            del self._deferred_messages[: len(batch)]
        if self._deferred_mark == mark:
            self._deferred_pending = False

    async def _apersist_now(
        self,
        step_messages: List[Dict[str, Any]],
        *,
        previous_response_id: Optional[str],
        parts: tuple[SessionState, Usage],
    ) -> None:
        """История и состояние шага — одной записью. Сессия меняется только
        после записи: упавшая запись не оставляет её впереди хранилища."""
        known_before = set(self.known_message_keys)
        fresh, seen = [], set(known_before)
        for message in step_messages:
            key = make_message_key(message)
            if key not in seen:
                fresh.append(message)
                seen.add(key)
        runtime = self.runtime
        if fresh:
            runtime = rt_rules.bumped_live_volume(
                runtime,
                fresh,
                tags=self._proj_tags,
                strip_think=self._proj_strip_think,
            )
            runtime = rt_rules.bumped_image_seq(runtime, fresh)
        state, usage = parts
        if state.previous_response_id != previous_response_id or (
            "previous_response_id" not in state.model_fields_set
        ):
            state = state.model_copy(
                update={"previous_response_id": previous_response_id}
            )
        await self.writer.acommit(
            append_messages=fresh,
            set_state=state,
            set_usage=usage,
            set_info=_own_info(self.info, self.backend_api_mode),
            set_runtime=runtime,
            set_extra=extra_for_write(self.extra),
        )
        self.known_keys_snapshot_before_last_batch = frozenset(known_before)
        self.history_messages.extend(fresh)
        self.known_message_keys.update(seen)
        self.persisted_messages_count += len(fresh)
        self.previous_response_id = previous_response_id
        self.state, self.usage, self.runtime = state, usage, runtime
        self.extra = extra_for_write(self.extra)

    # --- сжатие ---------------------------------------------------------------

    def _compact_summary_message(
        self, summary_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Сообщение-сводка, которым замещается история."""
        summary_text = str(
            summary_payload.get("summary_message_content") or ""
        ).strip()
        return {
            "role": "user",
            "content": wrap_technical_user_content(
                f"Summarized context from previous dialogue.\n{summary_text}",
                technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
            ),
            "id": generate_message_id_func("m_sum"),
        }

    async def aarchive_and_compact_summary_payload(
        self,
        summary_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Отложить историю в архив и заместить её сводкой — одной записью
        вместе с состоянием после сжатия."""
        from gc_core.execution.environment import utc_now

        # Компакция переписывает сохранённый тред: несохранённые шаги — сначала.
        await self.aflush_deferred()
        summary_message = self._compact_summary_message(summary_payload)
        compact = [summary_message]
        state = session_after_compaction(
            self.state, summary_payload, now_iso=utc_now().isoformat()
        )
        runtime = rt_rules.with_live_volume(
            self.runtime,
            compact,
            tags=self._proj_tags,
            strip_think=self._proj_strip_think,
        )
        receipt = await self.writer.acommit(
            replace_history=Compaction(messages=compact),
            set_state=state,
            set_usage=self.usage,
            set_info=_own_info(self.info, self.backend_api_mode),
            set_runtime=runtime,
            set_extra=extra_for_write(self.extra),
        )
        # Сжатие develop всегда оставляет секцию ``tui`` (пустую, если её не
        # было): слияние с пустым — без изменения у той, что есть.
        await self.writer.aside(patch_frontend=("tui", {}))
        self.history_messages = compact
        self.runtime_messages = []
        self.known_message_keys = {make_message_key(m) for m in compact}
        self.persisted_messages_count = len(compact)
        self.previous_response_id = None
        self.state, self.runtime = state, runtime
        self.extra = extra_for_write(self.extra)
        data = meta = None
        if self.threads_dir and receipt.archive_id:
            data, meta = file_layout.archive_paths(
                self.threads_dir,
                self.system_id,
                self.thread_id,
                receipt.archive_id,
            )
        return {
            "archive_suffix": receipt.archive_id,
            "archive_data_path": data,
            "archive_meta_path": meta,
            "new_messages_count": len(compact),
            "newly_summarized_message_id": summary_message.get("id"),
        }

    # --- живой статус хода ----------------------------------------------------

    async def _aset_runtime(self, runtime: RuntimeStatus) -> None:
        # Статус не изменился — не пишем: сброс флагов на старте каждого
        # прогона без залипших флагов обходится без записи.
        if runtime == self.runtime:
            return
        if self.writer.gave_up_lease:
            # Тред отпущен или потерян: его runtime ведёт другой ход, а
            # запись сбоку хранилище по сроку не фенсит — поздняя отметка
            # затёрла бы его.
            return
        await self.writer.aside(set_runtime=runtime)
        self.runtime = runtime

    async def amark_turn(self, *, running: bool) -> None:
        """Отметка хода для восстановления после падения."""
        await self._aset_runtime(
            rt_rules.with_turn(self.runtime, running=running)
        )

    async def aset_flag(self, flag: str, *, active: bool) -> None:
        """Индикатор TUI: ``summarizing``, ``finalizing``,
        ``finish_gate_checking``."""
        await self._aset_runtime(
            rt_rules.with_flag(self.runtime, flag, active=active)
        )

    async def areset_run_flags(self) -> None:
        """Снять залипшие флаги хода (прошлый прогон упал)."""
        await self._aset_runtime(rt_rules.reset_run_flags(self.runtime))

    async def aadd_trace_id(self, trace_id: str) -> None:
        await self._aset_runtime(
            rt_rules.with_trace_id(self.runtime, trace_id)
        )

    async def aset_mcp_tools(self, snapshot: Any) -> None:
        await self._aset_runtime(
            rt_rules.with_mcp_tools(self.runtime, snapshot)
        )

    async def aappend_turn_outcome(
        self, outcome: TurnOutcome, *, discarded: bool
    ) -> bool:
        """Итог хода — запись потока ``turns`` за последним сообщением хода
        и короткий итог в ``runtime.last_turn``, одной записью сбоку по
        аренде хода (любое хранилище, durable — тоже).

        ``discarded`` — записи хода отброшены стопом: отложенные шаги не
        сливаются, запись встаёт за уже записанным. Пустой тред записи не
        получает; отпущенный или потерянный тред — тоже: его runtime ведёт
        другой ход. Отказ по аренде — ``ThreadBusy``. Возвращает, записано
        ли."""
        if not discarded:
            await self.aflush_deferred()
        if not self.history_messages or self.writer.gave_up_lease:
            return False
        after = await self._alast_message_id()
        runtime = rt_rules.with_last_turn(self.runtime, last_turn_of(outcome))
        await self.writer.aside(
            append=[(TURNS, [turn_entry(outcome, after=after)])],
            set_runtime=runtime,
        )
        self.runtime = runtime
        return True

    async def _alast_message_id(self) -> Optional[str]:
        """``id`` последнего записанного сообщения — тот, что видят
        читатели: у сообщения без ``id`` его даёт хранилище на чтении."""
        mid = self.history_messages[-1].get("id")
        if not (isinstance(mid, str) and mid):
            record = await self.writer.retried(
                lambda: self.store.get_thread(self.key), _READ
            )
            high = record.max_seq.get(_HISTORY, 0) if record else 0
            tail = await self.writer.retried(
                lambda: self.store.read_stream(
                    self.key, _HISTORY, after_seq=high - 1, limit=1
                ),
                _READ,
            )
            mid = tail[0].data.get("id") if tail else None
        return mid if isinstance(mid, str) and mid else None

    # --- аренда ---------------------------------------------------------------

    async def arenew_lease(self) -> None:
        await self.writer.arenew()

    async def arelease_lease(self) -> None:
        await self.writer.arelease()

    @property
    def renews_lease(self) -> bool:
        return self.writer.renews

    # --- входящие потоки и трасса субагентов ----------------------------------

    async def aread_inbox(self, *, after: int, limit: Optional[int] = None):
        return await self.writer.retried(
            lambda: self.store.read_stream(
                self.key, INBOX, after_seq=after, limit=limit
            ),
            _READ,
        )

    async def aread_control(self, *, after: int):
        return await self.writer.retried(
            lambda: self.store.read_stream(self.key, CONTROL, after_seq=after),
            _READ,
        )

    async def asave_plan_file(self, plan: Optional[Dict[str, Any]]):
        """Файл плана для человека — возможность файлового хранилища."""
        save = getattr(self.store, "save_plan_file", None)
        if not callable(save):
            return None
        return await save(self.key, plan)


def _writer_for(store: Any, key: ThreadKey, settings: Any) -> SessionWriter:
    """Писатель сессии по политике аренды хранилища (Temporal — свой
    владелец, бессрочно, уже взятая аренда) или своей сессии со сроком."""
    retry = RetryPolicy.from_settings(settings)
    policy = getattr(store, "session_lease_policy", None)
    if callable(policy):
        owner, ttl, lease = policy()
        return SessionWriter(
            store, key, owner=owner, ttl=ttl, retry=retry, lease=lease
        )
    return SessionWriter(store, key, owner=session_owner(), retry=retry)


async def _aread_thread(
    store: Any, key: ThreadKey, writer: SessionWriter
) -> tuple[Optional[ThreadRecord], ThreadState]:
    """Запись треда и история — один раз, при открытии сессии."""
    record = await writer.retried(lambda: store.get_thread(key), _READ)
    entries = await writer.retried(
        lambda: store.read_stream(key, _HISTORY), _READ
    )
    messages, keys = messages_and_keys(store, key, entries)
    if record is not None and (writer.lease is None or writer.revision == 0):
        writer.revision = record.revision
    max_seq = record.max_seq.get(_HISTORY, 0) if record else 0
    return record, ThreadState(messages, set(keys), max_seq)


async def append_subagent_record(
    store: Any,
    key: ThreadKey,
    *,
    writer: Optional[SessionWriter] = None,
    parent_call_id: str,
    child_run_id: str,
    kind: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Запись трассы субагентов (поток ``subagent_events``) — сбоку, по
    аренде сессии, если она есть; возвращает запись в форме develop."""
    data = {
        "parent_call_id": parent_call_id,
        "child_run_id": child_run_id,
        "kind": kind,
        "payload": payload,
    }
    if writer is not None:
        receipt = await writer.aside(append=[(_SUBAGENTS, [data])])
    else:
        # Без сессии — по аренде из политики хранилища (workflow Temporal:
        # тред под бессрочной арендой принимает запись сбоку только по ней).
        policy = getattr(store, "session_lease_policy", None)
        lease = policy()[2] if callable(policy) else None
        receipt = await store.append_side(
            key,
            SideChange(
                op_id=generate_message_id_func("trace"),
                lease=lease,
                append=[(_SUBAGENTS, [data])],
            ),
        )
    seq = receipt.max_seq.get(_SUBAGENTS, 1)
    written = await store.read_stream(
        key, _SUBAGENTS, after_seq=seq - 1, limit=1
    )
    entry = written[0]
    return {"ts": entry.ts, "seq": entry.seq, **entry.data}
