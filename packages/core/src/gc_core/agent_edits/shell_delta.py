"""Шелл-делта: правки, сделанные shell-командой агента, в журнал правок.

Второй канал записи журнала (первый — ``recorder``, его зовут fs-тулы):
файлы правит и ``execute_command`` / ``run_python_code``, а такие правки ни
один тул не видит. Вокруг вызова команды снимается ``git status`` до и после,
разница в обе стороны кладётся в тот же журнал треда: появившееся/изменившееся
— ``op="shell"`` с хэнками, исчезнувшее — ``op="delete"``.

Канал живёт при живой thread-identity (``is_recording``) и не выключен ключом
``agent_edits.shell_tracking`` — этого достаточно для запуска объекта. Внутри
два подканала разной природы:

* карманный — снимает байты уже ИЗВЕСТНЫХ журналу файлов, кроме внешних
  (путь уже встречался в записях этого треда, а ключ не абсолютный —
  внешний путь карман отсеивает на слиянии потоков, см. ``_pocket_before``,
  и инвариант ``journal.py``: базы у такого ключа не бывает) до команды и
  сверяет их после. Работает в ОБОИХ режимах (и вне git тоже), про
  ``git status`` не знает вовсе и потому видит правки untracked/ignored
  путей, которые git-подканал не видит в принципе;
* git-подканал (см. остаток докстринга) — снимает ``git status`` до и после,
  работает только в git-режиме (``agent_edits.mode``) и только если снимок
  «до» вообще снялся.

Карманный подканал отрабатывает ПЕРВЫМ: путь, который он записал, git-
подканал повторно не трогает (дедуп по путям одной команды, не по журналу
целиком). Soft-fail в обоих: команда пользователя уже выполнена, любая
ошибка git/файлов логируется и глотается. Отказ git-подканала при снимке
«до» — не «пустой репозиторий», а отсутствие снимка: без него git-подканал
молчит, иначе вся чужая незакоммиченная грязь была бы записана как правки
этой команды.

Гигиена git-вызовов живёт в ``agent_edits.git_ops`` (без optional-локов, хуков
и ``core.fsmonitor``, всё под ``preserve_console_mode``); дифф добавляет к ней
отказ от внешних диффтулов и textconv — конфиг проверяемого репозитория не
должен исполнять свой код ни на одном нашем вызове.

База «до» у канала своей породы: команду не видел ни один тул, и поднимать
«до» приходится задним числом. Путь был ЧИСТ до команды (не значился в
снимке ``git status`` до неё) — тогда диск, индекс и ``HEAD`` ДО команды
совпадали. Если ``HEAD`` остался на месте или его сдвинули только свои
коммиты команды (гейт ``_own_commits``), «до» читается из ``HEAD`` до
команды в ТОЧНОЕ пространство (``.orig``/``.turn``), и такая правка
становится откатываемой. Пути, которые команда сама закоммитила, в status
не видны вовсе — их добирает разница деревьев ``HEAD`` до и после за тем же
гейтом (``_committed_paths``); они пишутся со строками, но без базы и без
отметки создания — без отката: коммит в окне команды мог быть чужим. Иначе (путь был грязен, неслит после
команды, ``HEAD`` сдвинут чужой историей — ``checkout``, ``pull``,
``reset`` — или у пути настроен кастомный фильтр) — «до» поднимается из
ИНДЕКСА git (``.gitorig``/``.gitturn``) и годится только для счётчиков:
откату такая база не подаётся никогда (см. докстринг
``agent_edits.snapshots``). Файл,
созданный самой командой, базы не получает вовсе: законность пустой базы
выражается записью ``op="create"``, а ревизию для ступени HEAD несёт отметка
создания в сторе (``SnapshotStore.mark_created``, ревизия ДО команды).
"""

from __future__ import annotations

import logging
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from gc_core.agent_edits import git_ops

# Приватные хелперы рекордера переиспользуются намеренно: периметр правки и
# thread-identity обязаны считаться в обоих каналах записи одинаково.
from gc_core.agent_edits.ctx import (
    agent_edits_config,
    identity_from_ctx,
    launch_root_from_ctx,
    limit,
    protected_extra_from_ctx,
)
from gc_core.agent_edits.delete_records import (
    DeleteBudget,
    delete_record,
    is_binary_bytes,
)
from gc_core.agent_edits.deltas import (
    abs_path_for_key,
    build_diff,
    decode_edit_text,
    truncate_hunks,
)
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.mode import resolve_mode
from gc_core.agent_edits.paths import (
    canonical_path_key,
    goes_through_link,
    is_external_key,
)
from gc_core.agent_edits.recorder import is_recording
from gc_core.agent_edits.snapshots import (
    SnapshotStore,
    snapshots_root,
    thread_key_from_journal_path,
)
from gc_core.config_modules.agent_edits import (
    DEFAULT_HUNK_CONTEXT_LINES,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_MAX_DIFF_SOURCE_BYTES,
    DEFAULT_SHELL_TRACKING,
    DEFAULT_SNAPSHOTS_MAX_FILES,
)
from gc_core.thread_store import edits_journal_path
from gc_core.tools.subagents.sidecar import current_turn_id
from gc_core.utils.file_text import (
    classify_file_content,
    sha256_hex_of_bytes,
    sha256_hex_of_file_bytes,
)

_logger = logging.getLogger(__name__)

# Потолок путей на одну команду — предохранитель, а не рабочий предел: запись
# идёт пачками (`_diffs_for`, `_base_facts`, `deferred_index`). Замер на Mac:
# 10 000 изменённых путей — 7 с, 2000 — 1,4 с; пофайловые запуски git стоили
# 35 с на 2000. Выше — случаи вроде переключения ветки с огромным диффом: пути
# сверх потолка не записываются, а записи урезанной команды несут настоящее
# число путей (`EditRecord.command_paths`), и экран изменений признаётся, что
# список неполон.
MAX_PATHS_PER_COMMAND = 10_000

# Кусок записи команды: столько путей за раз проходят пачку диффов, записей и
# баз. Кусок, а не вся команда разом, — чтобы память (диффы и блобы куска),
# время одного запуска git под общим таймаутом и цена отказа пачки были
# ограничены куском, а не числом путей. Пять сотен — несколько запусков git на
# кусок против сотен тысяч байт вывода: на запись 10 000 путей это ~100
# запусков вместо 30 000 пофайловых.
_RECORD_SLICE_PATHS = 500

# Режимы git обычного файла → режим файла записи удаления. Симлинк
# (``120000``) и подмодуль (``160000``) строк удаления не получают.
_REGULAR_FILE_MODES = {"100644": 0o644, "100755": 0o755}

# XY неслитого пути в ``git status`` (merge/rebase внутри команды): его «до»
# неоднозначно — точной базы и строк удаления он не получает (§5.1).
_UNMERGED_XY = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})

# Суммарный кап карманного снимка «до»: байты кандидатов держатся в памяти на
# одну команду. Кандидатов две группы — известные журналу (обычно единицы) и
# грязные пути рабочего дерева (их бывает сколько угодно), поэтому 32 МиБ — это
# не «с запасом на единицы», а потолок, в который набор упирается штатно.
# Константа модуля, не конфиг (см. §8 спеки волны 4).
POCKET_TOTAL_CAP_BYTES = 32 * 1024 * 1024

# Кап карманного снимка по ЧИСЛУ файлов. Байтового мало: цена здесь не в байтах,
# а в обращениях к диску — замер на Windows показал 668 мс на 800 файлах при
# полутора мегабайтах суммарно (50 файлов — 39 мс, 200 — 157 мс). Столько
# платит КАЖДАЯ shell-команда в долгом треде, ещё до своего запуска, — поэтому
# кап свой и маленький, а не общий с потолком записи (`MAX_PATHS_PER_COMMAND`):
# тот оплачивает только команда, которая сама тронула столько путей.
#
# Хвост отсекается по порядку кандидатов, а он смысловой, а не по недавности:
# впереди разошедшиеся с индексом (их вытеснение возвращает молчаливую потерю
# несохранённой работы — см. `_at_risk_paths`), за ними известные журналу
# (обычный путь подберёт git-подканал, игнорируемый — никто), последними —
# только проиндексированные: их содержимое лежит в объектах git.
POCKET_MAX_PATHS = 100

# Допуск при сравнении mtime файла со временем снимка «до»: у файловых систем
# Windows гранулярность записи времени доходит до ~15 мс, и файл, созданный
# сразу после снимка, может получить чуть более раннюю отметку. Допуск смещён в
# сторону «считать созданным» только в пределах этой погрешности.
_MTIME_TOLERANCE_NS = 20_000_000

# Дифф: без внешних диффтулов и textconv-фильтров репозитория (та же дыра) и без
# C-квотирования путей — заголовки хэнков из журнала потом рисует UI.
# Префиксы и запрет склейки переименований — ради диффа ПАЧКОЙ
# (`_diff_many`): секции режутся по заголовку `diff --git a/<путь> b/<путь>`,
# и он однозначен, только если обе половины совпадают. Пользовательский
# `diff.noprefix`/`diff.mnemonicPrefix` иначе сменил бы префиксы, а
# `diff.renames` склеил бы удалённый и созданный пути в одну секцию. Ответу
# пофайлового вызова это не меняет ничего: у одного пути пары для склейки нет.
_GIT_DIFF_ARGS = (
    "-c",
    "core.quotepath=false",
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)

# git спецкейсит именно "/dev/null" внутри --no-index — и на Windows тоже.
_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class _FileFacts:
    """Что запись пути знает о файле на диске — считается один раз (``_plan``)."""

    exists: bool
    is_file: bool
    binary: bool
    size: int


def _file_facts(path: Path) -> _FileFacts:
    exists = path.exists()
    return _FileFacts(
        exists=exists,
        is_file=exists and path.is_file(),
        binary=exists and _is_binary_file(path),
        size=_file_size(path) if exists else 0,
    )


