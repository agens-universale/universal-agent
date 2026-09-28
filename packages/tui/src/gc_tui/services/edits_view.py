"""Данные экрана правок: журнал + снапшоты + ``deltas`` → готовый вид.

Экран не считает сам (Р11): то же число в статусной строке хода и в шапке
экрана обязано приходить из одного расчёта — ``agent_edits.deltas``. Здесь
только проводка: какой набор записей берём, какую базу подставляем и что
пускаем в откат.

**Две породы баз, и роли у них разные** (см. ``agent_edits.snapshots``):

* СЧЁТ идёт от ``counting_*_baseline`` — более ранней из точного снимка
  fs-тула и счётной базы шелл-канала. Иначе дельта команды выпала бы из
  кумулятива;
* ОТКАТ получает только ТОЧНЫЙ снимок (``session_baseline``/``turn_baseline``),
  а право на него даёт предикат ``has_*_baseline``. Счётная база — содержимое
  индекса git, и запись её поверх файла стёрла бы несохранённые ручные правки
  пользователя. В ``revert_paths`` она не подаётся никогда.

Волна 4 добавляет принятие (``accept``/``reset_accept``): третья порода
снимка, ``.accepted`` (см. ``agent_edits.snapshots.AcceptedState``), — точный
снимок диска на момент явного принятия. ``session_view`` считает базу
«Сессии» по лестнице отсчёта (``agent_edits.ladder``): активные файлы — от
ближайшей ступени (принято/коммит/начало сессии), разобранные (нечего
смотреть — принято и чисто, либо чисто закоммичено) сворачиваются в отдельную
группу с кумулятивом сессии. ``revert``/``file_diff``/``revert_delta``
«Сессии» считают базу по той же лестнице (задача 9) — превью и запись всегда
одна и та же ступень; счётная база (может достаться только числу — см.
``_session_ladder_baseline``) на диск через них не попадает никогда.
``revert_targets`` называет цель отката словами (§4.4 спеки) и, только у
«Хода», предупреждает о ступенях, возникших после начала хода.

Источник базы выбирает представление, а не сервис молча (Р8): «Сессия» — база
до первой правки в треде, «Ход» — база до последнего хода с правками. У
старого хода точной базы нет (Р12) — ``FileEntry.exact`` в таком случае
``False``, и счётчики честно взяты из журнала.

Слой ``tui/services``: без Textual, чистые данные поверх ``agent_edits``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from gc_core.agent_edits import git_ops
from gc_core.agent_edits.deltas import (
    EMPTY_DELTA,
    FileDelta,
    FileEntry,
    abs_path_for_key,
    created_here,
    delta_vs_disk,
    file_entries,
)
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.ladder import STEP_ACCEPTED, STEP_HEAD, SessionStep, session_step
from gc_core.agent_edits.mode import resolve_mode
from gc_core.agent_edits.paths import is_external_key
from gc_core.agent_edits.revert import RevertOutcome, revert_paths
from gc_core.agent_edits.snapshots import (
    SnapshotStore,
    snapshots_root,
    thread_key_from_journal_path,
)
from gc_core.config_modules.agent_edits import (
    DEFAULT_HUNK_CONTEXT_LINES,
    DEFAULT_MAX_DIFF_SOURCE_BYTES,
)
from gc_core.thread_store import edits_journal_path
from gc_tui.services.edits_lookup import disk_signature, journal_signature

_logger = logging.getLogger(__name__)

SOURCE_SESSION = "session"
SOURCE_TURN = "turn"

# Контекст ∞ для режима «файл целиком»: весь файл одним хэнком. Число, а не
# признак, — чтобы не заводить второй путь расчёта ради того же diff.
FULL_FILE_CONTEXT = 1_000_000

# Причины отказа `accept`/`reset_accept` — тот же приём.
ACCEPT_EXTERNAL = "external"          # ключ вне проекта — базы не бывает, capture_accepted не зовём
ACCEPT_READ_FAILED = "read_failed"    # файл пропал/залочен между is_file() и read_bytes()
ACCEPT_TOO_LARGE = "too_large"        # файл больше max_diff_source_bytes — принятие недоступно
ACCEPT_WRITE_FAILED = "write_failed"  # стор не принял снимок: занятый лок индекса, полный диск
RESET_NOT_ACCEPTED = "not_accepted"   # у пути нет принятой базы — сбрасывать нечего
RESET_WRITE_FAILED = "write_failed"   # стор не отдал снимок: тот же класс отказов

# Причина попадания файла в группу «разобрано» (§4.1/§4.2 спеки волны 4) — уже
# готовые слова для колонки группы, не машинный код: словарь задачи фиксирует
# ровно эти два значения, третьего не заводим.
SETTLED_MANUAL = "вручную"    # жив accepted_state (даже если settled сработал по коммиту)
SETTLED_COMMIT = "коммитом"   # accepted_state нет, чисто относительно HEAD

# Подписи целей отката (§4.4 спеки) — готовые слова превью, собираются ЗДЕСЬ
# (граница сервиса и UI, задача 9): экран Textual их не формулирует.
TARGET_SESSION_START = "начало сессии"
TARGET_TURN_START = "начало хода"
TARGET_DELETE_CREATED = "будет удалён (создан агентом)"
TARGET_DELETE_ACCEPTED_ABSENT = "будет удалён (принято отсутствие файла)"
TARGET_DELETE_HEAD_ABSENT = "будет удалён (удаление закоммичено)"

# Причина «пропустить» цель отката — тот же приём: готовые слова, не машинный
# код (в отличие от REASON_* самого отката, который работает уже ПОСЛЕ факта).
SKIP_SETTLED = "изменений нет"
SKIP_NOT_REVERTABLE = "достоверной базы для отката нет"


@dataclass(frozen=True)
class AcceptOutcome:
    """Итог принятия: чья база сдвинута, что ушло в индекс, что пропущено (Р26/Р27/Р29)."""

    accepted: List[str]              # ключи, у которых сдвинута база
    staged: int                      # сколько ушло в индекс (включая удаления)
    stage_error: Optional[str]       # None — git add прошёл, либо принятие не в git-режиме
    skipped: List[Tuple[str, str]]   # (ключ, причина) — напр. «файл больше лимита»
    with_external: List[str]         # принятые, у которых entry.external — для плашки


@dataclass(frozen=True)
class ResetOutcome:
    """Итог отмены принятия: у кого снята база и для кого жива ступень HEAD."""

    reset: List[str]
    to_head: List[str]               # после сброса живёт ступень HEAD (условная плашка §13)
    skipped: List[Tuple[str, str]]


@dataclass(frozen=True)
class RowStep:
    """Действующая ступень лестницы отсчёта одного пути (§3.2 спеки волны 4).

    Не «у пути где-то есть `.accepted`» — а то, что РЕАЛЬНО победило в
    арбитраже ``agent_edits.ladder.session_step`` и питает счёт, превью
    отката и сам откат прямо сейчас (тай-брейк Р28 мог отдать более новому
    коммиту). ``ref`` — короткая подпись коммита («e5f2c1a · 14:20»),
    собранная ``git_ops.head_commit_label``: у ``kind=STEP_ACCEPTED`` её нет
    (``None``), у ``kind=STEP_HEAD`` нет времени принятия (``ts=None``) —
    ровно по одному «времени» на природу ступени.
    """

    kind: str                 # STEP_ACCEPTED | STEP_HEAD
    ts: Optional[float]       # момент принятия (kind=STEP_ACCEPTED), иначе None
    ref: Optional[str]        # короткая подпись коммита (kind=STEP_HEAD), иначе None


@dataclass(frozen=True)
class ActiveRow:
    """Файл в активном списке: обычный итог + действующая ступень отсчёта +
    факт принятой базы — два разных вопроса (финальный фикс волны 4 смоука,
    см. `tui.screens.diff._step_is_accepted`/`_has_accepted_base`): ``step``
    — от чего СЧИТАЮТСЯ числа (гейт глифа `↺`, действует только в «Сессии»,
    Р27), ``has_accepted_base`` — есть ли у пути `.accepted` вообще (гейт
    `u`/`Ctrl+U` — живёт и в «Ходе», Р27-ревью владельца на смоуке).
    """

    entry: FileEntry
    step: Optional[RowStep]     # None — счёт от начала сессии; в «Ходе» — ВСЕГДА None (Р27)
    has_accepted_base: bool     # есть ли `.accepted` у пути — НЕ то же самое, что `step`
    # Содержимое файла СЕЙЧАС и есть принятое — состояние, а не факт из
    # прошлого. Тот же расчёт, которым правило (а) отправляет путь в группу
    # «принято»: в «Сессии» такой путь до активного списка не доходит, а в
    # «Ходе» группы нет, и состояние показывает пометка строки.
    accepted_now: bool = False


@dataclass(frozen=True)
class SettledRow:
    """Файл в свёрнутой группе «разобрано»: кумулятив сессии, а не текущая дельта."""

    path: str
    add: int           # кумулятив СЕССИИ (от начала сессии до диска), не сумма записей
    del_: int
    kind: str           # created|modified|deleted
    reason: str          # SETTLED_MANUAL | SETTLED_COMMIT
    exact: bool = True   # False → счётчики из журнала: базы нет, сравнить было нечем


@dataclass(frozen=True)
class SessionView:
    """Готовый вид источника волны 4: активные (лестница) + разобранные (группа).

    ``settled`` у ``SOURCE_TURN`` всегда пуст — «Ход» лестницу и группу не
    видит (Р27, §4.1 спеки): его база — по-прежнему начало хода, без изменений
    волны 3. ``total_add``/``total_del`` — сумма ТОЛЬКО по ``active`` (шапка
    экрана считает активные файлы, разобранные — нет, §4.2).
    """

    source: str                  # SOURCE_SESSION | SOURCE_TURN
    turn_id: Optional[str]
    active: List[ActiveRow]
    settled: List[SettledRow]
    total_add: int
    total_del: int
    # Команды источника, урезанные потолком записи шелл-канала, — пары (ход,
    # настоящее число путей); пусто — урезанных нет, список полон. Пути сверх
    # потолка не записаны: их нет ни в ``active``, ни в ``settled``, и откатить
    # их отсюда нельзя — экран обязан в этом признаться. Пара и есть имя
    # команды: предупреждение скрывают про конкретную команду.
    cut_commands: Tuple[Tuple[str, int], ...] = ()


@dataclass(frozen=True)
class RevertTarget:
    """Куда вернёт `r` один путь — превью словами (§4.4 спеки, задача 9).

    ``label`` — готовый текст для шапки одиночного превью и стрелки массового,
    собран ЗДЕСЬ (граница сервиса и UI): Textual подставляет его как есть.
    ``skip_reason`` заполнен только при ``kind=="skip"`` — отката к этому пути
    нет вовсе (разобран в «Сессии», либо точной базы нет ни одной породы).
    ``warns`` непусты только у ``source=="turn"``: предупреждения о ступенях
    (принятие, коммит), возникших после начала хода — «Сессия» ниже своей
    ближайшей ступени не откатывается по построению, предупреждать не о чем.
    """

    path: str
    kind: str                 # accepted|head|session|turn|delete|skip
    label: str
    skip_reason: Optional[str]
    warns: Tuple[str, ...]


@dataclass(frozen=True)
class ResetTarget:
    """Что сброс принятия сделает с одним путём (Д5 второго смоука волны 4).

    ОДИН расчёт на «что показано» и «что произойдёт»: подтверждение сброса
    называет и цену (``loses_point`` — точки отката к принятому состоянию не
    станет), и последствие (``target`` — откуда пойдёт счёт), и час принятия
    (``accepted_ts``) — раньше цену считал один метод, цель другой, и
    заголовок «у N файлов» расходился с тем, у скольких путей принятие
    реально снимется.

    ``loses_point`` — правило §4.3: дельта от принятой базы непуста ЛИБО
    сравнить её было нечем (бинарь, файл сверх капа, отказ чтения снимка) —
    безопасная сторона. У пути без принятой базы терять нечего (``False``), и
    ``accepted_ts`` у него нет; ``accepted_ts`` пуст и когда снимок не
    прочитался — подпись обязана прожить без часа, а не выдумать его.
    """

    path: str
    target: RevertTarget          # куда пойдёт счёт после сброса
    loses_point: bool             # исчезнет ли точка отката к принятому состоянию
    accepted_ts: Optional[float]  # момент принятия; None — базы нет либо не прочиталась


def diff_limits(config: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    """``(max_diff_source_bytes, hunk_context_lines)`` из секции ``agent_edits``.

    Одна точка чтения на сервис и на сводку хода: разъехавшиеся лимиты — это
    разные числа в статусной строке и на экране.

    Мусор в конфиге (``true``, строка, отрицательное) заменяется дефолтом, но
    ``hunk_context_lines: 0`` — законное значение: дифф без контекстных строк.
    """
    section = (config or {}).get("agent_edits") or {}
    return (
        _limit(section, "max_diff_source_bytes", DEFAULT_MAX_DIFF_SOURCE_BYTES,
               minimum=1),
        _limit(section, "hunk_context_lines", DEFAULT_HUNK_CONTEXT_LINES,
               minimum=0),
    )


def _limit(section: Dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    raw = section.get(key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _records_for_path(
    records: Sequence[EditRecord], path_key: str
) -> List[EditRecord]:
    """Записи одного пути в хронологии — общий отбор для тела и для подписей.

    Сортировка стабильная: при равных ``ts`` сохраняется порядок дописывания
    в журнал.
    """
    return sorted(
        (rec for rec in records if rec.path == path_key), key=lambda rec: rec.ts
    )


def _format_hhmm(ts: float) -> str:
    """Момент как «часы:минуты» локального времени — формат подписей §4.4 спеки."""
    return f"{datetime.fromtimestamp(ts):%H:%M}"


def _head_state_label(commit_ref: Optional[str]) -> str:
    """«закоммиченное состояние (ref)» — одна ступень, один текст на неё:
    читают и цель отката (`_session_revert_target`), и цель сброса
    (`reset_targets`, задача 4 доп. волны 4 смоука) — без общего места
    формулировка разъехалась бы между ними так же, как разъехались
    `HEAD_CAPTION_STEM`/`RESET_FROM_HEAD` в `tui.screens.diff` (тот же класс
    находки, тем же приёмом чинится и здесь).
    """
    return f"закоммиченное состояние ({commit_ref})" if commit_ref else "закоммиченное состояние"


def _row_step(step: Optional[SessionStep]) -> Optional[RowStep]:
    """``SessionStep`` лестницы → ``RowStep`` строки: наружу идёт только то,
    что нужно экрану — момент принятия у ``accepted``, готовая подпись
    коммита (``head_ref``, уже собранная батчем при расчёте лестницы) у
    ``head``. Один расчёт (``self._session_ladder``) питает и это, и счёт, и
    откат — здесь только проекция в форму строки списка.
    """
    if step is None:
        return None
    if step.kind == STEP_ACCEPTED:
        return RowStep(kind=STEP_ACCEPTED, ts=step.ts, ref=None)
    return RowStep(kind=STEP_HEAD, ts=None, ref=step.commit_ref)


class _LadderGitFacts:
    """``agent_edits.ladder.GitFacts`` — один экземпляр на ``EditsView``, с кэшем.

    ``has_filter``/``head_bytes``/``head_ref`` спрашивают дорогие git-вызовы
    (``check-attr``, ``cat-file``, ``log``) и кэшируют ответ по
    ``(rel, head_rev)``: ревизия сама инвалидирует запись — коммит меняет
    ``head_rev``, и старая пара просто перестаёт совпадать, без явного сброса.
    Кэш переживает отдельные сборки ``session_view`` (§3.2 спеки: «байты
    поднимаются только на промахе кэша, не на каждую пересборку вида») —
    без него правка ЛЮБОГО файла в наборе пересчитывала бы ступень всем
    остальным, не поменявшимся, заново.

    ``head_oid`` — из батча, который сборка передаёт через ``prime()``: у
    ``git ls-tree`` есть настоящий batch-режим (один вызов на весь набор
    путей), кэшировать его по отдельности незачем.

    ``prime()`` вызывается перед КАЖДЫМ использованием — задаёт контекст
    текущего запроса (ревизия + oid'ы). Однопоточный сервис, конкурентного
    доступа нет.

    У ``head_bytes`` в ключ входит ещё и стиль концов строк файла на диске:
    байты ступени следуют ему, и правка, сменившая стиль, обязана промахнуться
    мимо старой записи.
    """

    def __init__(self, launch_root: str, max_source_bytes: int) -> None:
        self._launch_root = launch_root
        self._max_source_bytes = max_source_bytes
        self._head_rev: Optional[str] = None
        self._oids: Dict[str, str] = {}
        self._bytes_cache: Dict[
            Tuple[str, Optional[str], Optional[bytes], bool], Optional[bytes]
        ] = {}
        self._filter_cache: Dict[Tuple[str, str], bool] = {}
        self._ref_cache: Dict[Tuple[str, str], Optional[str]] = {}
        self._commit_ts_cache: Dict[Tuple[str, str], Optional[float]] = {}
        # Сырые блобы HEAD, прочитанные пачкой (`prefetch`); ``None`` — пути в HEAD нет.
        self._blob_cache: Dict[Tuple[str, str], Optional[bytes]] = {}
        # Пути ДОКАЗАННО нет в HEAD (`head_absent`); отказ git сюда ложится как «есть».
        self._absent_cache: Dict[Tuple[str, str], bool] = {}

    def prime(self, head_rev: str, oids: Dict[str, str]) -> None:
        """Задаёт контекст текущего запроса (ревизия + батч oid'ов).

        Вызов с той же ревизией и пустым ``oids`` — сигнал «мне нужен только
        ``head_rev`` для ключей кэша, свежий батч не считал» (так зовёт
        ``_head_step`` — при ``accepted=None`` тай-брейк по oid не читается
        вовсе, см. его докстринг). Такой вызов НЕ должен стирать батч,
        который для этой же ревизии уже подготовил предыдущий ``prime()``
        (обычно — ``_compute_session_steps``): иначе более поздний
        `_head_step` в той же сборке вида тихо срывает откат на HEAD вместо
        принятой ступени. Смена ревизии — другое дело: старые oid'ы ей не
        соответствуют и обязаны уйти, даже если новых нет.
        """
        if head_rev == self._head_rev and not oids:
            return
        self._head_rev = head_rev
        self._oids = oids

    def prefetch(self, by_rev: Dict[str, List[str]]) -> None:
        """Собрать ПАЧКОЙ то, что лестница спросит по закоммиченным путям.

        ``by_rev`` — закоммиченные пути по ревизии их снимка. Лестница спрашивает
        у такого пути атрибуты, байты HEAD и подпись коммита (плюс время — для
        предупреждений «Хода»), и по одному это три запуска git на путь в потоке
        UI: 2000 путей — 26 с открытия экрана изменений. Здесь — по запуску на
        кусок путей; ответы ложатся в те же кэши, что читают методы ниже, а путь,
        которого пачка не покрыла, они спросят сами, по одному.

        Зовётся после ``prime``: ключи кэшей несут ревизию текущего запроса.
        """
        head_rev = self._head_rev
        if head_rev is None:
            return
        rels = [rel for paths in by_rev.values() for rel in paths]
        unchecked = [rel for rel in rels if (rel, head_rev) not in self._filter_cache]
        filtered = git_ops.custom_filter_paths(self._launch_root, unchecked)
        for rel in unchecked:
            self._filter_cache[(rel, head_rev)] = rel in filtered
        # Блоб файла сверх капа источника лестнице не нужен: `head_bytes` такой
        # файл с диска не читает и берёт форму выкладки — в пачке он был бы
        # чистым балластом памяти.
        unread = [
            rel for rel in rels
            if not self._filter_cache[(rel, head_rev)]
            and (rel, head_rev) not in self._blob_cache
            and self._within_source_cap(rel)
        ]
        for rel, blob in git_ops.blobs_at(self._launch_root, unread, rev="HEAD").items():
            self._blob_cache[(rel, head_rev)] = blob
        for since, paths in by_rev.items():
            unsigned = [rel for rel in paths if (rel, head_rev) not in self._ref_cache]
            for rel, (short_hash, ts) in git_ops.last_commits(
                self._launch_root, since, unsigned
            ).items():
                self._ref_cache[(rel, head_rev)] = git_ops.commit_label(short_hash, ts)
                self._commit_ts_cache[(rel, head_rev)] = float(ts)

    def _within_source_cap(self, rel: str) -> bool:
        """Файл на диске есть и не больше капа источника — его байты лестница читает."""
        path = abs_path_for_key(self._launch_root, rel)
        try:
            return path.is_file() and path.stat().st_size <= self._max_source_bytes
        except OSError:
            return False

    def head_bytes(self, rel: str) -> Optional[bytes]:
        # Байты HEAD зависят от стиля концов строк файла на диске: ключ кэша
        # несёт этот стиль, и правка, сменившая его, промахивается сама.
        # Кап источника проверяется по ``stat`` ДО чтения, и это тот же
        # НАСТРОЕННЫЙ лимит (``agent_edits.max_diff_source_bytes``), с которым
        # считает ``deltas.delta_vs_disk``: файл сверх капа целиком не читается,
        # и база остаётся в форме выкладки. Разъехавшись с лимитом счёта, кап
        # вернул бы «весь файл заменён» на файле, который счёт уже пускает.
        path = abs_path_for_key(self._launch_root, rel)
        try:
            disk = (
                path.read_bytes()
                if path.is_file()
                and path.stat().st_size <= self._max_source_bytes
                else None
            )
        except OSError:
            disk = None
        eol = git_ops.disk_eol(disk) if disk is not None else None
        key = (rel, self._head_rev, eol, disk is None)
        if key not in self._bytes_cache:
            blob_key = (rel, self._head_rev)
            if disk is not None and blob_key in self._blob_cache:
                # Блоб уже прочитан пачкой (`prefetch`) — то же правило формы,
                # что у `head_bytes_like_disk`, без запуска git.
                blob = self._blob_cache[blob_key]
                self._bytes_cache[key] = (
                    None if blob is None else git_ops.blob_like_disk(blob, disk)
                )
            else:
                self._bytes_cache[key] = git_ops.head_bytes_like_disk(
                    self._launch_root, rel, disk, rev="HEAD"
                )
        return self._bytes_cache[key]

    def head_absent(self, rel: str) -> bool:
        """Удаление пути закоммичено, и файла нет и на диске.

        Отсутствие в HEAD само по себе удалением не является: путь пропадает из
        HEAD и тогда, когда файл снят с учёта (`git rm --cached`), пересоздан
        после коммита или переименован одним регистром — файла или каталога,
        на macOS и Windows `ls-tree` различает регистр, а диск нет. Во всех этих
        случаях файл лежит на диске, и ступень «файла нет» заставила бы откат
        его УДАЛИТЬ. Поэтому ступень строится, только если файла нет и на диске:
        откат тогда удалять нечего, а путь честно уходит в разобранные.

        Отказ git — «есть». Отсутствие в HEAD кэшируется по ревизии, диск
        проверяется при каждом обращении: файл может появиться снова.
        """
        key = (rel, self._head_rev)
        if key not in self._absent_cache:
            tree = git_ops.head_tree(self._launch_root, [rel])
            self._absent_cache[key] = tree is not None and rel not in tree
        if not self._absent_cache[key]:
            return False
        path = abs_path_for_key(self._launch_root, rel)
        try:
            return not path.exists() and not path.is_symlink()
        except OSError:
            return False

    def note_head_tree(self, rels: Sequence[str], tree: Optional[Dict[str, str]]) -> None:
        """Ответ ``ls-tree`` по ``rels``, уже полученный сборкой, — в кэш отсутствия.

        Тот же запуск, что дал oid'ы (``prime``): второй по тем же путям только
        ради «есть ли путь в HEAD» был бы лишним. ``None`` — git отказал, и
        отсутствие не доказано: кэш не трогаем, путь спросит сам.
        """
        if tree is None or self._head_rev is None:
            return
        for rel in rels:
            self._absent_cache[(rel, self._head_rev)] = rel not in tree

    def head_oid(self, rel: str) -> Optional[str]:
        return self._oids.get(rel)

    def has_filter(self, rel: str) -> bool:
        key = (rel, self._head_rev)
        if key not in self._filter_cache:
            self._filter_cache[key] = git_ops.has_custom_filter(self._launch_root, rel)
        return self._filter_cache[key]

    def head_ref(self, rel: str) -> Optional[str]:
        key = (rel, self._head_rev)
        if key not in self._ref_cache:
            self._ref_cache[key] = git_ops.head_commit_label(self._launch_root, rel)
        return self._ref_cache[key]

    def head_commit_ts(self, rel: str) -> Optional[float]:
        """``%ct`` последнего коммита пути — ТОЛЬКО для предупреждений «Хода»
        (§4.4): «коммит моложе начала хода», не выбор ступени (§3.2 сравнивает
        только blob-oid). Отдельный кэш от ``head_ref``: тому нужна ГОТОВАЯ
        подпись, этому — сырое число для сравнения со временем записи журнала.
        """
        key = (rel, self._head_rev)
        if key not in self._commit_ts_cache:
            self._commit_ts_cache[key] = git_ops.head_commit_ts(self._launch_root, rel)
        return self._commit_ts_cache[key]


def _cut_commands(records: List[EditRecord]) -> Tuple[Tuple[str, int], ...]:
    """Урезанные команды набора — (ход, число путей), без повторов, по порядку.

    Отдельного имени у команды в журнале нет: её записи несут только ход и
    ``command_paths``. Две урезанные команды одного хода с РАВНЫМ числом путей
    сольются в одну — скрыв одну, скроешь обе; дописывать ради этого в журнал
    имя команды несоразмерно.
    """
    seen = dict.fromkeys(
        (rec.turn_id, rec.command_paths) for rec in records if rec.command_paths
    )
    return tuple(seen)


class EditsView:
    """Данные экрана правок одного треда: два источника, откат, принятие."""

    def __init__(
        self,
        threads_dir: str,
        system_id: str,
        thread_id: str,
        launch_root: str,
        config: Dict[str, Any],
    ) -> None:
        journal_path = edits_journal_path(threads_dir, system_id, thread_id)
        self._journal = EditsJournal(journal_path)
        self._launch_root = str(launch_root)
        self._store = SnapshotStore(
            snapshots_root(self._launch_root),
            thread_key_from_journal_path(journal_path),
            Path(threads_dir),
        )
        self._max_source_bytes, self._hunk_context_lines = diff_limits(config)
        self._mode: Optional[str] = None
        # Ключ включает ревизию HEAD (см. `session_view`): без неё коммит,
        # сделанный при открытом экране, не был бы виден. Читается она ФАЙЛАМИ
        # (`head_rev_from_files`), а не подпроцессом: ключ считается до проверки
        # кэша — иначе нельзя, ключ и есть вопрос «не устарело ли», — а вид
        # зовут часто. Когда превью отката звало его на каждый путь, массовый
        # откат шестидесяти файлов платил шестьдесят с лишним запусков git в
        # потоке интерфейса: 2,2 секунды до окна подтверждения (пятый раунд
        # ревью, находка 9). Теперь превью собирает вид раз на набор
        # (`revert_deltas`), но ключ остаётся дешёвым.
        self._cache: Dict[str, Tuple[Any, SessionView]] = {}
        self._ladder_facts = _LadderGitFacts(self._launch_root, self._max_source_bytes)
        # Ступень лестницы «Сессии» на путь — свежа СРАЗУ ПОСЛЕ любого вызова
        # `session_view(SOURCE_SESSION)` (её ставит `_build_session_view` на
        # каждый промах кэша); `file_diff`/`revert_delta`/`revert`/
        # `revert_targets` обязаны прогреть её сами перед чтением.
        self._session_ladder: Dict[str, Optional[SessionStep]] = {}
        # Пути, реально уехавшие в коммит за сессию — живёт вместе со ступенями
        # (см. `_compute_session_steps`), читает правило членства в группе.
        self._session_committed: set = set()

    @property
    def mode(self) -> str:
        """``git-mode``/``cache-mode``: от него зависит доступность ``git add``.

        Считается один раз: репозиторий не перестаёт быть репозиторием посреди
        сессии, а ``resolve_mode`` ходит в PATH за самим git.
        """
        if self._mode is None:
            self._mode = resolve_mode(self._launch_root)
        return self._mode

    def invalidate(self) -> None:
        """Забыть посчитанное: журнал или файлы изменились извне."""
        self._cache.clear()

    def session_view(self, source: str) -> SessionView:
        """Вид источника: активные (лестница отсчёта) + разобранные (группа, §4.1/§4.2).

        Кэш — по подписи журнала, подписям файлов набора, ``rev-parse HEAD`` и
        подписи индекса снимков: коммит не трогает ни журнал, ни рабочее
        дерево, и без ревизии в ключе файл, закоммиченный при открытом экране,
        не уехал бы в группу разобранных до случайной инвалидации; принятие
        (в том числе из другого окна) меняет только индекс. Ревизия читается
        один раз на сборку (``SOURCE_TURN`` её не спрашивает вовсе — лестница и
        группа «Хода» не касаются, Р27).
        """
        records, turn_id = self._records(source)
        head_rev = (
            git_ops.head_rev_from_files(self._launch_root)
            if source == SOURCE_SESSION and self.mode == "git-mode"
            else None
        )
        # Подпись индекса снимков — ради принятия: оно меняет только индекс, и
        # принятие из другого окна иначе не сбрасывало бы кэш — откат отсюда
        # вёл бы к началу сессии мимо принятого состояния.
        key = (turn_id, journal_signature(records),
               disk_signature(self._launch_root, records), head_rev,
               self._store.index_signature())
        cached = self._cache.get(source)
        if cached is not None and cached[0] == key:
            return cached[1]

        view = (
            self._build_session_view(records, head_rev)
            if source == SOURCE_SESSION
            else self._build_turn_view(records, turn_id)
        )
        self._cache[source] = (key, view)
        return view

    def file_diff(
        self,
        source: str,
        path_key: str,
        *,
        full: bool = False,
    ) -> Tuple[str, bool]:
        """``(unified-хэнки, exact)`` тела файла в этом представлении.

        ``exact=False`` — базы нет: тело собрано из хэнков журнала подряд, это
        пошаговая история правок, а не разница «до → сейчас». Форма хэнков у
        двух каналов разная (difflib у fs-тулов, ``git diff`` у шелл-канала) —
        обе валидны, движок рендера читает и ту и другую.

        База берётся по тому же правилу, что счётчики списка: у созданного
        здесь файла она пуста, пока у пути нет ступени лестницы отсчёта —
        иначе снимок «после первой правки» съел бы из тела весь сочинённый
        файл. Ступень выше (``_stepped``, то же правило, что
        ``stepped_over_created`` у ``deltas.file_entries``) эту трактовку
        снимает: тело диффа становится дельтой от ступени, а не всем файлом
        (находка гейта волны 4 — раньше эта функция проверяла
        ``created_here`` в обход поправки и лгала о размере файла). У
        «Сессии» база без ступени — ступень лестницы отсчёта (задача 9): та
        же, что использует счёт и откат, — превью не имеет права разойтись с
        тем, что реально отменит `r`.

        РАЗОБРАННЫЙ путь (в ``settled``, а не в ``active``) считается от базы
        НАЧАЛА СЕССИИ: вьюер группы показывает кумулятив сессии — «что агент
        сделал с файлом всего» (§4.2/Р31), тем же расчётом, что числа строки
        группы (§12). Раньше такой путь не находился среди активных и тело
        собиралось суммой записей журнала — не «до → сейчас», а история
        шагов, с ложной пометкой «точный снимок недоступен» при живом
        `.orig` (находка финального ревью ветки, I1).
        """
        records, turn_id = self._records(source)
        path_records = _records_for_path(records, path_key)
        if not path_records:
            return "", False

        view = self.session_view(source)
        entry = next(
            (row.entry for row in view.active if row.entry.path == path_key), None,
        )
        settled = entry is None and any(row.path == path_key for row in view.settled)
        created = created_here(path_records)
        if entry is not None and entry.exact:
            if created and source == SOURCE_SESSION and self._stepped(path_key):
                created = False
            baseline = (
                None
                if created
                else (
                    self._session_ladder_baseline(path_key)
                    if source == SOURCE_SESSION
                    else self._counting_baseline(source, turn_id)(path_key)
                )
            )
        elif settled:
            # Разобранный файл: тело — КУМУЛЯТИВ СЕССИИ, тем же расчётом и от
            # той же базы, что числа строки группы (§12: сумма записей журнала
            # воспроизвела бы «+293 при реальных +133»). Ступень лестницы тут
            # не при чём: дельта от неё пуста — на то файл и разобран.
            baseline = None if created else self._store.counting_session_baseline(path_key)
            if baseline is None and not created:
                # Базы НЕТ вовсе (снимки вычищены; путь, который рекордер не
                # снимал) — это не «база пуста»: пустую базу легализует только
                # `created_here` (контракт `delta_vs_disk`, то же правило, что
                # в `deltas.file_entries`). Иначе тело объявило бы весь файл
                # написанным агентом — вранья громче, чем сумма записей.
                return "".join(rec.hunks for rec in path_records if rec.hunks), False
        else:
            return "".join(rec.hunks for rec in path_records if rec.hunks), False

        delta = delta_vs_disk(
            baseline,
            abs_path_for_key(self._launch_root, path_key),
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=(
                FULL_FILE_CONTEXT if full else self._hunk_context_lines
            ),
        )
        if settled and not delta.countable:
            # Сравнить стороны нечем (бинарь, кап): у активного пути это видно
            # заранее по `entry`, у разобранного выясняется только здесь —
            # `SettledRow` признака не несёт. Честная деградация к журналу.
            return "".join(rec.hunks for rec in path_records if rec.hunks), False
        return delta.hunks, True

    def revert_delta(self, source: str, path_key: str) -> FileDelta:
        """Что именно отменит откат: ТОЧНАЯ база представления против диска.

        Отдельный расчёт от ``file_diff`` нужен потому, что базы у счёта и у
        отката разные. ``file_diff`` идёт от счётной базы (более ранней из
        точного снимка и базы шелл-канала), а ``revert_paths`` пишет точный
        снимок. Стоит шелл-каналу записать ``.gitorig`` раньше рекордера — и
        базы расходятся; подтверждение, собранное от счётной, показывало бы не
        то, что произойдёт. Живой случай: команда удалила строку, инструмент
        вернул её обратно — от счётной базы «различий нет», а откат к точному
        снимку эту строку уничтожает.

        ``countable=False`` — сравнить стороны было нечем (бинарь, файл сверх
        капа): откат такого пути законен, но ни диффа, ни чисел у него нет.

        Правило «у созданного здесь файла база пуста» то же, что в счётчиках
        списка: снимок такого пути — середина набора, а не «до» — но только
        пока у пути нет ступени лестницы отсчёта (``_stepped``); со ступенью
        выше база — она (находка гейта волны 4: раньше эта функция проверяла
        ``created_here`` в обход поправки, и превью отката лгало о масштабе
        перед подтверждением, хотя сам `revert()` уже писал верно).
        """
        return self.revert_deltas(source, [path_key])[0]

    def revert_deltas(self, source: str, path_keys: Sequence[str]) -> List[FileDelta]:
        """``revert_delta`` для многих путей — вид и выборка записей ОДИН раз.

        Превью массового отката звало ``revert_delta`` на каждый путь, а тот
        перебирал все записи журнала и пересобирал ключ кэша вида — ``stat`` по
        всем путям набора. Квадрат: на 10 000 файлов ~10^8 системных вызовов,
        и `Ctrl+R` вешал интерфейс (смок 2026-09-24). Правило базы — одно, здесь.
        """
        records, turn_id = self._records(source)
        by_path: Dict[str, List[EditRecord]] = {}
        for rec in sorted(records, key=lambda rec: rec.ts):   # стабильно, как `_records_for_path`
            by_path.setdefault(rec.path, []).append(rec)
        # Прогревает `self._session_ladder` (задача 9): без вызова база
        # «Сессии» ниже могла бы читать устаревшую (или ещё не построенную)
        # ступень — `revert()` не обещал бы то же, что показывает подтверждение.
        self.session_view(source)
        precise = self._precise_baseline(source, turn_id)
        deltas: List[FileDelta] = []
        for path_key in path_keys:
            path_records = by_path.get(path_key)
            if not path_records:
                deltas.append(EMPTY_DELTA)
                continue
            created = created_here(path_records)
            if created and source == SOURCE_SESSION and self._stepped(path_key):
                created = False
            deltas.append(delta_vs_disk(
                None if created else precise(path_key),
                abs_path_for_key(self._launch_root, path_key),
                max_source_bytes=self._max_source_bytes,
                hunk_context_lines=self._hunk_context_lines,
            ))
        return deltas

    def file_records(self, source: str, path_key: str) -> List[EditRecord]:
        """Записи журнала по одному пути в хронологии.

        Нужны там, где точной базы нет: тело вьюера собирается из ОТДЕЛЬНЫХ
        правок, и у каждой своя подпись — время и канал. ``file_diff`` их
        склеивает, а границы и время в склейке теряются.
        """
        records, _turn_id = self._records(source)
        return _records_for_path(records, path_key)

    def revert_state(self, path_keys: Sequence[str]) -> Tuple:
        """Всё, от чего зависит откат путей, — снимок «что показали в превью».

        Подтверждение берёт его при показе и сверяет перед записью. Разошлось —
        откат записал бы то, чего пользователь не видел:

        * файлы на диске — правка при открытом окне. Подпись не только размер и
          время изменения: время выставляет кто угодно (`cp -p`, `rsync -a`), а
          на части файловых систем оно грубое. Номер файла меняет атомарная
          замена, ``ctime`` — любая запись, и программе его не выставить;
        * индекс снимков — принятие или его снятие в другом окне меняет цель
          отката «Сессии», а диск не трогает;
        * ревизия HEAD — коммит двигает ступень «закоммичено» той же ценой.
        """
        files = []
        for key in path_keys:
            try:
                st = abs_path_for_key(self._launch_root, key).stat()
            except OSError:
                files.append((key, None))
            else:
                files.append((key, st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns))
        head_rev = (
            git_ops.head_rev_from_files(self._launch_root)
            if self.mode == "git-mode" else None
        )
        return (tuple(files), self._store.index_signature(), head_rev)

    def revert_targets(self, source: str, path_keys: Sequence[str]) -> List[RevertTarget]:
        """Куда вернёт `r` каждый путь — превью словами, до самого отката (§4.4 спеки).

        Источник задаёт форму цели (Р8): «Сессия» идёт по лестнице отсчёта
        (``accepted``/``head``/``session``/``delete``), «Ход» — всегда к
        началу хода (``turn``), но с предупреждениями о ступенях, возникших
        ПОСЛЕ начала хода (принятие или коммит командой — единственные
        реальные случаи, принятие во время прогона отклонено). Путь без
        ступени и без точного снимка (только счётная база — контракт §12:
        она НИКОГДА не доезжает до отката) и путь, разобранный в «Сессии»
        (отменять нечего — §4.2), приходят как ``kind="skip"`` у ОБОИХ
        источников.
        """
        view = self.session_view(source)
        active_by_path = {row.entry.path: row.entry for row in view.active}
        settled_paths = {row.path for row in view.settled}   # у «Хода» всегда пусто

        head_rev: Optional[str] = None
        turn_start_ts: Optional[float] = None
        if source == SOURCE_TURN:
            turn_records, _turn_id = self._records(SOURCE_TURN)
            turn_start_ts = min((rec.ts for rec in turn_records), default=None)
            if self.mode == "git-mode":
                head_rev = git_ops.head_rev_from_files(self._launch_root)
        committed = (
            self._prepare_head_steps(path_keys, head_rev)
            if turn_start_ts is not None else None
        )

        targets: List[RevertTarget] = []
        for key in path_keys:
            if key in settled_paths:
                targets.append(RevertTarget(key, "skip", "", SKIP_SETTLED, ()))
                continue
            entry = active_by_path.get(key)
            if entry is None or not entry.revertable:
                targets.append(RevertTarget(key, "skip", "", SKIP_NOT_REVERTABLE, ()))
                continue
            if source == SOURCE_SESSION:
                targets.append(self._session_revert_target(key, entry))
            else:
                targets.append(
                    self._turn_revert_target(key, entry, head_rev, turn_start_ts, committed)
                )
        return targets

    def _session_revert_target(self, key: str, entry: FileEntry) -> RevertTarget:
        """Цель «Сессии» для одного пути — та же ступень, что база отката/счёта.

        Ступень проверяется ПЕРВОЙ: `kind == "created"` бывает и у пути, чья
        ступень — принятое отсутствие файла (его дельта считается от пустоты,
        §3.1), а причина удаления у них разная — «создан агентом» против
        «принято отсутствие файла», и путать их значило бы врать о том,
        почему файл исчезнет.
        """
        step = self._session_ladder.get(key)
        if step is not None and step.content is None:
            label = (
                TARGET_DELETE_ACCEPTED_ABSENT if step.kind == STEP_ACCEPTED
                else TARGET_DELETE_HEAD_ABSENT
            )
            return RevertTarget(key, "delete", label, None, ())
        if entry.kind == "created":
            # Ступени с содержимым нет (иначе `file_entries` не дал бы этот
            # kind, см. §3.2 «Пересмотр правила») — откат удаляет файл.
            return RevertTarget(key, "delete", TARGET_DELETE_CREATED, None, ())

        if step is None:
            return RevertTarget(key, "session", TARGET_SESSION_START, None, ())
        if step.kind == STEP_ACCEPTED:
            label = f"принятое состояние ({_format_hhmm(step.ts)})"
            return RevertTarget(key, STEP_ACCEPTED, label, None, ())
        return RevertTarget(key, STEP_HEAD, _head_state_label(step.commit_ref), None, ())

    def _turn_revert_target(
        self,
        key: str,
        entry: FileEntry,
        head_rev: Optional[str],
        turn_start_ts: Optional[float],
        committed: Optional[set],
    ) -> RevertTarget:
        """Цель «Хода» — всегда начало хода; предупреждения о более поздних ступенях."""
        if entry.kind == "created":
            return RevertTarget(key, "delete", TARGET_DELETE_CREATED, None, ())

        warns: List[str] = []
        try:
            accepted = self._store.accepted_state(key)
        except (OSError, TimeoutError):
            # Замок индекса снимков занят (другое окно, CLI, субагенты): превью
            # без предупреждения о принятом — беднее правды, но экран на `r` жив.
            _logger.warning("принятое состояние не прочитано: %s", key, exc_info=True)
            accepted = None
        if (
            accepted is not None
            and turn_start_ts is not None
            and accepted.ts > turn_start_ts
        ):
            warns.append(
                f"заденет принятое вами состояние (принято в {_format_hhmm(accepted.ts)})"
            )

        if head_rev is not None and turn_start_ts is not None:
            try:
                head_step = self._head_step(key, head_rev, committed)
            except (OSError, TimeoutError):
                # Та же деградация, что у снятия принятия: превью без
                # предупреждения о коммите беднее правды, но экран на `r` жив.
                _logger.warning("ступень HEAD не определена: %s", key, exc_info=True)
                head_step = None
            if head_step is not None:
                rel = None if Path(key).is_absolute() else key
                commit_ts = self._ladder_facts.head_commit_ts(rel) if rel else None
                if commit_ts is not None and commit_ts >= turn_start_ts:
                    ref = head_step.commit_ref
                    warns.append(
                        f"заденет коммит {ref}, сделанный после начала хода"
                        if ref else "заденет коммит, сделанный после начала хода"
                    )

        return RevertTarget(key, "turn", TARGET_TURN_START, None, tuple(warns))

    def reset_targets(self, path_keys: Sequence[str]) -> List[ResetTarget]:
        """Что снятие принятия (`u`/`Ctrl+U`) сделает с каждым путём — до
        самого сброса (задача 4 доп. волны 4 смоука, рядом с `revert_targets`;
        цена потери — Д5 второго смоука): подтверждение не вправе считать это
        само — «что показано» и «что произойдёт» обязаны идти из одного
        расчёта. Отсюда же берётся и гейт «спрашивать ли вообще» (§4.3):
        спрашиваем, пока хоть у одного пути ``loses_point``.

        Без параметра источника: принятая база — глобальный маркер пути, не
        привязанный к «Сессии»/«Ходу» (`reset_accept` — тот же контракт), а
        сам `u` достижим везде, где у пути есть принятая база
        (`ActiveRow.has_accepted_base`, финальный фикс волны 4) — включая
        «Ход» (Р27-ревью владельца на смоуке: клавиша осталась рабочей там,
        эффект просто виден в «Сессии»). Цель ниже при этом ВСЕГДА про
        лестницу «Сессии»: своей лестницы у «Хода» нет (Р27), а принятая
        база — маркер сессионный независимо от того, откуда пришёл `u`.

        Ближайшая ОСТАВШАЯСЯ ступень — тот же расчёт, которым `reset_accept`
        пост-фактум решает `ResetOutcome.to_head` (`_head_step`): HEAD, если
        коммит отличен от снимка начала сессии, иначе начало сессии. Путь без
        принятой базы сюда по контракту вызова не доезжает (экран отбирает
        пути по `has_accepted_base`/`SETTLED_MANUAL`) — но при случайном
        вызове получает `session` тем же путём, что путь без ступени вообще:
        назвать НЕЧЕГО отменять было бы неправдой, а безопасная сторона —
        назвать ближайшую ступень, а не промолчать.
        """
        head_rev = (
            git_ops.head_rev_from_files(self._launch_root)
            if self.mode == "git-mode" else None
        )
        committed = self._prepare_head_steps(path_keys, head_rev)
        targets: List[ResetTarget] = []
        for key in path_keys:
            try:
                step = self._head_step(key, head_rev, committed)
            except (OSError, TimeoutError):
                _logger.warning("ступень HEAD не определена: %s", key, exc_info=True)
                step = None
            if step is not None:
                target = RevertTarget(
                    key, STEP_HEAD, _head_state_label(step.commit_ref), None, (),
                )
            else:
                target = RevertTarget(key, "session", TARGET_SESSION_START, None, ())
            loses_point, accepted_ts = self._accepted_point(key)
            targets.append(ResetTarget(key, target, loses_point, accepted_ts))
        return targets

    def _accepted_point(self, path_key: str) -> Tuple[bool, Optional[float]]:
        """Точка отката к принятому состоянию: теряется ли она сбросом и когда
        её поставили (правило §4.3, единственная его редакция).

        Не теряет ничего путь с доказанно пустой дельтой от принятой базы
        (диск равен принятому — повторное ``a`` восстановит то же) и путь,
        который вообще не принят (сбрасывать нечего). Дельту сравнить не
        удалось (кап, бинарь) — безопасная сторона: как будто она непуста, а
        не как будто путь чист.

        Прочитать принятую базу не удалось (занятый лок индекса, отказ диска)
        — та же безопасная сторона и никакого исключения наружу: экран зовёт
        `reset_targets` прямо на нажатие `u`, вместе с самим сбросом.
        """
        try:
            accepted = self._store.accepted_state(path_key)
        except (OSError, TimeoutError):
            _logger.warning("принятая база не прочитана: %s", path_key, exc_info=True)
            return True, None
        if accepted is None:
            return False, None
        delta = delta_vs_disk(
            accepted.content,
            abs_path_for_key(self._launch_root, path_key),
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=self._hunk_context_lines,
        )
        return (not delta.countable or (delta.add, delta.del_) != (0, 0)), accepted.ts

    def revert(self, source: str, path_keys: Sequence[str]) -> RevertOutcome:
        """Вернуть пути к базе ЭТОГО представления (Р8) — ступени лестницы у «Сессии».

        В ``revert_paths`` уходит ТОЧНЫЙ снимок источника — счётная база сюда
        не попадает ни при каких условиях (контракт ``agent_edits.revert``).
        """
        view = self.session_view(source)
        outcome = revert_paths(
            list(path_keys),
            self._entries_for_revert(source, view),
            self._precise_baseline(source, view.turn_id),
            self._launch_root,
        )
        if outcome.restored or outcome.deleted:
            self.invalidate()
        return outcome

    def accept(self, path_keys: Sequence[str]) -> AcceptOutcome:
        """Принять пути: ``.accepted`` := диск, затем ``git add`` (Р26/Р27/Р29, пересмотр Р9).

        Принятие всегда двигает базу «Сессии» — источник не спрашивается
        (Р26): «Ход» принятую базу не видит вовсе. Кап размера снимка
        проверяется здесь же (слой снапшотов конфиг не читает): файл больше
        ``max_diff_source_bytes`` уходит в ``skipped``, а не обрезается.
        Отказ самого чтения (файл пропал или залочен внешним процессом между
        ``is_file()`` и ``read_bytes()`` — тот же гонка-риск, что гвардит
        ``deltas.delta_vs_disk``) — тоже в ``skipped``, а не исключение: как и
        у ``revert``, отказ метода — это часть ``Outcome``, не падение
        наружу; экран Задач 8–13 зовёт мутирующие методы без try/except
        (см. ``diff.py._accept``/``_apply_revert``).

        Порядок обязателен: сперва байты диска и капы, потом
        ``store.capture_accepted`` для каждого пути, и только ПОСЛЕ этого —
        best-effort ``git add`` одним батчем. Отказ стейджинга не откатывает
        уже сдвинутую базу — пользователь получил то, что просил («это
        разобрано»), индекс тут отдельная забота со своим фидбэком
        (``stage_error``).

        Путь, которого нет на диске, — не ошибка: в ``.accepted`` уходит
        явный маркер отсутствия (``content=None``, см.
        ``SnapshotStore.capture_accepted``) — легитимная точка отсчёта для
        файла, который агент удалил.

        Внешний ключ (файл вне проекта) в ``.accepted`` не попадает вовсе:
        гвард в начале цикла уводит его в ``skipped`` раньше, чем дело
        доходит до ``contents``/``capture_accepted`` — журнал МОЖЕТ хранить
        такие ключи (``agent_edits.journal``), но точки отсчёта у них не
        бывает (``agent_edits.paths.is_external_key``). ``git add`` поэтому
        получает только пути ВНУТРИ ``launch_root`` — иных среди ``accepted``
        к моменту стейджинга и не остаётся. Батч — один вызов на все rel
        сразу и включает пути, которых уже нет на диске: для уже
        отслеженного файла это стейджит удаление. Пути, которых нет ни на
        диске, ни у git, отсеиваются до вызова (``_stageable``): один такой
        путь ронял весь батч и не стейджил ничего.
        """
        contents: Dict[str, Optional[bytes]] = {}
        skipped: List[Tuple[str, str]] = []
        for key in path_keys:
            if is_external_key(key):
                # Сегодня сюда не доходит ни один путь экрана — `self._entries`
                # уже просеян `file_entries` (§3 спеки), — но метод обязан
                # держать гвард сам: вызывающий, минующий экран, не должен
                # получить право снять точный снимок чужого файла и назначить
                # ему точку отсчёта в каталоге проекта.
                skipped.append((key, ACCEPT_EXTERNAL))
                continue
            abs_path = abs_path_for_key(self._launch_root, key)
            if not abs_path.is_file():
                contents[key] = None      # маркер «принято отсутствие файла»
                continue
            try:
                if abs_path.stat().st_size > self._max_source_bytes:
                    skipped.append((key, ACCEPT_TOO_LARGE))
                    continue
                contents[key] = abs_path.read_bytes()
            except OSError:
                skipped.append((key, ACCEPT_READ_FAILED))

        candidates = list(contents.keys())
        is_git = self.mode == "git-mode"
        oid_rels = [key for key in candidates if not Path(key).is_absolute()]
        oids = (
            git_ops.head_blob_oids(self._launch_root, oid_rels)
            if oid_rels and is_git else {}
        )

        accepted: List[str] = []
        for key in candidates:
            try:
                self._store.capture_accepted(key, contents[key], head_oid=oids.get(key))
            except (OSError, TimeoutError):
                # Занятый `index.json.lock` (второй экземпляр приложения,
                # параллельный прогон), антивирусный лок, полный диск: отказ —
                # часть исхода, а не падение наружу (докстринг метода). Экран
                # зовёт `accept` без try/except нарочно.
                _logger.warning("принятая база не записана: %s", key, exc_info=True)
                skipped.append((key, ACCEPT_WRITE_FAILED))
                continue
            accepted.append(key)

        rels = [key for key in accepted if not Path(key).is_absolute()]
        staged = 0
        stage_error: Optional[str] = None
        if rels and is_git:
            stageable = self._stageable(rels)
            if stageable:
                code, err = git_ops.add_paths(self._launch_root, stageable)
                if code != 0:
                    _logger.warning("git add при принятии не выполнен (%s): %s",
                                     code, err.strip())
                    stage_error = err
                else:
                    staged = len(stageable)

        with_external: List[str] = []
        if accepted:
            # НЕ через `session_view`/`view`: принятые пути в тот же миг уходят
            # в разобранные (дельта от свежепринятой базы пуста) и выпали бы из
            # `.active` — а `external` смотрит только на цепочку sha записей
            # пути, к активности/разобранности отношения не имеет.
            records, _turn_id = self._records(SOURCE_SESSION)
            accepted_keys = set(accepted)
            scoped = [rec for rec in records if rec.path in accepted_keys]
            entries_by_path = {
                e.path: e for e in file_entries(
                    scoped, self._store.counting_session_baseline, self._launch_root,
                    max_source_bytes=self._max_source_bytes,
                    hunk_context_lines=self._hunk_context_lines,
                )
            }
            with_external = [
                key for key in accepted
                if key in entries_by_path and entries_by_path[key].external
            ]

        self.invalidate()
        return AcceptOutcome(accepted, staged, stage_error, skipped, with_external)

    def _stageable(self, rels: List[str]) -> List[str]:
        """Пути, которые ``git add`` вообще способен принять: есть на диске
        (обычная правка) ЛИБО известны git (тогда стейджится удаление).

        Без фильтра батч роняет один-единственный путь, которого нет ни там,
        ни там, — созданный и удалённый агентом файл попадает в ``Ctrl+A``, а
        `git add` отвечает ``fatal: pathspec … did not match any files`` и не
        стейджит НИЧЕГО (проверено на живом git: код 128, индекс не тронут).
        Один батч ``ls-files`` рядом с батчем oid'ов — та же цена запроса.
        """
        tracked = git_ops.ls_files_set(self._launch_root, rels)
        return [
            rel for rel in rels
            if rel in tracked or abs_path_for_key(self._launch_root, rel).exists()
        ]

    def reset_accept(self, path_keys: Sequence[str]) -> ResetOutcome:
        """Снять принятие (`u`): удалить ``.accepted`` (Р29).

        Счёт продолжается от ближайшей оставшейся ступени — от начала сессии,
        если ступени HEAD нет; ``to_head`` говорит вызывающему, какая из двух
        формулировок плашки правдива (§13), не пересчитывая то же самое ещё
        раз. Путь без принятой базы уходит в ``skipped``, а не молчаливый
        no-op: вызывающий обязан отличить «нечего было сбрасывать» от
        «сброшено».
        """
        reset: List[str] = []
        to_head: List[str] = []
        skipped: List[Tuple[str, str]] = []
        head_rev = (
            git_ops.head_rev_from_files(self._launch_root)
            if self.mode == "git-mode" else None
        )
        # Признак коммита — батчем, как в превью (`reset_targets`): он не зависит
        # от принятой базы, которую цикл ниже снимает, и посчитать его до цикла
        # честно.
        committed = self._prepare_head_steps(path_keys, head_rev)
        for key in path_keys:
            try:
                cleared = self._store.clear_accepted(key)
            except (OSError, TimeoutError):
                # Тот же класс отказов, что в `accept`: занятый лок индекса,
                # антивирус, полный диск — исход, а не исключение наружу.
                _logger.warning("принятая база не снята: %s", key, exc_info=True)
                skipped.append((key, RESET_WRITE_FAILED))
                continue
            if not cleared:
                skipped.append((key, RESET_NOT_ACCEPTED))
                continue
            reset.append(key)
            if self._head_step_lives(key, head_rev, committed):
                to_head.append(key)
        self.invalidate()
        return ResetOutcome(reset, to_head, skipped)

    def accepted_step_ts(self, path_key: str) -> Optional[float]:
        """Момент принятия — ТОЛЬКО когда ступень лестницы «Сессии» для этого
        пути сейчас разрешилась в `.accepted` (§3.2, задача 9). Не то же самое,
        что наличие `.accepted` вообще: тай-брейк Р28 мог отдать счёт более
        новому коммиту, и тогда файл считается от HEAD, а не от принятого —
        полная фраза шапки вьюера («счёт от принятого состояния», §13 спеки)
        обязана говорить правду о том, откуда СЧИТАЕТСЯ файл, а не только о
        том, что принятие когда-то было. ``ActiveRow.step`` (глиф `↺` в
        списке, задача 1 волны 4 смоука) несёт то же самое различие — оба
        смотрят на РАЗРЕШИВШУЮСЯ ступень, а не на факт принятия.

        Требует свежего ``self._session_ladder`` — прогревается
        ``session_view(SOURCE_SESSION)`` (см. ``_session_ladder_baseline``);
        источник «Ход» ступеней не имеет вовсе (Р27) — вызывающий обязан
        спрашивать это только для «Сессии».
        """
        step = self._session_ladder.get(path_key)
        return step.ts if step is not None and step.kind == STEP_ACCEPTED else None

    # --- внутреннее ---------------------------------------------------------

    def _records(self, source: str) -> Tuple[List[EditRecord], Optional[str]]:
        """Набор записей источника и его ``turn_id`` (у «Сессии» его нет).

        Неизвестный источник — ошибка, а не «ну пусть будет Сессия»: опечатка
        в вызове иначе покажет пользователю чужой набор файлов молча.
        """
        if source == SOURCE_TURN:
            turn_id = self._last_turn_id_with_local_edits()
            if not turn_id:
                return [], None
            return self._journal.turn_records(turn_id), turn_id
        if source != SOURCE_SESSION:
            raise ValueError(f"неизвестный источник правок: {source!r}")
        return self._journal.read_all(), None

    def _last_turn_id_with_local_edits(self) -> Optional[str]:
        """turn_id последнего хода, у которого есть хоть одна НЕ внешняя запись.

        ``EditsJournal.last_turn_id`` берёт turn_id самой последней записи
        журнала вообще. Если этой записью окажется внешняя (файл вне проекта
        — базы у неё нет, и `file_entries` её отбросит, §3 спеки), «Ход»
        выбрал бы ходом именно её: список файлов вышел бы пустым при
        непустом `turn_id`, и экран объявил бы правки хода отменёнными, хотя
        их никто не отменял (находка финального ревью ветки) — а предыдущий
        ход с настоящей правкой стал бы для этого источника недостижим.
        Читаем журнал сами и ищем последнюю запись, которая вообще способна
        дойти до экрана.
        """
        for rec in reversed(self._journal.read_all()):
            if not is_external_key(rec.path):
                return rec.turn_id
        return None

    def _counting_baseline(
        self, source: str, turn_id: Optional[str]
    ) -> Callable[[str], Optional[bytes]]:
        """База для СЧЁТА: более ранняя из точной и счётной."""
        if source == SOURCE_TURN:
            return lambda key: (
                self._store.counting_turn_baseline(key, turn_id) if turn_id else None
            )
        return self._store.counting_session_baseline

    def _precise_baseline(
        self, source: str, turn_id: Optional[str]
    ) -> Callable[[str], Optional[bytes]]:
        """База для ОТКАТА: точный снимок — ступень лестницы у «Сессии» (задача 9),
        снимок хода у «Хода» (без изменений, Р27 — «Ход» ступеней не видит)."""
        if source == SOURCE_TURN:
            return lambda key: (
                self._store.turn_baseline(key, turn_id) if turn_id else None
            )
        return self._session_precise_baseline

    def _revertable_for(
        self, source: str, turn_id: Optional[str]
    ) -> Callable[[str], bool]:
        """Есть ли у пути точный снимок — то есть доступен ли откат."""
        if source == SOURCE_TURN:
            return lambda key: bool(
                turn_id and self._store.has_turn_baseline(key, turn_id)
            )
        return self._store.has_session_baseline

    def _compute_session_steps(
        self,
        path_keys: List[str],
        accepted_by_path: Dict[str, Any],
        head_rev: Optional[str],
    ) -> Dict[str, Optional[SessionStep]]:
        """Ступень лестницы (§3.2) на путь — один расчёт на счёт, превью и откат
        «Сессии» (задача 9: ``session_view``/``file_diff``/``revert_delta``/
        ``revert``/``revert_targets`` читают один и тот же результат).
        """
        git_facts: Optional[_LadderGitFacts] = None
        committed: set = set()
        if head_rev is not None:
            rels = [key for key in path_keys if not Path(key).is_absolute()]
            tree = git_ops.head_tree(self._launch_root, rels) if rels else {}
            self._ladder_facts.prime(head_rev, tree or {})
            self._ladder_facts.note_head_tree(rels, tree)
            git_facts = self._ladder_facts
            committed, by_rev = self._committed_by_rev(rels, head_rev)
            self._ladder_facts.prefetch(by_rev)
        # Признак живёт столько же, сколько ступени: его читают и лестница,
        # и правило членства в группе (`_settled_reasons`).
        self._session_committed = committed

        steps: Dict[str, Optional[SessionStep]] = {}
        for key in path_keys:
            accepted = accepted_by_path[key]
            if accepted is None and key not in committed:
                continue    # ни принятой базы, ни коммита по пути — ступени не будет
            rel = None if Path(key).is_absolute() else key
            steps[key] = session_step(
                key, rel, accepted, self._step_snapshot(key), git_facts,
                committed_since_snapshot=key in committed,
            )
        return steps

    def _step_snapshot(self, path_key: str) -> Optional[bytes]:
        """Снимок начала сессии ДЛЯ ЛЕСТНИЦЫ — у созданного здесь пути ПУСТОТА (Д1).

        Признак «создан здесь» тут — отметка стора (``created_head_rev``), а не
        журнал: ``_head_step`` записей не видит вовсе, и второй источник того же
        признака рано или поздно разошёлся бы с первым. Физический ``.orig`` у
        созданного пути существовать МОЖЕТ — рекордер снимает его со ВТОРОЙ
        правки, — но он не «до»: подставить его значило бы сравнивать коммит с
        серединой набора (то же правило, что в ``deltas.file_entries``).
        """
        if self._store.created_head_rev(path_key) is not None:
            return b""
        return self._store.session_baseline(path_key)

    def _committed_since_snapshot(self, rels: List[str], head_rev: str) -> set:
        """Пути, которых коммит коснулся ПОСЛЕ съёма их ``.orig`` (или после
        создания) — признак ступени HEAD (§3.2 спеки волны 4).

        Признак — ЗАПИСАННАЯ ревизия HEAD, а не сравнение содержимого: «HEAD
        отличается от снимка начала сессии» одинаково верно и для файла,
        ГРЯЗНОГО ДО сессии, — там коммита не было вовсе, а ступень HEAD увела
        бы откат на содержимое коммита поверх несохранённой работы пользователя
        (см. докстринг ``agent_edits.ladder``). Отметки нет (снимок старого
        формата, HEAD не прочитан, снимка нет совсем) — пути в множестве нет,
        ступени HEAD не будет.

        У созданного здесь пути ревизия берётся из отметки создания и
        ПОБЕЖДАЕТ ``session_head_rev``: ``.orig`` у такого пути снят со второй
        правки и несёт уже пост-коммитную ревизию — по ней коммита «не было»
        (Д1).

        Ревизия совпала с текущей — HEAD с тех пор не двигался, коммитов не
        было: такие пути отсеиваются БЕЗ обращения к git, и в обычной сессии
        (никто ничего не коммитил) вызовов не остаётся ни одного.
        """
        return self._committed_by_rev(rels, head_rev)[0]

    def _committed_by_rev(
        self, rels: List[str], head_rev: str
    ) -> Tuple[set, Dict[str, List[str]]]:
        """``(закоммиченные, они же по ревизии снимка)`` — см. ``_committed_since_snapshot``."""
        by_rev: Dict[str, List[str]] = {}
        for rel in rels:
            rev = self._store.created_head_rev(rel)
            if rev is None:
                rev = self._store.session_head_rev(rel)
            if rev is not None and rev != head_rev:
                by_rev.setdefault(rev, []).append(rel)
        touched: set = set()
        committed_by_rev: Dict[str, List[str]] = {}
        for rev, paths in by_rev.items():
            hit = git_ops.paths_committed_since(self._launch_root, rev, paths)
            touched |= hit
            committed_by_rev[rev] = [rel for rel in paths if rel in hit]
        return touched, committed_by_rev

    def _prepare_head_steps(
        self, path_keys: Sequence[str], head_rev: Optional[str]
    ) -> Optional[set]:
        """Закоммиченные среди ``path_keys`` — одним батчем, с прогревом фактов лестницы.

        Для циклов, которые зовут ``_head_step`` на многих путях (цели отката
        «Хода», снятие принятия и его превью): по одному каждый путь спрашивал бы
        git о коммите и лестницу — о трёх фактах, в потоке UI.

        ``None`` — батч не собрался (замок индекса снимков ответил таймаутом,
        отказ файловой системы): цикл спросит каждый путь по одному, как до
        батча, со своей обработкой отказа на путь. Наружу отказ не уходит —
        экран на `u` и `r` не падает.
        """
        if head_rev is None:
            return set()
        rels = [key for key in path_keys if not Path(key).is_absolute()]
        try:
            committed, by_rev = self._committed_by_rev(rels, head_rev)
        except (OSError, TimeoutError):
            _logger.warning("ступени HEAD не собраны батчем — по одному", exc_info=True)
            return None
        # Сбой прогрева стоит только прогрева: множество уже верно, а без него
        # цикл пересчитал бы признак коммита по пути, снова дёргая индекс снимков.
        try:
            self._ladder_facts.prime(head_rev, {})
            self._ladder_facts.prefetch(by_rev)
        except (OSError, TimeoutError):
            _logger.warning("факты лестницы не прогреты — по одному", exc_info=True)
        return committed

    def _stepped(self, path_key: str) -> bool:
        """У пути есть ступень лестницы «Сессии» — `.accepted` или HEAD
        (§3.2 спеки). Единственный источник для всех трёх потребителей,
        которым нужно снять created-трактовку созданного здесь файла:
        `_build_session_view` (через `stepped=` у `deltas.file_entries`),
        `file_diff`, `revert_delta`. Требует свежего ``self._session_ladder``
        — прогревается ``session_view(SOURCE_SESSION)``; вне «Сессии» ступеней
        не бывает (Р27), и вызывающие обязаны гасить эту ветку сами по
        ``source == SOURCE_SESSION`` до вызова — здесь проверки источника нет
        нарочно, чтобы не разойтись с уже готовым `self._session_ladder`.
        """
        return self._session_ladder.get(path_key) is not None

    def _absent_step(self, path_key: str) -> bool:
        """Ступень пути — принятое ОТСУТСТВИЕ файла (`.accepted` с `content=None`).

        База такого пути не «отсутствует», а ПУСТА: агент пересоздал файл, и
        дельта считается от пустоты (kind `created`, §3.1) — иначе строка
        врала бы и видом, и числами (счётчики из журнала при живой ступени,
        находка финального ревью ветки, I2). На выбор БАЗЫ ОТКАТА это не
        влияет вовсе: там ступень остаётся принятым «файла быть не должно»
        (см. ``_entries_for_revert``), и `r` файл удаляет.
        """
        step = self._session_ladder.get(path_key)
        return step is not None and step.content is None

    def _session_ladder_baseline(self, key: str) -> Optional[bytes]:
        """База «Сессии» для СЧЁТА и превью: ступень лестницы, иначе счётная база
        (может оказаться `.gitorig` — годится ТОЛЬКО для чисел, не для записи
        на диск, см. докстринг модуля). Требует свежего ``self._session_ladder``
        — прогревается ``session_view(SOURCE_SESSION)``.
        """
        step = self._session_ladder.get(key)
        if step is not None:
            return step.content
        return self._store.counting_session_baseline(key)

    def _session_precise_baseline(self, key: str) -> Optional[bytes]:
        """База «Сессии» для ОТКАТА: та же ступень, а без неё — ТОЧНЫЙ снимок
        начала сессии, никогда счётная (контракт ``agent_edits.revert``).
        Требует свежего ``self._session_ladder`` — см. ``_session_ladder_baseline``.
        """
        step = self._session_ladder.get(key)
        if step is not None:
            return step.content
        return self._store.session_baseline(key)

    def _entries_for_revert(
        self, source: str, view: SessionView
    ) -> Dict[str, FileEntry]:
        """``entries_by_path`` для ``revert_paths`` — с поправкой на «принято
        отсутствие» (задача 9).

        Путь, чья ступень — принятый маркер «файла быть не должно»
        (``STEP_ACCEPTED`` с ``content is None``), обязан прийти в
        ``revert_paths`` как ``kind="created"``: это единственный сигнал,
        которым контракт отката (не меняется — см. докстринг
        ``agent_edits.revert``) просит УДАЛИТЬ файл, а не записать байты —
        записывать здесь и нечего, база — ``None``. Естественный ``entry.kind``
        для такого пути — ``modified``/``deleted`` (ступень уже занята,
        ``stepped_over_created`` снимает пометку ``created`` в ``file_entries``),
        поэтому подмена нужна именно здесь, а не в исходном расчёте.
        """
        entries = {row.entry.path: row.entry for row in view.active}
        if source != SOURCE_SESSION:
            return entries
        overridden = dict(entries)
        for key, entry in entries.items():
            step = self._session_ladder.get(key)
            if step is not None and step.content is None:
                overridden[key] = replace(entry, kind="created", revertable=True)
        return overridden

    def _build_session_view(
        self, records: List[EditRecord], head_rev: Optional[str]
    ) -> SessionView:
        """«Сессия»: активные — от лестницы отсчёта (§3.2), разобранные — §4.1."""
        path_keys = sorted({rec.path for rec in records})
        accepted_by_path = {key: self._store.accepted_state(key) for key in path_keys}

        # Ступень `.accepted` работает и без git (`accept` не требует git-mode
        # — см. test_accept_plain_folder_no_git_tail): без HEAD строится только
        # ветка 1 лестницы (git=None режет её внутри session_step), ветка
        # «принято» — нет. Батч oid'ов и прайм кэша — только когда HEAD есть.
        # Результат оседает в `self._session_ladder`: его читают ТАКЖЕ
        # `file_diff`/`revert_delta`/`revert`/`revert_targets` (задача 9) —
        # одна ступень на счёт, превью и запись.
        self._session_ladder = self._compute_session_steps(
            path_keys, accepted_by_path, head_rev,
        )

        entries = file_entries(
            records,
            self._session_ladder_baseline,
            self._launch_root,
            revertable_for=self._revertable_for(SOURCE_SESSION, None),
            stepped=self._stepped,
            absent_step=self._absent_step,
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=self._hunk_context_lines,
        )

        reasons = self._settled_reasons(path_keys, head_rev, accepted_by_path)
        active: List[ActiveRow] = []
        settled_records: List[EditRecord] = []
        for entry in entries:
            # «Правки не осталось» проверяется ПЕРВЫМ: это утверждение сильнее
            # любой причины разбора. Путь, вернувшийся к началу сессии, правило
            # членства всё равно поймает как «чист относительно HEAD» и подпишет
            # коммитом, которого не было.
            if self._is_undone(entry, accepted_by_path):
                continue
            if entry.path in reasons:
                settled_records.extend(_records_for_path(records, entry.path))
            else:
                active.append(
                    ActiveRow(
                        entry=entry,
                        step=_row_step(self._session_ladder.get(entry.path)),
                        has_accepted_base=accepted_by_path[entry.path] is not None,
                    )
                )

        return SessionView(
            source=SOURCE_SESSION,
            turn_id=None,
            active=active,
            settled=self._settled_rows(settled_records, reasons),
            total_add=sum(row.entry.add for row in active),
            total_del=sum(row.entry.del_ for row in active),
            cut_commands=_cut_commands(records),
        )

    def _build_turn_view(
        self, records: List[EditRecord], turn_id: Optional[str]
    ) -> SessionView:
        """«Ход»: без лестницы и без группы (Р27) — расчёт волны 3 в остальном
        без изменений.

        ``step`` — ВСЕГДА ``None`` (С1 смоука): лестница отсчёта — понятие
        «Сессии», «Ход» её не считает и не видит вовсе, а счёт здесь всегда
        идёт от начала хода независимо от того, принят путь или нет.
        ``has_accepted_base`` при этом читается и здесь (финальный фикс волны
        4, Р27-ревью владельца на смоуке): «Ход» не считает от принятой базы,
        но `u`/`Ctrl+U` остаются рабочими и тут — им нужен факт принятия, а
        не действующая ступень.
        """
        entries = file_entries(
            records,
            self._counting_baseline(SOURCE_TURN, turn_id),
            self._launch_root,
            revertable_for=self._revertable_for(SOURCE_TURN, turn_id),
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=self._hunk_context_lines,
        )
        # Пусто — значит нет строки, и в «Ходе» тоже: нулевая строка здесь
        # означает ровно то же, что в «Сессии» — работы этого хода по файлу не
        # осталось. Считать шум по-разному на двух экранах значило бы заставить
        # пользователя помнить, какой из них что показывает.
        precise = self._precise_baseline(SOURCE_TURN, turn_id)
        active: List[ActiveRow] = []
        for entry in entries:
            accepted_state = self._store.accepted_state(entry.path)
            accepted = accepted_state is not None
            # «Пусто — значит нет строки» действует и здесь, без исключений.
            # Исключение ради достижимости `u` тут стояло, пока пометка «принят»
            # говорила о ФАКТЕ принятия: строка объясняла себя сама. Пометка
            # переехала на текущее состояние, и после отката строка `+0 −0`
            # оставалась без неё — видимость про прошлое, пометка про
            # настоящее (пятый раунд ревью, находка 5).
            #
            # Принятая база от этого не теряется: в «Сессии» она и есть ступень
            # отсчёта, дельта от неё после отката не пуста, и путь там виден и
            # снимаем. А `Ctrl+U` уходит из «Хода» вместе со строкой —
            # `mass_reset` считается по видимым строкам, — так что молчаливого
            # действия по невидимому пути не будет.
            if self._nothing_left(entry, precise):
                continue
            active.append(ActiveRow(
                entry=entry, step=None, has_accepted_base=accepted,
                accepted_now=accepted and self._matches_accepted(entry.path, accepted_state),
            ))
        return SessionView(
            source=SOURCE_TURN,
            turn_id=turn_id,
            active=active,
            settled=[],
            total_add=sum(row.entry.add for row in active),
            total_del=sum(row.entry.del_ for row in active),
            cut_commands=_cut_commands(records),
        )

    def _matches_accepted(self, path_key: str, accepted: Any) -> bool:
        """Содержимое пути СЕЙЧАС совпадает с принятым.

        Ровно тот расчёт, которым правило (а) отправляет путь в группу
        «принято» — одно понятие, один предикат: «принят» значит «файл и есть
        то, что вы приняли», а не «когда-то принимали». Второе вечно и потому
        как подпись бесполезно: после новой правки оно всё равно утверждало бы
        принятость.
        """
        if accepted is None:
            return False
        delta = delta_vs_disk(
            accepted.content,
            abs_path_for_key(self._launch_root, path_key),
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=self._hunk_context_lines,
        )
        return delta.countable and (delta.add, delta.del_) == (0, 0)

    def _nothing_left(
        self, entry: FileEntry, baseline_for: Callable[[str], Optional[bytes]]
    ) -> bool:
        """Правки не осталось: откат этому пути ДОКАЗУЕМО ничего не сделал бы.

        Такой строке не место на экране изменений ни в одном из источников.
        Она мертва целиком: содержимого нет, `a` нечего принимать, `r` отвечает
        «отменять нечего». Единственное, что она сообщает, пользователь и так
        знает — он сам нажал `r` и получил врезку. Откатите десяток файлов, и
        десяток пустых строк вытеснит настоящие правки за край экрана.

        Скрытие сильнее любой подписи: ошибка здесь отнимает и вид на правку, и
        единственный способ ей управлять. Поэтому каждое условие закрывает свой
        способ соврать.

        * `kind == "created"` — не прячем: откат такому пути не пишет базу, а
          УДАЛЯЕТ файл (`revert._delete_created`). Совпадение со снимком тут
          ничего не значит: у созданного и правленного второй раз пути снимок —
          это содержимое ПОСЛЕ создания;
        * создан в наборе (`entry.created`) и его нет на диске — прячем, и
          ДО гейта точности: это исходное «файла нет», откату делать нечего, а
          считается ли путь (бинарь, файл сверх капа), тут неважно;
        * ноль по СЧЁТНОЙ дельте (`entry`) — той самой, которой экран рисует
          числа. Сверься мы только с точной базой, спряталась бы строка с
          ненулевыми `+/−`, и её числа исчезли бы из итогов. Заодно это дешёвый
          предфильтр: чтение снимка и диска ниже идёт только для кандидатов;
        * остальные пути — только если файл ЕСТЬ на диске. `delta_vs_disk`
          читает отсутствующий файл как пустой текст, поэтому у пути с базой
          `b""` удаление дало бы ноль — а откат его вернул бы.

        И только после этого — сверка с ТОЧНЫМ снимком, тем, которым откат
        пишет. Счётная база может совпадать с диском, пока точная не совпадает
        (`test_revert_delta_counts_from_precise_baseline_not_counting_base`).
        Точного снимка нет вовсе (правка только шелл-командой) — не прячем:
        не знаем.
        """
        key = entry.path
        if entry.kind == "created":
            return False
        abs_path = abs_path_for_key(self._launch_root, key)
        if entry.created and not abs_path.is_file():
            # Исходное «файла нет» — откату делать нечего (смок 2026-09-24:
            # после отката 10 000 созданных файлов висело 10 000 строк «удалён
            # +0 −0»). До гейта точности: у бинаря и файла сверх капа числа
            # неточные, и за гейтом такой путь сюда не доходил.
            return True
        if not (entry.exact and (entry.add, entry.del_) == (0, 0)):
            return False
        if not abs_path.is_file():
            return False
        baseline = baseline_for(key)
        if baseline is None:
            return False
        delta = delta_vs_disk(
            baseline,
            abs_path,
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=self._hunk_context_lines,
        )
        return delta.countable and (delta.add, delta.del_) == (0, 0)

    def _is_undone(self, entry: FileEntry, accepted_by_path: Dict[str, Any]) -> bool:
        """То же для «Сессии», но только у пути БЕЗ ступени и без принятия.

        Ступень различает породы, а нулевая дельта — нет: нулевая она у всех
        трёх. `None` — «начало сессии»: правка стёрта. `.accepted` — правка на
        диске жива и принята («принят»). HEAD — путь уехал в коммит, и это
        записанный факт.
        """
        key = entry.path
        if accepted_by_path[key] is not None or self._session_ladder.get(key) is not None:
            return False
        return self._nothing_left(entry, self._session_precise_baseline)

    def _settled_reasons(
        self,
        path_keys: List[str],
        head_rev: Optional[str],
        accepted_by_path: Dict[str, Any],
    ) -> Dict[str, str]:
        """Кто разобран (§4.1) и почему — членство и подпись причины РАЗДЕЛЬНО.

        Членство: (а) есть принятая база и дельта от неё пуста, ИЛИ (б) путь
        РЕАЛЬНО уехал в коммит за эту сессию и диск от коммита не отличается —
        известен git (``ls_files_set``) и не значится в ``git status``.
        «Известен git» обязателен: без него ignored-файлы, абсолютные ключи и
        путь-источник rename ложно свернулись бы в «коммитом» — они не в HEAD
        и не в porcelain.

        Правило (б) спрашивает ЗАПИСАННЫЙ факт коммита
        (``_session_committed``), а не совпадение содержимого с HEAD. Разница
        стоит данных пользователя: агент, записавший в файл ровно содержимое
        коммита, делает путь чистым относительно HEAD — и по старой форме тот
        уезжал в группу разобранного, где откат его ПРОПУСКАЕТ. Несохранённая
        работа пользователя, поверх которой агент писал, оказывалась с экрана
        недостижима, хотя точный снимок с ней цел.

        Цена признана сознательно, и это не редкость на путь, а ДВА КЛАССА
        целиком — группа у них не наполнится никогда:

        * репозиторий без коммитов на старте сессии (``git init`` и первая
          работа в нём): ``head_rev_from_files`` отдаёт ``None``, ревизию не
          получает ни один путь — и после первого коммита экран уже не
          успокоится;
        * шелл-команда, которая правит и коммитит за один заход: ``HEAD`` за
          команду сдвинулся, база берётся счётная, а ``capture_shell_session``
          ревизию не пишет вовсе.

        Плюс единичные пути: снимок старого формата, ``HEAD`` не читался при
        съёме. Все они остаются строкой и убираются нажатием ``a`` — принятие
        ревизии не требует. Обмен выбран осознанно: ложное «нет» стоит лишней
        строки, ложное «да» — несохранённой работы пользователя. Посеять
        ревизию задним числом (лениво, при первом показе) — отдельная задача:
        сев её «сейчас», мы бы навсегда замаскировали коммит, случившийся до
        посева.

        Подпись причины — ОТДЕЛЬНЫЙ вопрос, не «какое правило сработало»:
        «вручную» при живом ``accepted_state``, иначе «коммитом» (§4.2 спеки).
        Файл, принятый и затем закоммиченный поверх (диск ушёл дальше
        принятого через ЧУЖОЙ коммит — правило (а) уже не матчит, разобран по
        (б)), всё равно подписан «вручную»: у него должен остаться `u`.
        """
        settled: set = set()
        for key in path_keys:
            accepted = accepted_by_path[key]
            if accepted is None:
                continue
            delta = delta_vs_disk(
                accepted.content,
                abs_path_for_key(self._launch_root, key),
                max_source_bytes=self._max_source_bytes,
                hunk_context_lines=self._hunk_context_lines,
            )
            if delta.countable and (delta.add, delta.del_) == (0, 0):
                settled.add(key)

        if head_rev is not None:
            rels = [key for key in path_keys if not Path(key).is_absolute()]
            if rels:
                ls_files = git_ops.ls_files_set(self._launch_root, rels)
                # Отказ git ОТЛИЧАЕТСЯ от пустого снимка — так и задумано в
                # `status_entries` (её докстринг об этом прямо говорит). `or []`
                # схлопывал это различие: при отказе (занят index.lock, таймаут,
                # git не в PATH) пустой снимок означал бы «грязных файлов нет», и
                # ВСЕ отслеживаемые молча объявлялись бы закоммиченными — экран
                # показывал бы «изменений нет», а откат отказывался бы работать.
                # Безопасная сторона: не знаем — ступень не объявляем.
                status_rows = git_ops.status_entries(self._launch_root)
                if status_rows is not None:
                    status_paths = {path for path, _xy in status_rows}
                    for rel in rels:
                        if (
                            rel in self._session_committed
                            and rel in ls_files
                            and rel not in status_paths
                        ):
                            settled.add(rel)
                        elif (
                            rel in self._session_committed
                            and rel not in status_paths
                            and self._head_deleted(rel)
                            and not abs_path_for_key(self._launch_root, rel).exists()
                        ):
                            # Коммит удалил путь, и на диске его нет: закоммиченное
                            # «файла нет» и есть текущее состояние. `ls_files` такого
                            # пути не знает — правило выше его бы не поймало.
                            settled.add(rel)

        return {
            key: (SETTLED_MANUAL if accepted_by_path[key] is not None else SETTLED_COMMIT)
            for key in settled
        }

    def _head_deleted(self, path_key: str) -> bool:
        """Ступень пути — удаление, закоммиченное в этой сессии (``STEP_HEAD`` без байтов)."""
        step = self._session_ladder.get(path_key)
        return step is not None and step.kind == STEP_HEAD and step.content is None

    def _settled_rows(
        self, records: List[EditRecord], reasons: Dict[str, str]
    ) -> List[SettledRow]:
        """Кумулятив группы — ``file_entries`` от начала сессии (Р11), не сумма записей."""
        if not records:
            return []
        entries = file_entries(
            records,
            self._store.counting_session_baseline,
            self._launch_root,
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=self._hunk_context_lines,
        )
        return [
            SettledRow(
                path=e.path, add=e.add, del_=e.del_, kind=e.kind,
                reason=reasons[e.path], exact=e.exact,
            )
            for e in entries
        ]

    def _head_step(
        self, path_key: str, head_rev: Optional[str], committed: Optional[set] = None
    ) -> Optional[SessionStep]:
        """Ступень HEAD сама по себе (§3.2 волны 4, правило 1 ``agent_edits.ladder``)
        — НЕЗАВИСИМО от того, кто победил бы в арбитраже с ``.accepted``.

        ``ladder.session_step`` зовётся с ``accepted=None``: интересует только
        голое условие правила 1 (коммит отличен от снимка начала сессии), не
        итог борьбы со ступенью «принято» — используется и там, где принятой
        базы уже нет (``reset_accept`` зовёт ПОСЛЕ ``clear_accepted``), и там,
        где она есть, но неважна (предупреждение «Хода» про коммит — задача 9,
        ``revert_targets``: коммит после начала хода задевает откат «Хода»
        независимо от того, что там решила лестница «Сессии»). ``head_rev``
        вызывающий передаёт готовым — один ``rev-parse`` на весь батч, а не на
        каждый путь.

        ``committed`` — закоммиченные пути, уже посчитанные батчем
        (``_prepare_head_steps``) циклом, который зовёт этот метод на многих
        путях; ``None`` — признак спрашивается здесь, на один путь.
        """
        if head_rev is None:
            return None
        rel = None if Path(path_key).is_absolute() else path_key
        if rel is None:
            return None     # ключ вне репозитория — ступени HEAD нет по построению
        if committed is None:
            # Батч из одного пути: при неизменившемся HEAD (обычный случай)
            # git не зовётся вовсе.
            committed = self._committed_since_snapshot([rel], head_rev)
        # head_oid тай-брейка тут не нужен (accepted=None выше него режет
        # ветку) — батч пустой, лишнего `ls-tree` не заводим.
        self._ladder_facts.prime(head_rev, {})
        return session_step(
            path_key, rel, None, self._step_snapshot(path_key),
            self._ladder_facts, committed_since_snapshot=rel in committed,
        )

    def _head_step_lives(
        self, path_key: str, head_rev: Optional[str], committed: Optional[set] = None
    ) -> bool:
        """Ступень HEAD жива после сброса принятия — см. ``_head_step``.

        Никогда не бросает: это ПОДПИСЬ плашки, а не действие. Отказ чтения
        снимка или занятый лок индекса деградируют в «счёт снова с начала
        сессии» — текст беднее правды, но сам сброс уже состоялся, и ронять
        из-за подписи метод, который обещал отдавать отказ исходом, нельзя.
        """
        try:
            return self._head_step(path_key, head_rev, committed) is not None
        except (OSError, TimeoutError):
            _logger.warning("ступень HEAD не определена: %s", path_key, exc_info=True)
            return False
