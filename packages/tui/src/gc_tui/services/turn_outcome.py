"""Словарь итогов хода: глиф и слова в одном месте.

Строка-итог индикатора ленты, статус в списке `/sessions` и заголовок
вкладки терминала читают одну таблицу глифов и слов — итог не
разъезжается (решение 23). Цвета у каждого места
свои. Фразы индикатора («Выполнил за 5с», «Ошибка», лимиты) и подписи
списка («✓ выполнено») живут рядом."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from gc_core.persistence.turn_outcome import TurnOutcome
from rich.text import Text

from gc_tui.palette import ACCENT, DEEP_DIM
from gc_tui.services.duration_format import format_duration
from gc_tui.services.plural import files_word
from gc_tui.services.relative_time import format_turn_clock
from gc_tui.services.trace_glyphs import ERROR_GLYPH

#: Подпись успешного исхода хода. Ход, кончившийся предложенным планом,
#: работой не является: «Выполнил» под планом читалось бы как «сделано».
DONE_LABEL = "Выполнил"
DONE_LABEL_PLAN = "План готов"


@dataclass(frozen=True)
class Outcome:
    glyph: str
    #: Подпись в списке сессий (без глифа); None — в списке не показывается.
    list_label: str | None
    #: Фраза строки-итога индикатора; для «completed» — подпись по умолчанию.
    sentence: str


_LIMIT_GLYPH = "⏹"

OUTCOMES: dict[str, Outcome] = {
    "completed": Outcome("✓", "выполнено", DONE_LABEL),
    "failed": Outcome(ERROR_GLYPH, "ошибка", "Ошибка"),
    "cancelled": Outcome("■", "остановлено", "Отменено"),
    # Мета говорит «идёт», а хода нет ни у одного живого процесса: процесс
    # умер или вышел посреди хода. Глиф стопа — ход прекращён не моделью.
    "interrupted": Outcome("■", "прервано", "Прервано"),
    "running": Outcome("●", "идёт", ""),
    "asking": Outcome("?", "ждёт ответа", ""),
    "stopped_steps": Outcome(
        _LIMIT_GLYPH, None, "Остановлено: превышен лимит шагов"
    ),
    "stopped_errors": Outcome(
        _LIMIT_GLYPH, None, "Остановлено: превышен лимит ошибок"
    ),
    "stopped_tokens": Outcome(
        _LIMIT_GLYPH, None, "Остановлено: превышен лимит токенов"
    ),
    "stopped_tool_calls": Outcome(
        _LIMIT_GLYPH,
        None,
        "Остановлено: превышен лимит вызовов инструментов",
    ),
    "stopped_judge": Outcome(
        _LIMIT_GLYPH,
        None,
        "Остановлено: проверка действий недоступна — повторите позже "
        "или смените режим",
    ),
}

#: Другие слова того же исхода: статус в meta треда и состояние индикатора.
_ALIASES = {
    "done": "completed",
    "stopped": "cancelled",
    "unknown_failed": "failed",
}


def settled_status(word: str | None, *, live: bool) -> str | None:
    """Статус для показа: «running», которого не держит живой ход, —
    «interrupted». Мету не правит: это чтение."""
    return "interrupted" if word == "running" and not live else word


#: Слова закрытого хода, которые читаются из статуса сессии.
_FINISHED_WORDS = frozenset(
    {"completed", "cancelled", "failed", "interrupted"}
)


def word_of_status(status: str | None) -> str | None:
    """Слово закрытого хода по статусу сессии (`tui.status`, верхний
    `status`, «interrupted» чтения); живой или неизвестный — None."""
    word = _ALIASES.get(status, status) if status else None
    return word if word in _FINISHED_WORDS else None


def meta_status_of(word: str) -> str:
    """Статус сессии (`tui.status`: кэш списка и заголовка) по слову
    исхода хода: лимиты и стоп — «stopped»."""
    word = _ALIASES.get(word, word)
    if word in ("completed", "failed"):
        return word
    return "stopped"


def outcome_of(word: str | None) -> Outcome | None:
    """Исход по слову статуса или состоянию индикатора; незнакомое — None."""
    if not word:
        return None
    return OUTCOMES.get(_ALIASES.get(word, word))


#: Причина остановки лимитом (`State.terminal_outcome` и поле `reason`
#: записи итога) → слово словаря.
_LIMIT_WORDS = {
    "step_limit": "stopped_steps",
    "tool_errors": "stopped_errors",
    "token_limit": "stopped_tokens",
    "tool_call_limit": "stopped_tool_calls",
    "judge_unavailable": "stopped_judge",
}

_PLAN_REASON = "plan_proposed"


def outcome_word(*, failed: bool, stopped: bool, reason: str | None) -> str:
    """Слово исхода по флагам прогона. Порядок важен:
    ошибка → стоп → лимитные исходы → completed (по умолчанию)."""
    if failed:
        return "failed"
    if stopped:
        return "stopped"
    return _LIMIT_WORDS.get(reason, "completed")


def _word(status: str, reason: str | None) -> str:
    word = outcome_word(
        failed=status == "error",
        stopped=status == "stopped" and reason not in _LIMIT_WORDS,
        reason=reason,
    )
    return _ALIASES.get(word, word)


def kind_of(outcome: TurnOutcome) -> tuple[str, bool]:
    """Слово `OUTCOMES` и флаг «ход кончился планом» по записи итога.
    Остановка с причиной, которой словарь не знает, — остановка."""
    plan = outcome.status == "done" and outcome.reason == _PLAN_REASON
    return _word(outcome.status, outcome.reason), plan


def status_of_last_turn(last_turn: object) -> str | None:
    """Статус сессии по короткому итогу последнего хода
    (``runtime.last_turn``); итога нет или он кривой — None."""
    if not isinstance(last_turn, dict):
        return None
    status, reason = last_turn.get("status"), last_turn.get("reason")
    if status not in ("done", "stopped", "error"):
        return None
    return meta_status_of(
        _word(status, reason if isinstance(reason, str) else None)
    )


def outcome_error(outcome: TurnOutcome) -> str | None:
    """Причина упавшего хода из записи итога (строка `└`)."""
    return (outcome.error or {}).get("message")


def turn_head(
    word: str,
    *,
    plan: bool,
    duration_sec: float | None,
    ended_at: str | None,
    now: datetime | None = None,
) -> Text:
    """Голова строки итога: глиф + фраза; «за <длительность>» только у
    выполненного хода, время конца — у любого, если оно известно."""
    outcome = outcome_of(word)
    if outcome is None:
        return Text("")
    done = outcome is OUTCOMES["completed"]
    sentence = DONE_LABEL_PLAN if done and plan else outcome.sentence
    head = f"{outcome.glyph} {sentence}"
    if done and duration_sec is not None:
        head += f" за {format_duration(duration_sec)}"
    clock = format_turn_clock(ended_at, now=now)
    return Text(f"{head} · {clock}" if clock else head)


# Минимальный зазор между головой строки итога и правым сегментом сводки.
SUMMARY_GAP = 2


def format_turn_summary(files: int, add: int, del_: int) -> str | None:
    """«изменено 3 файла · +120 −15»; ход без правок → None.

    Знаки счётчиков — те же, что в строке вызова с правкой (`(+a −r)`,
    U+2212)."""
    if files <= 0:
        return None
    return f"изменено {files} {files_word(files)} · +{add} −{del_}"


def compose_status_line(
    head: Text,
    summary: str | None,
    available_width: int,
    *,
    hover: bool = False,
) -> tuple[Text, tuple[int, int] | None]:
    """`(строка, x-диапазон сегмента)`; `None` — сегмента в строке нет.

    Сегмент не влезает по ширине — скрываем целиком: строка индикатора ровно
    в одну клетку высотой, а обрезанное «изменено 3 фай…» врало бы о числах.

    Диапазон отдаётся вместе с текстом, потому что он известен только здесь:
    сегмент кликабелен (открывает экран изменений в источнике «Ход»), а по
    цельному `Text` координаты его первой клетки не восстановить.

    `hover` — курсор стоит на сегменте: текст берёт АКЦЕНТ. Это единственный
    язык наведения, который база знает: `.trace-action:hover` красит
    `$accent`, других hover-правил в проекте нет. Сегмент — такой же
    кликабельный элемент, и отвечать руке он обязан тем же цветом.
    """
    if not summary:
        return head, None
    segment = Text(summary, style=ACCENT if hover else DEEP_DIM)
    gap = available_width - head.cell_len - segment.cell_len
    if gap < SUMMARY_GAP:
        return head, None
    out = Text()
    out.append_text(head)
    out.append(" " * gap)
    out.append_text(segment)
    return out, (head.cell_len + gap, out.cell_len)