@dataclass(frozen=True)
class _BaseFacts:
    """Ответы git для ``_capture_base``, собранные пачкой (``_base_facts``).

    ``filtered`` — пути точной ступени, которые меняет выкладка (им точная база
    не читается); ``head``/``index`` — сырые блобы HEAD и индекса, ``None`` —
    пути на ступени нет.
    """

    filtered: set[str]
    head: dict[str, bytes | None]
    index: dict[str, bytes | None]


def _diff_args(
    rel: str, *, untracked: bool, base_rev: str | None
) -> list[str]:
    """Аргументы пофайлового диффа пути — общие для ``_diff`` и параллельной пачки."""
    if untracked:
        # Для untracked обычный `git diff` пуст — сравниваем с /dev/null.
        # Магия pathspec здесь НЕ ставится: под --no-index git читает
        # аргументы как пути файловой системы и на ":(literal)…" отвечает
        # «Could not access» — а заодно и не нуждается в ней.
        return [*_GIT_DIFF_ARGS, "--no-index", "--", _DEV_NULL, rel]
    if base_rev:
        return [*_GIT_DIFF_ARGS, base_rev, "--", _literal(rel)]
    return [*_GIT_DIFF_ARGS, "--", _literal(rel)]


def _literal(rel: str) -> str:
    """Путь как ИМЯ ФАЙЛА, а не как pathspec-шаблон (см. ``git_ops.add_paths``).

    Без магии ``git diff -- 'f[ab].txt'`` отдаёт хэнки ещё и по ``fa.txt``:
    чужой файл лёг бы в запись журнала под этим путём — с чужим содержимым на
    экране и раздутыми счётчиками хода.
    """
    return f":(literal){rel}"


# Порода «команда добавила НОВЫЙ путь в индекс»: в ``HEAD`` его нет. Набор — по
# документации ``git status``: ``A`` в колонке индекса и ``[ MTD]`` в колонке
# дерева (``AT`` — сменился тип файла, путь всё равно новый). Неслитые коды
# (``AA``/``AU``) сюда не входят СОЗНАТЕЛЬНО — они тоже начинаются с ``A``, но
# путь под ними в ``HEAD`` есть (см. ``_created_by_command``).
_STAGED_ADD_XY = frozenset({"A ", "AM", "AT", "AD"})


def _loosened_ignore_scopes(
    launch_root: str,
    changed: list[str],
    before: dict[str, _StatusEntry],
    after: dict[str, _StatusEntry],
) -> list[str]:
    """Каталоги, где команда могла ОСЛАБИТЬ правила игнорирования: ``"sub/"``, ``""`` — весь проект.

    Правило ``.gitignore`` действует на свой каталог и всё ниже. Ослабить
    правила мог изменённый или удалённый ``.gitignore`` — и новый, только если
    в нём есть исключения ``!``: без них новый файл лишь добавляет
    игнорирование, и генерация проекта вместе с ``.gitignore`` не теряет
    отката созданных файлов. Новый файл не прочитался — считаем ослаблением.
    """
    scopes: list[str] = []
    for rel in changed:
        path = PurePosixPath(rel)
        if path.name != ".gitignore":
            continue
        scope = "" if str(path.parent) == "." else f"{path.parent}/"
        entry = after.get(rel)
        new = (
            rel not in before
            and entry is not None
            and (entry.xy == "??" or entry.xy in _STAGED_ADD_XY)
        )
        if new and not _has_negation(Path(launch_root) / rel):
            continue
        scopes.append(scope)
    return scopes


def _has_negation(path: Path) -> bool:
    """В файле правил есть исключение ``!…`` — или прочитать его не удалось."""
    try:
        text = path.read_bytes()
    except OSError:
        return True
    return any(line.lstrip().startswith(b"!") for line in text.splitlines())


class _GitFailed(RuntimeError):
    """git-вызов не удался. «Пусто» вместо снимка считать нельзя: пустой снимок
    «до» превращает всю чужую незакоммиченную грязь репозитория в правки этой
    команды."""


@dataclass(frozen=True)
class _StatusEntry:
    """Строка ``git status`` + подпись файла на диске.

    Кода status мало: повторная правка уже грязного файла его не меняет
    (``??`` остаётся ``??``), поэтому изменение ловим ещё и по size/mtime.
    """

    xy: str
    signature: tuple[int, int] | None


@dataclass(frozen=True)
class _PocketEntry:
    """Карманный снимок «до» одного файла набора: байты + быстрая подпись.

    ``before_signature`` — то же ``(size, mtime_ns)``, что и у ``_StatusEntry``:
    позволяет на «после» отсеять неизменившиеся кандидаты одним ``stat()``, не
    трогая байты вовсе.
    """

    abs_path: Path
    before_bytes: bytes
    before_signature: tuple[int, int]
    mode: int


