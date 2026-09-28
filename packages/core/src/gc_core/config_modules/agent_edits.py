"""Конфиг секции ``agent_edits:`` — запись правок агента (ядро, все режимы) и их показ в TUI.

До переименования секция называлась ``diff_tui``: старые конфиги переносит
``config_modules.compat``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_SNAPSHOTS_MAX_FILES = 500
DEFAULT_MAX_DIFF_CHARS = 100_000
DEFAULT_HUNK_CONTEXT_LINES = 3
DEFAULT_MAX_DIFF_SOURCE_BYTES = 2_000_000
DEFAULT_SHELL_TRACKING = True
DEFAULT_TRACE_INLINE_MAX_LINES = 30
DEFAULT_REPLAY_EXPANDED_TURNS = 20
DEFAULT_TURN_SUMMARY_IN_STATUS = True


class AgentEditsRecordConfig(BaseModel):
    """Где вести журнал правок — по режиму запуска.

    Журнал питает дифф в ленте, сводку хода и экран изменений — всё это есть
    только в TUI, поэтому там запись включена по умолчанию, а ``tui: false``
    выключает все три разом. ``gc-cli`` (с ним — стенд и фоновые прогоны) и
    сервер по умолчанию не пишут: там журнал никто не смотрит, а запись стоила
    бы снимков файлов на диске и ``git status`` вокруг каждой команды.
    Библиотечное использование ядра режима не объявляет и не пишет.
    """

    model_config = ConfigDict(extra="forbid")

    tui: bool = True
    cli: bool = False
    server: bool = False


class AgentEditsConfig(BaseModel):
    """Yaml section ``agent_edits:`` — правки агента, не параметры fs-тулов.

    ``snapshots_max_files`` — потолок НЕ глобальный, и это сознательно.
    Вытеснение обходит снимки треда с живым журналом: снять базу живого треда
    значит молча отнять право на откат по файлу, который прямо сейчас виден на
    экране изменений. Кап убирает за тредами, которых больше нет
    (``SnapshotStore.evict_lru``), и за сброшенными
    (``thread_store.reset_thread_files``). В долгом треде с сотнями правленных
    файлов хранилище растёт сверх этого числа — цена сохранённого отката.
    """

    model_config = ConfigDict(extra="forbid")

    snapshots_max_files: int = DEFAULT_SNAPSHOTS_MAX_FILES
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS
    hunk_context_lines: int = DEFAULT_HUNK_CONTEXT_LINES
    max_diff_source_bytes: int = DEFAULT_MAX_DIFF_SOURCE_BYTES
    shell_tracking: bool = DEFAULT_SHELL_TRACKING
    trace_inline_max_lines: int = DEFAULT_TRACE_INLINE_MAX_LINES
    replay_expanded_turns: int = DEFAULT_REPLAY_EXPANDED_TURNS
    turn_summary_in_status: bool = DEFAULT_TURN_SUMMARY_IN_STATUS
    record: AgentEditsRecordConfig = Field(
        default_factory=AgentEditsRecordConfig
    )