class ShellDelta:
    """Правки shell-команды → записи в журнал правок: карманный снимок байтов
    известных файлов набора + снимки ``git status`` вокруг команды (два
    подканала, см. докстринг модуля)."""

    def __init__(
        self,
        launch_root: str,
        ctx: Any,
        seen: dict[str, tuple[str, tuple[int, int] | None]] | None = None,
        pocket_cache: dict[Path, tuple[tuple[int, int], list[str]]]
        | None = None,
        command_call_id: str | None = None,
    ) -> None:
        self._launch_root = launch_root
        self._ctx = ctx
        # Вызов команды — на каждую запись канала (`_edit_record`); "" → None.
        self._command_call_id = command_call_id or None
        self._seen = seen if seen is not None else {}
        self._pocket_cache = pocket_cache if pocket_cache is not None else {}
        self._before: dict[str, _StatusEntry] | None = None
        self._before_ts_ns = 0
        # Где команда могла ослабить правила игнорирования (`_loosened_ignore_scopes`).
        self._loosened_scopes: list[str] = []
        # Подпись `info/exclude` до команды: правило там в статусе не видно.
        self._info_exclude_before: object = None
        self._head_before: str | None = None
        self._head_after: str | None = None
        # Ответ гейта «свои коммиты» (`_own_commits`): считается раз на команду.
        self._own_commits_memo: bool | None = None
        # Пути, закоммиченные самой командой (`_committed_paths`).
        self._committed: frozenset[str] = frozenset()
        self._pocket: dict[str, _PocketEntry] = {}
        # Бюджет строк удалений на команду: общий для кармана и git-подканала;
        # создаётся заново в `after_and_record` (бюджет — на каждую команду).
        self._delete_budget = DeleteBudget(_max_diff_source_bytes(ctx))

    @classmethod
    def begin(
        cls,
        ctx: Any,
        *,
        seen: dict[str, tuple[str, tuple[int, int] | None]] | None = None,
        pocket_cache: dict[Path, tuple[tuple[int, int], list[str]]]
        | None = None,
        command_call_id: str | None = None,
    ) -> ShellDelta | None:
        """Снимок «до» команды. ``None`` — канал неактивен целиком, звать нечего.

        Контракт (Р33): объект живёт при ``is_recording ∧ shell_tracking`` —
        этого достаточно, карманный подканал работает и без git (cache-mode).
        Git-подканал — ДОПОЛНИТЕЛЬНОЕ условие внутри уже живого объекта:
        git-режим И снимок «до» git-статуса реально снялся; иначе объект
        всё равно возвращается, просто ``_before``/``_head_before`` остаются
        ``None`` (см. ``after_and_record``).

        ``seen``/``pocket_cache`` — карты дедупликации и кэша путей набора.
        Объект живёт одну команду, поэтому обе обязан держать вызывающий, на
        чём-то, что переживает команды (обёртка backend'а — одна на ctx). Без
        них дедупликации/кэша между командами нет.

        ``command_call_id`` — вызов тула, запустивший команду: ложится на
        каждую запись канала (``EditRecord.command_call_id``); пустой — ``None``.
        """
        try:
            if not is_recording(ctx) or not _shell_tracking_enabled(ctx):
                return None
            launch_root = launch_root_from_ctx(ctx)
            if not launch_root:
                return None
            delta = cls(
                launch_root,
                ctx,
                seen,
                pocket_cache,
                command_call_id=command_call_id,
            )
            # Порядок обязателен: карманный снимок берёт кандидатов из
            # git-статуса (пути, чьё «до» индекс представить не может), поэтому
            # статус снимается ПЕРВЫМ. Чтение файлов их не меняет, так что
            # отметка времени `_before_ts_ns` от перестановки не страдает.
            if resolve_mode(launch_root) == "git-mode":
                delta.before()
            delta._pocket_before()
            return delta
        except Exception:
            _logger.warning("shell delta: канал не запущен", exc_info=True)
            return None

    def before(self) -> None:
        """Снимок «до». При отказе git снимка НЕТ (не пустой словарь)."""
        try:
            # Отметка времени — ДО обхода status: файл, созданный командой,
            # обязан оказаться свежее её (см. признак создания в `_record`).
            self._before_ts_ns = time.time_ns()
            self._forget_stale()
            self._before = self._snapshot()
            self._head_before = git_ops.head_rev_from_files(self._launch_root)
            self._info_exclude_before = self._info_exclude_signature()
        except Exception:
            self._before = None
            self._head_before = None
            _logger.warning("shell delta: снимок «до» не снят", exc_info=True)

    def _forget_stale(self) -> None:
        """Забыть пути, которые между командами трогал кто-то ещё.

        Дедуп помнит, чем КОМАНДА кончила по пути, и живёт всю сессию: у
        неотслеживаемого файла дифф с /dev/null никогда не пуст, и повтор
        ловится только по хэшу. Но между командами файл меняют мимо канала —
        откат на экране изменений удаляет созданное, пользователь правит
        руками, — и память становится ложью: повторная команда пишет то же
        содержимое, запись отбрасывается как дубль, и правки нет ни в сводке
        хода, ни на экране (пятый раунд ревью, находка 4).

        Сверяется ПОДПИСЬ, а не содержимое: хэшировать каждый помнимый путь
        перед каждой командой дорого, а размер с временем правки ловят и
        удаление, и запись поверх. Подпись не сошлась — память о пути просто
        уходит, и следующая запись по нему будет честной.
        """
        for key in list(self._seen):
            _, signature = self._seen[key]
            if (
                _file_signature(abs_path_for_key(self._launch_root, key))
                != signature
            ):
                self._seen.pop(key, None)

    def _pocket_before(self) -> None:
        """Карманный снимок «до»: байты уже известных файлов набора.

        Кандидатов три группы, и порядок между ними значимый: сперва пути,
        разошедшиеся с индексом (``_at_risk_keys`` — их «до» не знает никто,
        кроме диска), затем известные журналу (``_pocket_known_paths``, в
        порядке недавности правки), последними — только проиндексированные (их
        содержимое лежит в объектах git). Берутся существующие на диске в пределах
        ``max_diff_source_bytes`` (пофайловый кап из конфига), суммарно не
        больше ``POCKET_TOTAL_CAP_BYTES``.

        Кандидат, не влезающий в ОСТАТОК бюджета, пропускается, а набор идёт
        дальше. Прежде здесь стоял обрыв, оправданный тем, что кандидаты шли
        строго по недавности и утрамбовывать бюджет более старыми файлами
        помельче значило бы предпочесть их недавним. Список стал смешанным, и
        обрыв означал другое: один большой грязный файл убивал весь остаток,
        включая известные журналу (пятый раунд ревью, находка 2).

        Никогда не бросает: карманный подканал обязан жить и вне git, и при
        любом отказе файловой системы — лог не должен уронить команду.
        """
        pocket: dict[str, _PocketEntry] = {}
        try:
            max_source = _max_diff_source_bytes(self._ctx)
            known = _pocket_known_paths(self._ctx, self._pocket_cache)
            # Фильтр стоит на СЛИЯНИИ потоков, а не внутри одного из них:
            # источника два (грязные пути `_at_risk_keys` и пути, известные
            # журналу), и закрыть один значит не закрыть ничего. Внешний путь
            # доходит сюда ОБОИМИ путями: из журнала — с тех пор, как
            # рекордер пишет о правках вне проекта, — и из `_at_risk_keys`,
            # когда грязный путь внутри репозитория резолвится наружу через
            # ссылку (`canonical_path_key` больше не режет внешние ключи,
            # только защищённые от отслеживания). Байты такого пути читать
            # нельзя: точная база дала бы право откатить чужой файл нажатием
            # `r`.
            diverged, staged_only = self._at_risk_keys()
            merged = _dedup(diverged + known + staged_only)
            candidates = [k for k in merged if not is_external_key(k)][
                :POCKET_MAX_PATHS
            ]
            total = 0
            for key in candidates:
                try:
                    abs_path = abs_path_for_key(self._launch_root, key)
                    st = abs_path.stat()
                    if not abs_path.is_file() or st.st_size > max_source:
                        continue
                    if total + st.st_size > POCKET_TOTAL_CAP_BYTES:
                        continue
                    data = abs_path.read_bytes()
                except OSError:
                    continue
                total += len(data)
                pocket[key] = _PocketEntry(
                    abs_path=abs_path,
                    before_bytes=data,
                    before_signature=(st.st_size, st.st_mtime_ns),
                    mode=stat.S_IMODE(st.st_mode),
                )
        except Exception:
            _logger.warning(
                "shell delta: карманный снимок «до» не снят", exc_info=True
            )
            pocket = {}
        self._pocket = pocket

    def _at_risk_paths(self) -> tuple[list[str], list[str]]:
        """Пути, чьё «до» ИНДЕКС ПРЕДСТАВИТЬ НЕ МОЖЕТ — снимаем их байтами.

        Счётная база шелл-канала читается из индекса git, а индекс не содержит
        несохранённых правок рабочего дерева. Для пути, который на момент
        команды отличается от индекса, такая база лжёт о «до»: команда вроде
        ``git restore``/``git checkout --``/``git reset --hard`` возвращает файл
        К ИНДЕКСУ, дифф выходит пустым, записи не появляется вовсе — и
        несохранённая работа пользователя исчезает молча и бесследно, без
        строки на экране и без всякой возможности откатить (найдено смоком
        2026-09-10 на живой команде ``git restore``).

        Признак — путь вообще есть в статусе «до», то есть отличается от HEAD.
        Неотслеживаемые (``??``) — потому что в индексе их нет вовсе;
        разошедшиеся с индексом — потому что он не знает правок дерева. И пути,
        отличающиеся ТОЛЬКО индексом (``M ``, ``A ``): индекс знает их
        содержимое, но команда вправе сбросить и его — ``git reset --hard``,
        ``git stash`` — и база, прочитанная из индекса ПОСЛЕ команды,
        совпадёт с диском: пустой дифф, ни записи, ни отката, а
        проиндексированная работа пропала бы молча. Чистые пути сюда не
        входят: их «до» — HEAD, и его команда не трогает без коммита.

        Отдаёт ДВЕ группы, и порядок между ними — часть контракта: карман
        обрезается потолком путей. Первыми — разошедшиеся с индексом (вторая
        колонка porcelain не пробел): их «до» не знает никто, кроме диска.
        Проиндексированные — последними, даже после известных журналу
        (``_pocket_before``): их содержимое хотя бы лежит в объектах git, а
        игнорируемый файл, который агент уже правил, другого источника «до» не
        имеет. Вперемешку, по алфавиту статуса, сотня проиндексированных файлов
        вытесняла бы за потолок и то и другое — молча.

        Снимок «до» не снялся (git отказал) — пустые группы: карманный
        подканал остаётся на прежнем наборе кандидатов.
        """
        if self._before is None:
            return [], []
        diverged = [
            rel for rel, entry in self._before.items() if entry.xy[1:2] != " "
        ]
        staged_only = [
            rel for rel, entry in self._before.items() if entry.xy[1:2] == " "
        ]
        return diverged, staged_only

    def _at_risk_keys(self) -> tuple[list[str], list[str]]:
        """Те же пути, но КЛЮЧАМИ ОТСЛЕЖИВАНИЯ.

        `_at_risk_paths` отвечает за критерий риска и отдаёт сырые строки
        porcelain; ключ считает тот же `canonical_path_key`, через который
        проходит git-подканал (`_build_record`). Защищённое от отслеживания
        он по-прежнему режет — такой путь выходит `None` и в `keys` не
        попадает. Но периметра из многих корней больше нет (задача 3): путь,
        который резолвится НАРУЖУ (грязный путь внутри репозитория — ссылка на
        каталог вне `launch_root`), эта функция честно отдаёт АБСОЛЮТНЫМ
        ключом, а не отбрасывает.

        От такого внешнего ключа сама `_at_risk_keys` не защищает — защищает
        фильтр на слиянии потоков (`_pocket_before`, `is_external_key`). Без
        него карман писал бы по такому пути не только запись в журнал, но и
        ТОЧНУЮ базу, а она даёт право на откат: `r` записал бы байты чужого
        файла, лежащего вне проекта (пятый раунд ревью, находка 1; фильтр на
        слиянии — задача 3).
        """
        protected = protected_extra_from_ctx(self._ctx)

        def keys_of(rels: list[str]) -> list[str]:
            keys = []
            for rel in rels:
                key = canonical_path_key(
                    str(self._abs(rel)),
                    self._launch_root,
                    protected_extra=protected,
                )
                if key is not None:
                    keys.append(key)
            return keys

        diverged, staged_only = self._at_risk_paths()
        return keys_of(diverged), keys_of(staged_only)

    def after_and_record(self) -> None:
        """Снимок «после» + записи в журнал. Никогда не бросает.

        Карманный подканал отрабатывает ПЕРВЫМ и всегда (живёт и без git);
        пути, которые он записал, git-подканал ниже пропускает — иначе одна
        правка легла бы в журнал дважды.
        """
        self._delete_budget = DeleteBudget(_max_diff_source_bytes(self._ctx))
        recorded = self._pocket_after_and_record()
        if self._before is None:
            # Git-подканал не активен (cache-mode либо снимок «до» не снялся):
            # сравнивать «после» не с чем — любая «разница» была бы чужой
            # грязью репозитория, а не правками команды.
            return
        try:
            after = self._snapshot()
            self._head_after = git_ops.head_rev_from_files(self._launch_root)
            self._record(self._before, after, skip=recorded)
        except Exception:
            _logger.warning(
                "shell delta: правки команды не записаны", exc_info=True
            )

    def _record(
        self,
        before: dict[str, _StatusEntry],
        after: dict[str, _StatusEntry],
        *,
        skip: frozenset[str] = frozenset(),
    ) -> None:
        changed, committed = self._changed_paths(before, after, skip)
        self._committed = committed
        if not changed:
            return
        # По ВСЕМ путям команды, до потолка: правило, ослабленное в `.gitignore`
        # за потолком записи, ослаблено всё равно.
        self._loosened_scopes = _loosened_ignore_scopes(
            self._launch_root, changed, before, after
        )
        command_paths = None
        if len(changed) > MAX_PATHS_PER_COMMAND:
            _logger.warning(
                "shell delta: команда затронула %s путей, пишем первые %s",
                len(changed),
                MAX_PATHS_PER_COMMAND,
            )
            command_paths = len(changed)
            changed = changed[:MAX_PATHS_PER_COMMAND]

        identity = identity_from_ctx(self._ctx)
        if identity is None:
            return
        journal_path = edits_journal_path(*identity)
        journal = EditsJournal(journal_path)
        turn_id = current_turn_id(self._ctx) or ""
        store = SnapshotStore(
            snapshots_root(self._launch_root),
            thread_key_from_journal_path(journal_path),
            Path(identity[0]),
        )
        # Команда пишется КУСКАМИ по `_RECORD_SLICE_PATHS` путей; всё, что цикл
        # спросил бы у git по пути, кусок спрашивает пачками — диффы, атрибуты,
        # прежние версии, — а отметки индекса снимков копит `deferred_index`.
        # Пофайловые запуски делали команду, тронувшую 2000 файлов, делом 35 с,
        # пока агент ждёт.
        wrote_base = False
        with store.deferred_index():
            for start in range(0, len(changed), _RECORD_SLICE_PATHS):
                wrote_base |= self._record_slice(
                    changed[start : start + _RECORD_SLICE_PATHS],
                    before,
                    after,
                    journal,
                    store,
                    turn_id,
                    command_paths,
                )

        # Эвикция — только к факту записи новой базы (она обходит каталог целиком)
        # и строго ПОСЛЕ записи журнала: живость треда evict_lru проверяет по
        # наличию edits_<thread_key>.jsonl, и до первого append свои же свежие
        # снимки оказались бы кандидатами на вытеснение.
        if wrote_base:
            store.evict_lru(_snapshots_max_files(self._ctx))

    def _changed_paths(
        self,
        before: dict[str, _StatusEntry],
        after: dict[str, _StatusEntry],
        skip: frozenset[str],
    ) -> tuple[list[str], frozenset[str]]:
        """Пути команды (и закоммиченные ею): сперва разница снимков status, за ней — закоммиченные ею.

        Порядок — часть контракта: потолок путей (``MAX_PATHS_PER_COMMAND``)
        режет хвост, и закоммиченным достаётся только остаток. Пути, уже
        записанные карманом (``skip``), из обеих групп вычитаются.
        """
        status_changed = sorted(
            (
                {
                    path
                    for path, entry in after.items()
                    if before.get(path) != entry
                }
                | {path for path in before if path not in after}
            )
            - skip
        )
        committed = [
            rel
            for rel in self._committed_paths(before, after)
            if rel not in skip
        ]
        return status_changed + committed, frozenset(committed)

    def _committed_paths(
        self,
        before: dict[str, _StatusEntry],
        after: dict[str, _StatusEntry],
    ) -> list[str]:
        """Пути, которые команда поправила, создала или удалила и сама закоммитила (§5.3a).

        В status такой путь не значится ни до, ни после команды. Множество —
        разница деревьев HEAD до и после команды минус пути, грязные до неё
        (их ловит карман, либо они честно вернулись к индексу), минус пути из
        status после (их пишет обычная ветка). Только за гейтом
        ``_own_commits``: иначе ``git pull`` или переключение ветки дали бы
        тысячи «правок агента». Отказ git — пусто.
        """
        if not self._own_commits():
            return []
        changed = git_ops.paths_changed_between(
            self._launch_root, self._head_before, self._head_after
        )
        if changed is None:
            return []
        return sorted(changed - before.keys() - after.keys())

    def _own_commits(self) -> bool:
        """HEAD за команду сдвинули только коммиты ЭТОЙ команды (гейт §5.3a).

        Одновременно: HEAD сдвинулся, HEAD до команды — предок нового, и у
        всех коммитов между ними время коммиттера не раньше начала команды.
        pull, merge и checkout ветки-потомка приносят коммиты со старыми
        датами; у reset, amend, rebase и checkout чужой ветки старый HEAD не
        предок. Считается раз на команду.
        """
        if self._own_commits_memo is None:
            self._own_commits_memo = self._check_own_commits()
        return self._own_commits_memo

    def _check_own_commits(self) -> bool:
        before, after = self._head_before, self._head_after
        if before is None or after is None or before == after:
            return False
        if not git_ops.is_ancestor(self._launch_root, before, after):
            return False
        times = git_ops.commit_times(self._launch_root, before, after)
        start = self._before_ts_ns // 1_000_000_000
        return times is not None and all(ts >= start for ts in times)

    def _head_before_trusted(self) -> bool:
        """HEAD до команды — честное «до» чистого пути.

        Так, когда HEAD остался на месте или его сдвинули только свои коммиты
        команды (``_own_commits``). После чужого сдвига
        (``git checkout other && sed -i …``) «до» от старого HEAD показало бы
        разницу веток правкой агента, а откат записал бы старую ветку поверх
        новой: тогда дифф идёт от индекса, база счётная, удаление без строк.
        """
        return self._head_before is not None and (
            self._head_before == self._head_after or self._own_commits()
        )

    def _record_slice(
        self,
        part: list[str],
        before: dict[str, _StatusEntry],
        after: dict[str, _StatusEntry],
        journal: EditsJournal,
        store: SnapshotStore,
        turn_id: str,
        command_paths: int | None,
    ) -> bool:
        """Записи куска путей команды и их базы. ``True`` — записана хоть одна новая база.

        ``command_paths`` — настоящее число путей урезанной потолком команды:
        ложится на КАЖДУЮ её запись (какая бы ни осталась в виде экрана, признак
        неполноты не потеряется).

        Закоммиченный командой путь (``_committed``) пишется со строками от
        HEAD до команды, но без базы и без отметки создания — откатить его
        нельзя: коммит в окне команды мог сделать кто-то ещё (параллельный
        субагент, пользователь), гейт ``_own_commits`` его не отличит, и
        откат затёр бы чужую закоммиченную работу.
        """
        planned = self._plan(part)
        diffs = self._diffs_for(planned, before, after)
        created_rels = self._created_rels(planned, before, after)
        deleted = self._slice_deleted_contents(planned, before, after)
        needs_base: list[tuple[str, str, bool]] = []
        delete_bases: list[tuple[str, str, bytes]] = []
        for rel, key, file in planned:
            entry = after.get(rel)
            clean_before = rel not in before
            deleted_before = deleted.get(rel)
            try:
                record = self._build_record(
                    rel,
                    entry,
                    turn_id,
                    created=rel in created_rels,
                    clean_before=clean_before,
                    hunks=diffs.get(rel),
                    key=key,
                    file=file,
                    deleted=deleted_before,
                    committed=rel in self._committed,
                )
            except Exception:
                _logger.warning(
                    "shell delta: путь пропущен rel=%r", rel, exc_info=True
                )
                continue
            if record is None:
                continue
            record.command_paths = command_paths
            record.command_call_id = self._command_call_id
            journal.append(record)
            self._remember(record)
            if record.op == "create":
                self._mark_created(store, rel, record.path)
            elif not record.hunks or rel in self._committed:
                # Закоммиченный командой путь создания не бывает (в status
                # после его нет, ``_created_by_command``) — и базы не пишется.
                continue
            elif record.op == "shell":
                exact = self._exact_step(clean_before, _xy(entry))
                needs_base.append((rel, record.path, exact))
            elif record.op == "delete" and deleted_before:
                # Строки у удаления только с известным точным «до» — блобом
                # HEAD до команды (``_deleted_contents``): он и есть база.
                delete_bases.append((rel, record.path, deleted_before[0]))

        try:
            facts: _BaseFacts | None = self._base_facts(needs_base)
        except Exception:
            # Пачка не собралась — базы куска снимаются по одной, как до пачек:
            # отказ стоит скорости, а не права на откат.
            _logger.warning(
                "shell delta: пакетный сбор баз не удался, снимаем по одной",
                exc_info=True,
            )
            facts = None
        wrote_base = False
        for rel, key, exact in needs_base:
            wrote_base |= self._capture_base(
                store,
                rel,
                key,
                turn_id,
                exact=exact,
                facts=facts,
            )
        for rel, key, content in delete_bases:
            wrote_base |= self._capture_delete_base(
                store, rel, key, content, turn_id
            )
        return wrote_base

    def _mark_created(self, store: SnapshotStore, rel: str, key: str) -> None:
        """Отметка создания пути — ступень HEAD для пути без базы.

        Базы у созданного пути нет и быть не должно, но ступень HEAD ему
        нужна (Д1): ревизию несёт отметка создания. Берём ту, что была ДО
        команды, даже если команда сама коммитила, — признак отвечает за
        «коммит ПОСЛЕ создания» (§3.2).

        Свой try — как у соседей по циклу (`_build_record`, `_capture_base`):
        `file_lock` индекса при конкуренции (сабагенты, swarm в одном
        проекте) отвечает таймаутом, и улети он во внешний except — пути,
        оставшиеся в наборе, не попали бы в журнал НИКОГДА. Потеря отметки
        стоит ступени HEAD у одного пути, потеря записей — всего остатка
        команды.
        """
        try:
            store.mark_created(key, head_rev=self._head_before)
        except Exception:
            _logger.warning(
                "shell delta: отметка создания не записана rel=%r",
                rel,
                exc_info=True,
            )

    def _slice_deleted_contents(
        self,
        planned: list[tuple[str, str, _FileFacts]],
        before: dict[str, _StatusEntry],
        after: dict[str, _StatusEntry],
    ) -> dict[str, tuple[bytes, int]]:
        """``_deleted_contents`` для пропавших путей куска, чистых до команды.

        Неслитый после команды путь (``_UNMERGED_XY``) и путь после чужого
        сдвига HEAD (``_head_before_trusted``) содержимого не получают:
        удаление без строк и без базы (фильтр — ``_exact_step``). Отказ git
        стоит строк удаления, а не записей: пути пишутся без строк.
        """
        rels = [
            rel
            for rel, _key, file in planned
            if not file.exists
            and self._exact_step(rel not in before, _xy(after.get(rel)))
        ]
        try:
            return self._deleted_contents(rels)
        except Exception:
            _logger.warning(
                "shell delta: содержимое удалённых путей не прочитано",
                exc_info=True,
            )
            return {}

    def _deleted_contents(
        self, rels: list[str]
    ) -> dict[str, tuple[bytes, int]]:
        """``{путь: (байты «до», режим)}`` удалённых командой чистых путей.

        Путь чист до команды — его «до» есть блоб ``HEAD`` до команды (Р32);
        честность такого «до» отбирает вызывающий (``_exact_step``). Только
        обычные файлы (``100644``/``100755``): блоб симлинка — текст
        цели, откат поставил бы на место ссылки обычный файл. Гвард
        кастомного фильтра — ДО чтения содержимого: конфиг чужого репозитория
        не должен исполнять свой код. Режимы и размеры — одним ``ls-tree``,
        бинарность — одним ``diff --numstat`` (``_binary_deletions``); бюджет
        команды резервируется по размерам по сортированным небинарным путям
        ДО чтения блобов (карман его уже потратил первым), так что читаются
        только влезшие. Прочитанное, что всё же не текст, бюджет возвращает.
        Концы строк — форма фильтра: диска для «родной» формы нет.
        """
        if self._head_before is None or not rels:
            return {}
        entries = git_ops.tree_entries(
            self._launch_root, rels, rev=self._head_before
        )
        if not entries:
            return {}
        max_source = _max_diff_source_bytes(self._ctx)
        sizes = {
            rel: entry.size
            for rel, entry in entries.items()
            if entry.mode in _REGULAR_FILE_MODES
            and entry.size is not None
            and entry.size <= max_source
        }
        skipped = git_ops.custom_filter_paths(self._launch_root, list(sizes))
        skipped |= self._binary_deletions(list(sizes))
        reserved = [
            rel
            for rel in sorted(sizes)
            if rel not in skipped and self._delete_budget.take(sizes[rel])
        ]
        blobs = git_ops.blobs_filtered_at(
            self._launch_root, reserved, rev=self._head_before
        )
        contents: dict[str, tuple[bytes, int]] = {}
        for rel in reserved:
            blob = blobs.get(rel)
            if blob is None or not _is_text(blob):
                self._delete_budget.refund(sizes[rel])
                continue
            contents[rel] = (blob, _REGULAR_FILE_MODES[entries[rel].mode])
        return contents

    def _binary_deletions(self, rels: list[str]) -> set[str]:
        """Бинарные из удалённых путей — до чтения содержимого: бюджет строк
        на них не резервируется. Отказ git — пусто: бинарь тогда отсеется
        уже после чтения, с возвратом бюджета."""
        binary = git_ops.binary_paths_at(
            self._launch_root, rels, rev=self._head_before
        )
        return binary or set()

    def _capture_delete_base(
        self,
        store: SnapshotStore,
        rel: str,
        key: str,
        content: bytes,
        turn_id: str,
    ) -> bool:
        """Точная база удалённого пути — его блоб ``HEAD`` до команды.

        Та же точная ступень, что у ``_capture_base``: путь был чист, «до» —
        ``HEAD`` до команды без кастомного фильтра. Soft-fail.
        """
        try:
            wrote = store.capture_session(
                key, content, head_rev=self._head_before
            )
            if turn_id:
                store.capture_turn(key, content, turn_id)
            return wrote
        except Exception:
            _logger.warning(
                "shell delta: база удаления не снята rel=%r",
                rel,
                exc_info=True,
            )
            return False

    def _plan(self, part: list[str]) -> list[tuple[str, str, _FileFacts]]:
        """Пути куска, которые канал пишет, — с ключом и фактами о файле, ОДИН раз.

        Отсев защищённых (инструкции агента, его рабочие каталоги) и внешних —
        первым делом, до диффа: иначе их содержимое уходило бы в вывод git и в
        память и выбрасывалось бы только потом. Факты о файле (бинарь, размер)
        нужны и пачке диффов, и записи — второй обход открывал бы каждый файл ещё
        раз.

        Путь, идущий через симлинк в любом звене, пропускается целиком. Ключ
        раскрывает ссылку до цели, а дифф и база git описывают путь из индекса:
        `ln -sfn b.txt link` записал бы «правку» `b.txt` с базой-строкой `a.txt`;
        висячая ссылка получила бы базой текст прежней цели; а после
        `rm -rf d && ln -s other d` отслеживаемый `d/a.txt` привёл бы в
        `other/a.txt` — и откат затёр бы чужой файл базой из `d/a.txt`. Цель,
        если её правили, видит карманный подканал — он читает её по ключу.

        Проверяется каждое звено пути (``paths.goes_through_link``), а не совпадение
        ключа с путём из ``git status``: на Windows ``resolve()`` отдаёт регистр
        с диска, а git — из индекса, и сравнение выкидывало бы все правки файла,
        чей регистр разошёлся (обычное дело при core.ignorecase).
        """
        extra = protected_extra_from_ctx(self._ctx)
        planned: list[tuple[str, str, _FileFacts]] = []
        for rel in part:
            abs_path = self._abs(rel)
            key = canonical_path_key(
                str(abs_path), self._launch_root, protected_extra=extra
            )
            if (
                key is None
                or is_external_key(key)
                or goes_through_link(self._launch_root, rel)
            ):
                continue
            planned.append((rel, key, _file_facts(abs_path)))
        return planned

    def _diffs_for(
        self,
        planned: list[tuple[str, str, _FileFacts]],
        before: dict[str, _StatusEntry],
        after: dict[str, _StatusEntry],
    ) -> dict[str, str]:
        """Диффы путей куска пачкой — то, что ``_build_record`` спросил бы по одному.

        Пропускаются пути, которым дифф не нужен (пропали, бинарь, сверх
        ``max_diff_source_bytes``): в пачке их содержимое ушло бы в дифф целиком
        — ровно то, от чего пофайловый путь их бережёт. Путь без ответа
        ``_build_record`` добирает сам. Закоммиченный командой путь
        (``_committed``) в status после нет, но дифф ему нужен — от HEAD до
        команды, в той же группе, что чистые пути из status.
        """
        max_source = _max_diff_source_bytes(self._ctx)
        untracked: list[str] = []
        by_rev: dict[str | None, list[str]] = {}
        for rel, _key, file in planned:
            entry = after.get(rel)
            if (
                (entry is None and rel not in self._committed)
                or not file.is_file
                or file.binary
                or file.size > max_source
            ):
                continue
            if _xy(entry) == "??":
                untracked.append(rel)
            else:
                by_rev.setdefault(
                    self._diff_base_rev(rel not in before), []
                ).append(rel)
        diffs = self._untracked_diffs(untracked)
        for rev, rels in by_rev.items():
            diffs.update(self._diff_many(rels, base_rev=rev))
        return diffs

    def _base_facts(
        self, needs_base: list[tuple[str, str, bool]]
    ) -> _BaseFacts:
        """Ответы git для ``_capture_base`` по всем путям сразу — тремя запусками.

        Точная ступень (признак ``exact``, ``_exact_step``): сперва атрибуты
        — гвард фильтра обязан идти ПЕРЕД чтением содержимого, — затем блобы
        HEAD до команды без фильтра. Всем остальным и тем, кого HEAD не
        прочитал, — блобы индекса для счётной базы.
        """
        exact = [rel for rel, _key, ok in needs_base if ok]
        filtered = git_ops.custom_filter_paths(self._launch_root, exact)
        head = git_ops.blobs_at(
            self._launch_root,
            [rel for rel in exact if rel not in filtered],
            rev=self._head_before,
        )
        index_rels = [
            rel for rel, _key, _ok in needs_base if head.get(rel) is None
        ]
        index = git_ops.blobs_at(self._launch_root, index_rels, rev="")
        return _BaseFacts(filtered=filtered, head=head, index=index)

    def _exact_step(self, clean_before: bool, xy: str | None) -> bool:
        """Годится ли пути точная база из HEAD до команды (Р32, §5.3).

        Путь чист до команды ⇒ диск = индекс = HEAD ДО команды. Честное «до»
        это, когда HEAD остался на месте или его сдвинули свои коммиты
        команды (``_head_before_trusted``). Неслитый после команды путь
        (``xy`` из ``_UNMERGED_XY``) остаётся на счётной базе; ``xy=None`` —
        пути нет в status после команды.
        """
        return (
            clean_before
            and xy not in _UNMERGED_XY
            and self._head_before_trusted()
        )

    def _pocket_after_and_record(self) -> frozenset[str]:
        """Карманные записи «после»: изменившимся — точная запись, пропавшим —
        удаление с сохранённой базой. Возвращает пути, которые записала ЭТА
        функция — их обязан пропустить git-подканал (дедуп, см. ``_record``).

        Никогда не бросает: soft-fail, как весь канал.
        """
        if not self._pocket:
            return frozenset()
        try:
            return self._record_pocket()
        except Exception:
            _logger.warning(
                "shell delta: карманные правки не записаны", exc_info=True
            )
            return frozenset()

    def _record_pocket(self) -> frozenset[str]:
        identity = identity_from_ctx(self._ctx)
        if identity is None:
            return frozenset()
        journal_path = edits_journal_path(*identity)
        journal = EditsJournal(journal_path)
        turn_id = current_turn_id(self._ctx) or ""
        store = SnapshotStore(
            snapshots_root(self._launch_root),
            thread_key_from_journal_path(journal_path),
            Path(identity[0]),
        )
        recorded: set[str] = set()
        wrote_base = False
        for key, entry in self._pocket.items():
            # Весь блок пути — под ОДНИМ try: сбой на записи журнала/базы
            # (после того как `_pocket_record` уже вернул готовую запись) не
            # должен откатывать `recorded` для путей, уже дописанных раньше в
            # этом же цикле — иначе `_pocket_after_and_record` пробросит
            # исключение наружу, внешний except обнулит весь frozenset, и
            # git-подканал повторно запишет то, что карманный уже сохранил.
            try:
                record, base_bytes = self._pocket_record(key, entry, turn_id)
                if record is None:
                    continue
                record.command_call_id = self._command_call_id
                journal.append(record)
                self._remember(record)
                recorded.add(key)
                if base_bytes is not None:
                    wrote_base |= self._pocket_capture_base(
                        store, key, base_bytes, turn_id
                    )
            except Exception:
                _logger.warning(
                    "shell delta: карманный путь пропущен key=%r",
                    key,
                    exc_info=True,
                )
                continue

        if wrote_base:
            store.evict_lru(_snapshots_max_files(self._ctx))
        return frozenset(recorded)

    def _pocket_record(
        self, key: str, entry: _PocketEntry, turn_id: str
    ) -> tuple[EditRecord | None, bytes | None]:
        """Одна карманная запись «после».

        Возвращает ``(запись или None, байты базы для ``_pocket_capture_base``
        или None)``. Байты базы отдаются и для ``op="delete"`` — карманный
        подканал уже держит точные байты «до» в памяти, второй (git) запрос
        для этого не нужен (git-подканал читает «до» удалённого пути из
        блоба ``HEAD`` до команды, ``_deleted_contents``).
        """
        abs_path = entry.abs_path
        before_sha256 = sha256_hex_of_bytes(entry.before_bytes)
        try:
            st = abs_path.stat()
            exists_as_file = abs_path.is_file()
        except OSError:
            st = None
            exists_as_file = False

        if st is None or not exists_as_file:
            record = delete_record(
                key,
                turn_id,
                entry.before_bytes,
                mode=entry.mode,
                budget=self._delete_budget,
                context_lines=_hunk_context_lines(self._ctx),
                max_chars=_max_diff_chars(self._ctx),
            )
            return record, entry.before_bytes

        if (st.st_size, st.st_mtime_ns) == entry.before_signature:
            return None, None  # быстрый скрин: не менялось

        max_source = _max_diff_source_bytes(self._ctx)
        if st.st_size > max_source:
            after_sha256 = _sha256_of_file(abs_path)
            record = _edit_record(
                key,
                turn_id,
                op="shell",
                before_sha256=before_sha256,
                after_sha256=after_sha256,
                flags=["no_diff"],
                size_bytes=st.st_size,
            )
            return record, None

        try:
            after_bytes = abs_path.read_bytes()
        except OSError:
            return None, None
        if after_bytes == entry.before_bytes:
            return (
                None,
                None,
            )  # изменился только mtime (та же дыра, что у git-подканала)

        if is_binary_bytes(entry.before_bytes) or is_binary_bytes(after_bytes):
            record = _edit_record(
                key,
                turn_id,
                op="shell",
                before_sha256=before_sha256,
                after_sha256=sha256_hex_of_bytes(after_bytes),
                flags=["no_diff"],
                size_bytes=len(after_bytes),
            )
            return record, None

        old_text = decode_edit_text(entry.before_bytes)
        new_text = decode_edit_text(after_bytes)
        if old_text is None or new_text is None:
            record = _edit_record(
                key,
                turn_id,
                op="shell",
                before_sha256=before_sha256,
                after_sha256=sha256_hex_of_bytes(after_bytes),
                flags=["no_diff"],
                size_bytes=len(after_bytes),
            )
            return record, None

        diff_body, add, deleted = build_diff(
            old_text, new_text, _hunk_context_lines(self._ctx)
        )
        if not diff_body:
            return (
                None,
                None,
            )  # текст совпал несмотря на разные байты (иная кодировка)
        # Синтетический git-заголовок — форма совпадает с остальными записями
        # шелл-канала (докстринг ``journal.py``), тело — как у рекордера.
        hunks = f"diff --git a/{key} b/{key}\n{diff_body}"
        hunks, flags = truncate_hunks(hunks, _max_diff_chars(self._ctx))
        record = _edit_record(
            key,
            turn_id,
            op="shell",
            hunks=hunks,
            add=add,
            deleted=deleted,
            before_sha256=before_sha256,
            after_sha256=sha256_hex_of_bytes(after_bytes),
            flags=flags,
        )
        return record, entry.before_bytes

    def _pocket_capture_base(
        self, store: SnapshotStore, key: str, before_bytes: bytes, turn_id: str
    ) -> bool:
        """Точные ``.orig``/``.turn`` из карманных байтов «до» — но только если
        по пути в наборе ещё нет НИКАКОЙ базы, ни точной, ни счётной.

        Карманный подканал по конструкции не видит ПЕРВУЮ правку пути —
        кандидатом путь становится только после хотя бы одной записи в
        журнале (см. докстринг модуля). Если та, более ранняя, запись сама не
        смогла добыть точное «до» (грязный путь у git-подканала — счётная
        ``.gitorig``, см. ``_capture_base``), карманные байты «до» — это
        состояние ПОСЛЕ той правки, а не начало сессии: записать их в
        ``.orig`` значило бы выдать промежуточное состояние за полный откат
        (тот же Р13/Р32, что не даёт предпочесть точную базу при выборе для
        счёта — см. докстринг ``agent_edits.snapshots``).
        """
        wrote = False
        if store.counting_session_baseline(key) is None:
            # Ревизия — та, что была НА МОМЕНТ съёма карманных байт (до
            # команды), даже если команда сама коммитила: признак ступени
            # HEAD отвечает за «коммит после снимка» (§3.2).
            wrote = store.capture_session(
                key, before_bytes, head_rev=self._head_before
            )
        # Пустой turn_id — снимка хода не бывает: без этой проверки подканал
        # писал снимок под ПУСТЫМ ключом хода (та же защита, что в
        # `_capture_base`; там она уже стоит, здесь её забыли поставить).
        if turn_id and store.counting_turn_baseline(key, turn_id) is None:
            store.capture_turn(key, before_bytes, turn_id)
        return wrote

    def _created_rels(
        self,
        planned: list[tuple[str, str, _FileFacts]],
        before: dict[str, _StatusEntry],
        after: dict[str, _StatusEntry],
    ) -> set[str]:
        """Пути куска, созданные командой (``_created_by_command``), — за
        вычетом игнорируемых.

        Игнорируемого пути в ``git status`` нет никогда, поэтому «пути не было
        до команды» для него ничего не доказывает: файл пользователя, который
        команда поправила и принудительно проиндексировала (``git add -f``),
        выглядит добавленным и свежим — и откат удалил бы его вместе с
        содержимым. Правила игнорирования спрашиваются одним запуском на кусок
        и только у кандидатов.

        Правила после команды — не те, что были до неё: команда могла сама
        убрать правило («перестань игнорировать config и поправь его»), и файл
        пользователя проходил бы проверку как неигнорируемый. Прочитать
        правила «как было» git не умеет, поэтому там, где команда могла их
        ослабить (``_loosened_ignore_scopes``, ``info/exclude``), созданных нет
        вовсе. Остаётся глобальный файл исключений пользователя — его команда
        правит разве что нарочно.
        """
        candidates = [
            rel
            for rel, _key, _file in planned
            if self._created_by_command(rel, after.get(rel), before)
        ]
        return self._not_ignored(candidates)

    def _not_ignored(self, candidates: list[str]) -> set[str]:
        """Кандидаты в созданные, которых не скрывают правила игнорирования.

        Там, где команда могла ослабить правила (``_loosened_scopes``,
        ``info/exclude``), не проходит никто; остальные — одним запуском
        ``git check-ignore`` (``git_ops.ignored_paths``).
        """
        if not candidates:
            return set()
        scopes = list(self._loosened_scopes)
        if self._info_exclude_changed():
            scopes.append("")
        candidates = [
            rel
            for rel in candidates
            if not any(
                scope == "" or rel.startswith(scope) for scope in scopes
            )
        ]
        if not candidates:
            return set()
        return set(candidates) - git_ops.ignored_paths(
            self._launch_root, candidates
        )

    def _info_exclude_signature(self) -> object:
        """Подпись ``info/exclude`` (размер, время, номер файла); ``"missing"`` — файла нет."""
        path = git_ops.info_exclude_path(self._launch_root)
        if path is None:
            return "missing"
        try:
            st = path.stat()
        except FileNotFoundError:
            return "missing"
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns, st.st_ino)

    def _info_exclude_changed(self) -> bool:
        """Команда меняла ``info/exclude`` — в статусе его не видно, сверяем подпись до и после.

        Подпись, а не «время свежее начала команды»: репозиторий, созданный за
        миг до команды, дал бы свежее время без всякой правки правил. Не
        прочитали подпись — считаем, что меняла: ошибка в эту сторону лишает
        отката удалением, а не удаляет файл пользователя.
        """
        now = self._info_exclude_signature()
        return (
            now is None
            or self._info_exclude_before is None
            or now != self._info_exclude_before
        )

    def _created_by_command(
        self,
        rel: str,
        entry: _StatusEntry | None,
        before: dict[str, _StatusEntry],
    ) -> bool:
        """Файл создан ЭТОЙ командой — только тогда пустая база законна.

        Признак — конъюнкция трёх условий, и ни одно нельзя выбросить:

        * пути НЕ было в снимке «до». Само по себе слабо: чистый tracked-файл в
          ``git status`` не значится вовсе, и ``sed -i`` по нему дал бы ложное
          создание;
        * после команды путь неотслеживаемый (``??``) ЛИБО добавлен в индекс
          (``_STAGED_ADD_XY``) — в ``HEAD`` его нет ни в том, ни в другом
          случае, а команда вправе создать файл и сразу его проиндексировать.
          Коды перечислены ПОИМЁННО, а не проверкой первой буквы: у
          неслитого пути порода тоже начинается с ``A`` (``AA`` — добавлен
          обеими сторонами, ``AU`` — нами), но такой путь в ``HEAD`` ЕСТЬ, и
          конфликтное слияние внутри команды выглядело бы созданием — а
          созданное откат удаляет, не спрашивая базу. Само по себе условие
          слабо: так помечен ЛЮБОЙ путь вне ``HEAD``, в том числе лежавший у
          пользователя задолго до команды;
        * файл на диске СВЕЖЕЕ снимка «до». Без этого условия команда, которая
          лишь снимает путь с учёта git (``git rm --cached``,
          ``git reset --mixed``), выглядела бы созданием нетронутого файла
          пользователя — а созданное откат удаляет.

        Отметку времени берём из подписи, снятой при обходе status: лишний
        ``stat`` не нужен. Подписи нет (файл не читается) — считаем, что не
        создан: ошибка в эту сторону лишает отката, а не удаляет чужой файл.
        """
        if rel in before or entry is None:
            return False
        if entry.xy != "??" and entry.xy not in _STAGED_ADD_XY:
            return False
        return self._fresh(entry.signature)

    def _fresh(self, signature: tuple[int, int] | None) -> bool:
        """Файл свежее снимка «до» (с допуском ``_MTIME_TOLERANCE_NS``).

        Подписи нет (файл не читается) — не свежий: ошибка в эту сторону
        лишает отката удалением, а не удаляет чужой файл.
        """
        if signature is None:
            return False
        return signature[1] >= self._before_ts_ns - _MTIME_TOLERANCE_NS

    def _capture_base(
        self,
        store: SnapshotStore,
        rel: str,
        key: str,
        turn_id: str,
        *,
        exact: bool,
        facts: _BaseFacts | None,
    ) -> bool:
        """База «до» этой записи. ``True`` — записали новую (сигнал для эвикции).

        Точная ступень применима (``exact``: путь был чист до команды, не
        неслит после, а HEAD стоит или сдвинут своими коммитами команды —
        ``_exact_step``) и у пути нет кастомного фильтра — диск, индекс и
        ``HEAD`` до команды совпадали (Р32): «до» читается из ``HEAD`` до
        команды в форме файла на диске (``git_ops.head_bytes_like_disk``) и
        кладётся в ТОЧНОЕ
        пространство (``.orig``/``.turn``) — такой базе годится откат. Гвард
        фильтра обязан идти ПЕРЕД чтением содержимого: конфиг чужого
        репозитория не должен исполнять свой код.

        Стиль концов строк базы — по файлу ПОСЛЕ команды: это лучший
        доступный признак «родной» формы пути (команда, сменившая стиль,
        унесёт его и в базу — зато файл, лежащий с LF в CRLF-репозитории, не
        считается изменённым целиком).

        Иначе (путь был грязен или неслит, фильтр есть, или ``HEAD`` до
        команды не читает путь вовсе) — прежний путь:
        СЧЁТНАЯ база (Р13) из индекса git (``.gitorig``/``.gitturn``). Для
        отката не годится никогда: индекс не содержит несохранённых ручных
        правок пользователя (см. докстринг ``agent_edits.snapshots``).

        Пути нет на выбранной ступени или git отказал — молчим: счётчики
        честно откатятся на суммы из журнала. Soft-fail: правка уже произошла.

        Ответы git — из ``facts`` (собраны пачкой, ``_base_facts``); поштучно
        спрашивается только то, чего пачка не покрывает. ``facts is None`` —
        пачка не собралась, и всё спрашивается по одному.
        """
        try:
            content = self._exact_head_content(rel, exact=exact, facts=facts)
            if content is not None:
                wrote = store.capture_session(
                    key,
                    content,
                    head_rev=self._head_before,
                )
                if turn_id:
                    # Та же проверка, что и в счётной ветке ниже: без неё
                    # точная ветка писала снимок хода под ПУСТЫМ turn_id.
                    store.capture_turn(key, content, turn_id)
                return wrote
            # Точная ступень неприменима или HEAD не читает путь (например,
            # свежий rename) — счётный путь ниже.
            before = (
                facts.index[rel]
                if facts is not None and rel in facts.index
                else git_ops.show_index_bytes(self._launch_root, rel)
            )
            if before is None:
                return False
            wrote = store.capture_shell_session(key, before)
            if turn_id:
                store.capture_shell_turn(key, before, turn_id)
            return wrote
        except Exception:
            _logger.warning(
                "shell delta: база не снята rel=%r", rel, exc_info=True
            )
            return False

    def _exact_head_content(
        self, rel: str, *, exact: bool, facts: _BaseFacts | None
    ) -> bytes | None:
        """«До» из ``HEAD`` до команды в форме файла на диске — если точная
        ступень применима.

        ``None`` — ступень неприменима (``exact`` ложен, у пути кастомный
        фильтр) или ``HEAD`` до команды пути не читает. Гвард фильтра идёт
        ПЕРЕД чтением содержимого (см. ``_capture_base``).
        """
        if not exact:
            return None
        filtered = (
            rel in facts.filtered
            if facts is not None
            else git_ops.has_custom_filter(self._launch_root, rel)
        )
        if filtered:
            return None
        disk = _read_bytes(self._abs(rel))
        if disk is None or facts is None or rel not in facts.head:
            # Пачки нет, её кусок не прочитался, либо файл пропал уже после
            # команды («родной» формы не узнать — форма выкладки) — по одному.
            return git_ops.head_bytes_like_disk(
                self._launch_root, rel, disk, rev=self._head_before
            )
        blob = facts.head[rel]
        return None if blob is None else git_ops.blob_like_disk(blob, disk)

    def _remember(self, record: EditRecord) -> None:
        """Запомнить записанное состояние пути (только после удачной записи).

        Рядом с хэшем кладётся ПОДПИСЬ файла — по ней перед следующей командой
        видно, трогал ли путь кто-то ещё (``_forget_stale``).
        """
        if record.op == "delete":
            # Файла нет: путь снимается с дедупликации, чтобы повторное
            # создание того же содержимого снова попало в журнал.
            self._seen.pop(record.path, None)
        elif record.after_sha256 is not None:
            self._seen[record.path] = (
                record.after_sha256,
                _file_signature(
                    abs_path_for_key(self._launch_root, record.path)
                ),
            )

    def _build_record(
        self,
        rel: str,
        entry: _StatusEntry | None,
        turn_id: str,
        *,
        created: bool,
        clean_before: bool,
        hunks: str | None = None,
        key: str | None = None,
        file: _FileFacts | None = None,
        deleted: tuple[bytes, int] | None = None,
        committed: bool = False,
    ) -> EditRecord | None:
        """Запись пути. ``hunks``, ``key`` и ``file`` — уже посчитанное куском
        (``_diffs_for``, ``_plan``); ``None`` — считается здесь, по одному.
        ``deleted`` — байты «до» и режим пропавшего пути с зарезервированным
        бюджетом (``_deleted_contents``); ``None`` — удаление без строк.
        ``committed`` — путь закоммичен самой командой (``_committed``): его
        нет в status после, и правило «нет в status — записи нет» к нему не
        применяется.
        """
        abs_path = self._abs(rel)
        if key is None:
            key = canonical_path_key(
                str(abs_path),
                self._launch_root,
                protected_extra=protected_extra_from_ctx(self._ctx),
            )
        # Внешний путь шелл-канал не записывает ВОВСЕ: у шелл-записи `call_id`
        # пустой, значит блока в ленте она не даёт никогда, а на экран внешний
        # ключ не пускает `deltas.file_entries`. Такая запись была бы невидима
        # отовсюду — чистый мусор в журнале. Сюда же уехала защита от ссылки
        # наружу: с исчезновением периметра `canonical_path_key` отдаёт ей
        # абсолютный ключ вместо `None`, и проверка на `None` её не ловит.
        if key is None or is_external_key(key):
            return None
        if file is None:
            file = _file_facts(abs_path)

        if not file.exists:
            before, mode = deleted or (None, None)
            return delete_record(
                key,
                turn_id,
                before,
                mode=mode,
                budget=None,
                context_lines=_hunk_context_lines(self._ctx),
                max_chars=_max_diff_chars(self._ctx),
            )
        if entry is None and not committed:
            # Путь ушёл из status, файл на месте: команда сама вернула его к
            # состоянию индекса (add / commit / restore) — показывать нечего.
            return None

        after_sha256 = _sha256_of_file(abs_path)
        remembered = self._seen.get(key)
        if (
            after_sha256 is not None
            and remembered is not None
            and remembered[0] == after_sha256
        ):
            # Содержимое то же, что в прошлой записи об этом пути: команда
            # перезаписала файл теми же байтами (агент повторил команду). У
            # untracked дифф с /dev/null НИКОГДА не пуст, поэтому такой дубль
            # ловится только по хэшу — иначе он раздувает счётчики хода.
            return None
        # Контракт журнала — общий с рекордером: у бинаря и у файла сверх капа
        # исходника хэнков нет (иначе в них оказался бы псевдо-дифф «Binary
        # files ... differ»), и род содержимого говорит ФЛАГ `no_diff`. Вид
        # правки при этом остаётся своим: затирая его бинарностью, запись
        # теряла создание файла, а с ним право на откат удалением (пятый раунд
        # ревью, находка 5).
        if file.binary or file.size > _max_diff_source_bytes(self._ctx):
            return _edit_record(
                key,
                turn_id,
                op="create" if created else "shell",
                after_sha256=after_sha256,
                flags=["no_diff"],
                size_bytes=file.size,
            )

        if hunks is None:
            hunks = self._diff(
                rel,
                untracked=_xy(entry) == "??",
                base_rev=self._diff_base_rev(clean_before),
            )
        if not hunks:
            # git разницы не видит (правка отменена, изменилась только mtime).
            return None
        add, deleted = _count_add_del(hunks)
        hunks, flags = truncate_hunks(hunks, _max_diff_chars(self._ctx))
        return _edit_record(
            key,
            turn_id,
            # Файл создан этой командой: базы у него нет и быть не должно —
            # законность пустой базы даёт op="create" (deltas.created_here).
            op="create" if created else "shell",
            hunks=hunks,
            add=add,
            deleted=deleted,
            after_sha256=after_sha256,
            flags=flags,
        )

    def _snapshot(self) -> dict[str, _StatusEntry]:
        return {
            rel: _StatusEntry(xy=xy, signature=_file_signature(self._abs(rel)))
            for rel, xy in self._status()
        }

    def _status(self) -> list[tuple[str, str]]:
        """Снимок рабочего дерева. Вызов и разбор — в ``git_ops``: тот же снимок
        читает экран изменений (состояние индекса), и двух разборов
        porcelain-строк быть не должно."""
        rows = git_ops.status_entries(self._launch_root)
        if rows is None:
            raise _GitFailed("git status не выполнен")
        return rows

    def _diff_base_rev(self, clean_before: bool) -> str | None:
        """Ревизия, против которой дифф пути честен. ``None`` — против индекса.

        ``git diff -- rel`` сравнивает ИНДЕКС с деревом, и команда, которая
        сама индексирует свою правку (``… && git add -A``, ``black . && git
        add -u``, pre-commit), сдвигает обе стороны разом: дифф пуст, записи
        нет ни в журнале, ни в счётчиках хода, ни на экране.

        Путь, чистый до команды, совпадал с ``HEAD`` ДО команды и на диске, и
        в индексе (тот же инвариант Р32, на котором стоит точная база в
        ``_capture_base``, §5.3). Пока ``HEAD`` стоит или его сдвинули свои
        коммиты команды (``_head_before_trusted``), дифф от ``HEAD`` до
        команды виден независимо от того, попала правка в индекс или в коммит
        самой команды, а «до» у диффа и у базы одно. После чужого сдвига
        ``HEAD`` (``git checkout other``) — дифф от индекса: от старого
        ``HEAD`` он показал бы разницу веток правкой команды.

        Путь был грязен до команды — «до» этому подканалу неизвестно, и
        остаётся дифф от индекса: правка со стейджем тогда по-прежнему не
        видна. Такие пути ловит карманный подканал — по байтам, снятым до
        команды.

        Окно диффа при этом шире «индекс↔дерево»: правку, которую пользователь
        сделал И ПРОИНДЕКСИРОВАЛ вручную, пока команда работала, запись отнесёт
        к команде. Это не новая щель, а та же самая: незаиндексированную правку
        того же пользователя в том же окне канал приписывал команде и раньше —
        она видна обычному ``git diff``. Отличить «кто именно сдвинул путь» по
        двум снимкам состояния нельзя в принципе, и заводить ради этого
        механизм не за что.
        """
        if clean_before and self._head_before_trusted():
            return self._head_before
        return None

    def _diff(self, rel: str, *, untracked: bool, base_rev: str | None) -> str:
        # Код возврата здесь намеренно не читается: у --no-index exit 1 значит
        # «файлы различаются» — это норма, а не отказ.
        _, out = git_ops.run_git(
            self._launch_root,
            _diff_args(rel, untracked=untracked, base_rev=base_rev),
        )
        return _decode_git_output(out)

    def _diff_many(
        self, rels: list[str], *, base_rev: str | None
    ) -> dict[str, str]:
        """Дифф отслеживаемых путей ПАЧКОЙ — ``{путь: то же, что дал бы _diff}``.

        Один запуск git на кусок командной строки вместо запуска на путь (замер:
        2000 путей — 10 с одних диффов). Путь без секции в выводе — пустой
        дифф, ровно как у пофайлового вызова; но только если весь вывод
        разобран и git не отказал. Иначе кусок добирается пофайловым
        ``_diff`` — пачка вправе быть быстрее, но не иначе.
        """
        diffs: dict[str, str] = {}
        rev = [base_rev] if base_rev else []
        for chunk in git_ops.argv_chunks(
            rels, size=lambda rel: len(_literal(rel))
        ):
            code, out = git_ops.run_git(
                self._launch_root,
                [
                    *_GIT_DIFF_ARGS,
                    *rev,
                    "--",
                    *(_literal(rel) for rel in chunk),
                ],
            )
            # Отказ git — ни одной секции не верим: напечатанная до отказа может
            # быть оборвана посреди хэнка, и счётчики легли бы в журнал неверными.
            sections, clean = (
                git_ops.split_patch_by_path(out) if code == 0 else ({}, False)
            )
            for rel in chunk:
                if rel in sections:
                    diffs[rel] = _decode_git_output(sections[rel])
                elif clean:
                    diffs[rel] = ""
                else:
                    diffs[rel] = self._diff(
                        rel, untracked=False, base_rev=base_rev
                    )
        return diffs

    def _untracked_diffs(self, rels: list[str]) -> dict[str, str]:
        """Дифф новых файлов — по файлу, но параллельно (``git_ops.run_git_many``).

        Пачкой его не собрать: ``--no-index`` сравнивает ровно два пути, а
        временный индекс ради ``git diff`` исполнил бы фильтры репозитория,
        которые канал нарочно не исполняет (``has_custom_filter``).
        """
        answers = git_ops.run_git_many(
            self._launch_root,
            [_diff_args(rel, untracked=True, base_rev=None) for rel in rels],
        )
        return {
            rel: _decode_git_output(out)
            for rel, (_code, out) in zip(rels, answers)
        }

    def _abs(self, rel: str) -> Path:
        return Path(self._launch_root) / rel


def _edit_record(
    path: str,
    turn_id: str,
    *,
    op: str,
    hunks: str = "",
    add: int = 0,
    deleted: int = 0,
    before_sha256: str | None = None,
    after_sha256: str | None = None,
    flags: list[str] | None = None,
    size_bytes: int | None = None,
) -> EditRecord:
    """Запись канала: ``call_id`` тут не заполняется, ``before_sha256`` — по
    возможности.

    У git-подканала ``before_sha256`` неизвестен в принципе: делта видит файл
    только ПОСЛЕ команды, состояния «до» у него нет, и умолчание ``None``
    остаётся за ним. Карманный подканал держит точные байты «до» в памяти и
    хэш передаёт: цепочка целостности пути идёт сквозь его записи, а не
    рвётся на них (пятый раунд ревью).

    ``call_id=None`` — признак шелл-канала, и пустой он сознательно, а не по
    забывчивости: делту снимает
    обёртка вокруг запуска команды — слой ниже инструментов, — и привязывать
    инлайн-блок в ленте не к чему (см. спеку §5). Соседние агентские оболочки
    решают так же или хуже: правки через шелл у них либо не показываются вовсе,
    либо не привязываются к строке команды. Причина содержательная: одна команда
    трогает десятки файлов, инлайн-блоки залили бы ленту. Shell-правки живут в
    сводном слое: журнал, счётчики хода, экран обзора.

    Какой вызов команды трогал файл, говорит ``command_call_id``: его ставит
    объект делты на каждую запись перед дописыванием в журнал (``call_id``
    и признак канала он не трогает)."""
    return EditRecord(
        path=path,
        turn_id=turn_id,
        ts=time.time(),
        call_id=None,
        op=op,
        hunks=hunks,
        add=add,
        del_=deleted,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        flags=list(flags or []),
        size_bytes=size_bytes,
    )


def _shell_tracking_enabled(ctx: Any) -> bool:
    return bool(
        agent_edits_config(ctx).get("shell_tracking", DEFAULT_SHELL_TRACKING)
    )


def _max_diff_chars(ctx: Any) -> int:
    return limit(None, ctx, "max_diff_chars", DEFAULT_MAX_DIFF_CHARS)


def _max_diff_source_bytes(ctx: Any) -> int:
    return limit(
        None, ctx, "max_diff_source_bytes", DEFAULT_MAX_DIFF_SOURCE_BYTES
    )


def _snapshots_max_files(ctx: Any) -> int:
    return limit(None, ctx, "snapshots_max_files", DEFAULT_SNAPSHOTS_MAX_FILES)


def _hunk_context_lines(ctx: Any) -> int:
    return limit(None, ctx, "hunk_context_lines", DEFAULT_HUNK_CONTEXT_LINES)


def _xy(entry: _StatusEntry | None) -> str | None:
    """XY пути в снимке status; ``None`` — пути в снимке нет."""
    return None if entry is None else entry.xy


def _dedup(paths: list[str]) -> list[str]:
    """Без повторов, порядок первого появления — приоритет у начала списка."""
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _pocket_known_paths(
    ctx: Any, cache: dict[Path, tuple[tuple[int, int], list[str]]]
) -> list[str]:
    """Пути набора из журнала, недавно правленные — первыми.

    Кэш по подписи файла журнала (размер+mtime): без записи в журнал между
    командами повторный парс JSONL не нужен — сигнатура файла не изменилась.
    Журнала ещё нет на диске (свежий тред) — пустой список, не отказ.
    """
    identity = identity_from_ctx(ctx)
    if identity is None:
        return []
    journal_path = edits_journal_path(*identity)
    try:
        st = journal_path.stat()
    except OSError:
        return []
    signature = (st.st_size, st.st_mtime_ns)
    cached = cache.get(journal_path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    paths = _paths_by_recency(EditsJournal(journal_path).read_all())
    cache[journal_path] = (signature, paths)
    return paths


def _paths_by_recency(records: list[EditRecord]) -> list[str]:
    """Различные пути записей, отсортированные по last ts — недавние первыми."""
    last_ts: dict[str, float] = {}
    for rec in records:
        prev = last_ts.get(rec.path)
        if prev is None or rec.ts > prev:
            last_ts[rec.path] = rec.ts
    return sorted(last_ts, key=lambda path: last_ts[path], reverse=True)


def _is_text(data: bytes) -> bool:
    """Байты годятся в строки удаления: не бинарь и декодируются в текст."""
    return not is_binary_bytes(data) and decode_edit_text(data) is not None


def _is_binary_file(path: Path) -> bool:
    """Тот же классификатор, что у рекордера (проба головы файла)."""
    try:
        return classify_file_content(str(path)).is_binary
    except OSError:
        return False


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_bytes(path: Path) -> bytes | None:
    """Байты файла или ``None`` (нет/не читается) — для стиля концов строк базы."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _sha256_of_file(path: Path) -> str | None:
    try:
        return sha256_hex_of_file_bytes(str(path))
    except OSError:
        return None


def _decode_git_output(data: bytes) -> str:
    """Байты диффа git → текст журнала: явный декод, без опоры на локаль.

    Цепочка — общая с обоими каналами (``deltas.decode_edit_text``, Д2), и
    замена на U+FFFD остаётся только последним резервом: сюда приходит то, что
    git УЖЕ признал текстом (бинарь он диффом не отдаёт), и вернуть пустоту
    вместо хэнков нельзя. Записанное с заменой не восстанавливается — потому
    догадка о кириллице и стоит ПЕРЕД ней.
    """
    if not data:
        return ""
    return decode_edit_text(data) or data.decode("utf-8", errors="replace")


def _count_add_del(diff_text: str) -> tuple[int, int]:
    """add/del по телу хэнков: заголовки git-диффа лежат вне ``@@``-секций."""
    add = 0
    deleted = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("diff --git"):
            in_hunk = False
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            deleted += 1
    return add, deleted
