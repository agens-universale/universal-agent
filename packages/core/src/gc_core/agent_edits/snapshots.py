"""Per-thread снапшоты «до правки»: ``.orig`` (сессия) + ``.turn`` (ход).

Байтовое хранение, sha256-имена, атомарная запись. Снапшоты именуются на тред
(``<sha256(path_key)>_<thread_key>``), два уровня давности — ``.orig``
(первая правка в сессии) и ``.turn`` (первая правка в текущем ходе,
отслеживается по ``turn_id`` в ``index.json``).

Три ПОРОДЫ баз, и смешивать их нельзя:

* **точный снимок** (``.orig``/``.turn``) — реальное содержимое файла на диске
  за миг до правки, пишет ``recorder`` из fs-тулов. Годится и для счётчиков, и
  для отката;
* **счётная база** (``.gitorig``/``.gitturn``) — содержимое ИНДЕКСА git, пишет
  шелл-канал: команду никакой тул не видел, и другого «до» у неё нет. Годится
  ТОЛЬКО для счётчиков. Индекс не содержит несохранённых ручных правок
  пользователя, и запись его содержимого поверх файла стёрла бы их молча,
  поэтому откат счётную базу не видит вовсе: ``session_baseline`` /
  ``turn_baseline`` (и предикаты ``has_*``) читают только точные снимки;
* **принятая база** (``.accepted``) — точный снимок диска в момент явного
  принятия правок, не привязан к сессии/ходу. Годится и для счётчиков, и для
  отката. В отличие от ``.orig``/``.turn`` — пишется не «только если ещё
  нет», а перезаписывается при каждом принятии; см. ``capture_accepted``.

Точная база поверх счётной НЕ пишется (``capture_session``/``capture_turn``):
счётная значит, что путь уже правила шелл-команда без точного «до», и снятое
позже — состояние ПОСЛЕ команды; в ``.orig`` оно выдало бы промежуточное
состояние за начало сессии.

Для СЧЁТА берётся та база, которую записали РАНЬШЕ (``counting_*_baseline``),
а не «точная в приоритете»: в хранилищах, записанных до правила выше,
``.orig`` бывает моложе ``.gitorig`` — сначала файл правила команда, потом
инструмент. Предпочтение точной выбрало бы там более позднюю базу, и дельта
команды молча выпала бы из кумулятива.

Слой данных: без UI, git, конфиг-чтения; лимиты (``max_files``) приходят
параметром.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.file_lock import file_lock

# stem нового формата: <64 hex sha256>_<thread_key>; legacy — голый sha без хвоста.
_STEM_RE = re.compile(r"^[0-9a-f]{64}_(.+)$")

_JOURNAL_STEM_PREFIX = "edits_"

# Суффикс снимка → ключ его turn-отметки в index.json. У ``.gitorig`` отметки
# нет: существование файла самодостаточно; у ``.orig`` своя — см.
# ``_SESSION_HEAD_REV_KEY``.
_TURN_INDEX_KEYS = {".turn": "turn_id", ".gitturn": "shell_turn_id"}

# Ключи принятой базы в записи index.json — все три чистятся вместе (эвикция,
# clear_accepted): в отличие от turn-отметок, они не делят запись с другой породой.
_ACCEPTED_INDEX_KEYS = ("accepted_ts", "accepted_absent", "accepted_head_oid")

# Ревизия HEAD в момент съёма ``.orig`` — признак «коммит затронул путь уже
# после начала сессии» (§3.2 спеки волны 4). Живёт ровно столько же, сколько
# сам ``.orig``: эвикция сносит их вместе.
_SESSION_HEAD_REV_KEY = "session_head_rev"

# Ревизия HEAD в момент СОЗДАНИЯ файла — тот же признак у пути, у которого
# ``.orig`` нет и не будет («пустая база не хранится», Д1). Ключ ОТДЕЛЬНЫЙ от
# ``session_head_rev`` намеренно: вторая правка созданного файла снимает
# ``.orig`` с пост-созданным содержимым и приносит СВОЮ, уже пост-коммитную
# ревизию — общий ключ затёр бы момент создания и ступень HEAD исчезла бы.
_CREATED_HEAD_REV_KEY = "created_head_rev"

_SNAPSHOT_SUFFIXES = (".orig", ".turn", ".gitorig", ".gitturn", ".accepted")

# служебный каталог не должен светиться в git status пользовательского проекта
_GITIGNORE_BODY = "*\n"

# Умерший стор старой модели правок: `<launch_root>/.gc/diffs`.
_LEGACY_STORE_DIR = "diffs"


@dataclass(frozen=True)
class AcceptedState:
    """Принятая база одного файла — см. ``SnapshotStore.accepted_state``."""

    content: bytes | None  # None — принято «отсутствие файла» (явный маркер)
    ts: float  # момент принятия
    head_oid: str | None  # blob-oid HEAD:<rel> на момент принятия; None — не git/нет в HEAD


def _path_hash(path_key: str) -> str:
    return hashlib.sha256(path_key.encode("utf-8")).hexdigest()


def _read_earlier(*paths: Path) -> Optional[bytes]:
    """Содержимое того из путей, что записан РАНЬШЕ (по ``st_mtime_ns``).

    Несуществующие пути отбрасываются; не осталось ни одного — ``None``.
    """
    stamped = []
    for path in paths:
        try:
            stamped.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    if not stamped:
        return None
    try:
        return min(stamped, key=lambda item: item[0])[1].read_bytes()
    except OSError:
        return None


def _turn_taken(entry: Dict[str, Any], index_key: str, turn_id: str) -> bool:
    """Базу хода ``turn_id`` уже снимали — ещё одна не пишется.

    Точная база хода не ложится и поверх счётной того же хода — по той же
    причине, что у ``capture_session``: это состояние после команды.
    """
    if entry.get(index_key) == turn_id:
        return True
    return index_key == _TURN_INDEX_KEYS[".turn"] and (
        entry.get(_TURN_INDEX_KEYS[".gitturn"]) == turn_id
    )


def _drop_quietly(path: Path) -> None:
    """Удалить свой файл, не подменяя причину отказа, если удалить не вышло."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _apply_marks(
    index: Dict[str, Any], key: str, updates: Dict[str, Optional[str]]
) -> bool:
    """Наложить поля отметки на запись ``key``; ``None`` — снять поле.

    ``True`` — запись изменилась (писать индекс есть смысл). Опустевшая запись
    уходит из индекса целиком.
    """
    current = index.get(key)
    before = current if isinstance(current, dict) else {}
    merged = dict(before)
    for field, value in updates.items():
        if value is None:
            merged.pop(field, None)
        else:
            merged[field] = value
    if merged == before:
        return False
    if merged:
        index[key] = merged
    else:
        index.pop(key, None)
    return True


def snapshots_root(launch_root: str) -> Path:
    """Каталог снапшотов правок агента в проекте пользователя."""
    return Path(launch_root) / ".gc" / "agent_edits"


def _legacy_store_dir(root: Path) -> Optional[Path]:
    """Каталог умершего стора рядом с нашим корнем — или ``None``.

    Проверка имени родителя обязательна: без неё стор с произвольным корнем
    (так его конструируют тесты) начал бы заглядывать в случайный соседний
    каталог с именем ``diffs``.
    """
    parent = root.parent
    return parent / _LEGACY_STORE_DIR if parent.name == ".gc" else None


def _drop_legacy_store(root: Path) -> bool:
    """Снести умерший ``<launch_root>/.gc/diffs``. Best-effort, зовётся один раз
    при создании нового корня снапшотов.

    Осторожность вместо смелости: каталог удаляется, ТОЛЬКО если в нём нет
    ничего, кроме ``*.orig`` и ``index.json`` — то есть это ровно наш старый
    кэш, а не чужие файлы, случайно попавшие под то же имя.

    Ссылка на каталог по имени ``diffs`` не разбирается вовсе: под ней лежит
    ЧУЖОЙ каталог, а фильтр по содержимому смотрит уже сквозь ссылку и вынес бы
    его файлы. Junction на Windows проверяется отдельно — ``is_symlink()`` для
    него отдаёт ``False``.
    """
    legacy = _legacy_store_dir(root)
    if legacy is None or not legacy.is_dir():
        return False
    if legacy.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(legacy):
        return False
    try:
        entries = list(legacy.iterdir())
        for p in entries:
            if not p.is_file() or (p.suffix != ".orig" and p.name != "index.json"):
                return False
        for p in entries:
            p.unlink()
        legacy.rmdir()
    except OSError:
        return False
    return True


def thread_key_from_journal_path(journal_path: Path) -> str:
    """``edits_<thread_key>.jsonl`` → ``thread_key`` (см. докстринг класса)."""
    return Path(journal_path).stem[len(_JOURNAL_STEM_PREFIX):]


class SnapshotStore:
    """Снапшоты «до правки» одного треда под ``<launch_root>/.gc/agent_edits``
    (см. ``snapshots_root``).

    ``.orig`` — baseline на первую правку файла в сессии: пишется один раз,
    повторные вызовы — no-op. ``.turn`` — baseline на первую правку файла в
    текущем ходе; какому ``turn_id`` соответствует содержимое ``.turn``-файла,
    хранится в ``index.json``. Там же — ревизия HEAD на момент съёма ``.orig``
    (признак ступени HEAD, см. ``capture_session``).

    ``.accepted`` — принятая база: в отличие от ``.orig``/``.turn`` ВСЕГДА
    перезаписывает прежнее принятие. Принятие «отсутствия файла» хранит в
    ``index.json`` явный маркер (``accepted_absent``), а не голое отсутствие
    байтового файла — контракт валидности см. в докстринге ``accepted_state``.

    Отметка «создан здесь при ревизии X» (``mark_created``) живёт в
    ``index.json`` и байтового файла не имеет вовсе: база созданного пути —
    пустота, хранить её нечем.

    ``thread_key`` — составная строка ``f"{system_id}_{thread_id}"``, ровно
    та же, что стоит в имени журнала правок после префикса ``edits_`` (см.
    ``thread_store.edits_journal_path``: ``edits_<system_id>_<thread_id>.jsonl``).
    От этого напрямую зависит ``evict_lru``: живость треда проверяется точным
    именем ``edits_<thread_key>.jsonl`` в ``threads_dir`` — без wildcard.
    """

    def __init__(
        self,
        root: Path,
        thread_key: str,
        threads_dir: Optional[Path] = None,
    ) -> None:
        self.root = Path(root)
        self.thread_key = thread_key
        self.threads_dir = Path(threads_dir) if threads_dir is not None else None
        # Кэш индекса по подписи файла — см. `_index_snapshot`.
        self._index_cache: Optional[tuple[tuple[int, int], Dict[str, Any]]] = None
        # Отложенные отметки `deferred_index`: stem → {поле: значение, None — снять}.
        self._pending: Optional[Dict[str, Dict[str, Optional[str]]]] = None
        self.index_path = self.root / "index.json"

    # --- paths ---

    def _stem(self, path_key: str) -> str:
        return f"{_path_hash(path_key)}_{self.thread_key}"

    def _session_path(self, path_key: str) -> Path:
        return self.root / f"{self._stem(path_key)}.orig"

    def _turn_path(self, path_key: str) -> Path:
        return self.root / f"{self._stem(path_key)}.turn"

    def _shell_session_path(self, path_key: str) -> Path:
        return self.root / f"{self._stem(path_key)}.gitorig"

    def _shell_turn_path(self, path_key: str) -> Path:
        return self.root / f"{self._stem(path_key)}.gitturn"

    def _accepted_path(self, path_key: str) -> Path:
        return self.root / f"{self._stem(path_key)}.accepted"

    def _ensure_dir(self) -> None:
        first = not self.root.is_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        # Не «только при создании каталога»: каталоги, заведённые прежней версией
        # кода, тоже обязаны получить самоигнор — иначе у живых пользователей
        # снапшоты так и останутся в git status.
        ignore = self.root / ".gitignore"
        if not ignore.exists():
            ignore.write_text(_GITIGNORE_BODY, encoding="utf-8", newline="\n")
        # А вот чистка legacy — именно «только при создании»: она обходит чужой
        # каталог целиком, и платить этим обходом за каждую записанную строчку
        # незачем. Заведение нашего корня случается один раз на проект.
        if first:
            _drop_legacy_store(self.root)

    def _write_bytes(self, dest: Path, content: bytes) -> None:
        """Записать снимок атомарно, через СВОЙ временный файл.

        Имя временного файла уникально на писателя: имя снимка зависит только
        от ключа пути и ключа треда, а одному треду ключ дают и открытый TUI, и
        `gc-cli` по тому же треду (а скоро — параллельные субагенты). С общим
        именем второй писатель уносил бы файл из-под первого: либо снимок,
        перемешанный байтами (откат записал бы его пользователю поверх работы),
        либо ENOENT на замене — и путь остался бы без базы отката вовсе.

        Замок сюда не годится: он завёл бы ещё один долгоживущий файл в том же
        каталоге, а `os.replace` атомарен и сам по себе.

        Хвост уникальности — ВОСЕМЬ знаков, а не весь `uuid4().hex`. Имя
        снимка и так длинное (`<sha256 ключа пути>_<system_id>_<thread_id>`,
        около 127 знаков), а предел пути на Windows без `LongPathsEnabled` —
        260. Полные 32 знака отнимали у каталога проекта ещё треть этого
        бюджета, и проекты, которые работали, переставали бы — молча: отказ
        записи снимка рекордер глотает, пользователь видит «точный снимок
        недоступен» и мёртвый откат. Окно столкновения — микросекунды между
        созданием и заменой, пространство 2^32; бюджет пути дороже. Страж
        длины — `test_snapshots.test_tmp_name_stays_within_the_path_budget`.
        """
        tmp = self._stage_bytes(dest, content)
        try:
            atomic_replace(tmp, dest)
        except OSError:
            _drop_quietly(tmp)
            raise

    def _publish_once(self, dest: Path, content: bytes) -> bool:
        """Записать снимок, только если его ещё нет, — АТОМАРНО. ``True`` — записали.

        Проверка «снимка нет» и запись по отдельности оставляли окно: двое
        писателей одного пути (субагенты, TUI вместе с `gc-cli`) оба видели
        пустоту, и опоздавший затирал исходное состояние своим — состоянием
        ПОСЛЕ чужой правки; откат «к началу» возвращал бы не туда. Жёсткая
        ссылка на готовый временный файл публикует байты целиком и не
        перезаписывает существующее: занятое имя — проигрыш, а не замена.

        Файловая система без жёстких ссылок (FAT, часть сетевых дисков) — та же
        проверка и замена, что прежде: окно остаётся, но снимок пишется.
        """
        if dest.is_file():
            return False
        tmp = self._stage_bytes(dest, content)
        try:
            try:
                os.link(tmp, dest)
                return True
            except FileExistsError:
                return False
            except OSError:
                if dest.is_file():
                    return False
                atomic_replace(tmp, dest)
                return True
        finally:
            _drop_quietly(tmp)

    def _stage_bytes(self, dest: Path, content: bytes) -> Path:
        """Байты снимка — во временный файл рядом с ``dest`` (имя — см. ``_write_bytes``).

        Отдельно от замены — для записей, где байты обязаны лечь только ПОСЛЕ
        индекса (``capture_accepted``).
        """
        self._ensure_dir()
        tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(content)
        except OSError:
            _drop_quietly(tmp)
            raise
        return tmp

    # --- index (отметки всех пород; читать/писать только под file_lock) ---

    def _load_index(self) -> Dict[str, Any]:
        if not self.index_path.is_file():
            return {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_index(self, index: Dict[str, Any]) -> None:
        self._index_cache = None      # своя запись — подпись изменится, но не ждём её
        self._ensure_dir()
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        atomic_replace(tmp, self.index_path)

    def _mark(self, path_key: str, updates: Dict[str, Optional[str]]) -> None:
        """Поставить (значение) или снять (``None``) поля отметки одного пути.

        Запись индекса делят отметки нескольких пород (turn, accepted, ревизии
        HEAD): меняются только названные поля, перезапись записи словарём
        целиком осиротила бы чужие файлы. Внутри ``deferred_index`` — только в
        память, индекс пишется одним разом в конце блока.
        """
        key = self._stem(path_key)
        if self._pending is not None:
            self._pending.setdefault(key, {}).update(updates)
            return
        with file_lock(self.index_path, timeout=5.0):
            index = self._load_index()
            if _apply_marks(index, key, updates):
                self._save_index(index)

    @contextmanager
    def deferred_index(self) -> Iterator[None]:
        """Отметки захвата копятся в памяти и пишутся в индекс ОДНОЙ записью.

        Индекс переписывается целиком на каждой отметке, а растёт с числом путей:
        команда, тронувшая тысячи файлов, переписывала его по два раза на файл —
        цена квадратичная (замер: 2000 файлов — 20 с из 51 под профайлером).

        Чтения внутри блока видят отложенное (``_index_entry``) — на них стоит
        идемпотентность захвата. В конце отметки накладываются на СВЕЖЕ
        прочитанный под замком индекс, а не на копию из начала блока: пока идёт
        пакет, индекс вправе писать другой писатель того же треда (принятие в
        открытом TUI), и его отметка должна уцелеть. Замок держится только на
        время сброса — пакет длиной в секунды не заставляет соседа ждать.

        Сбой сброса оставляет снимки без отметок — та же деградация, что у
        одиночной записи, упавшей между байтами и индексом: у пути нет ступени
        HEAD или `.turn` перепишется тем же ходом ещё раз.
        """
        if self._pending is not None:
            yield                        # вложенный блок — копит в тот же пакет
            return
        self._pending = {}
        try:
            yield
        finally:
            pending, self._pending = self._pending, None
            if pending:
                with file_lock(self.index_path, timeout=5.0):
                    index = self._load_index()
                    changed = False
                    for key, updates in pending.items():
                        changed |= _apply_marks(index, key, updates)
                    if changed:
                        self._save_index(index)

    # --- capture ---

    def capture_session(
        self, path_key: str, content: bytes, *, head_rev: str | None = None
    ) -> bool:
        """Записать session-baseline, только если его ещё нет. ``True`` — записали.

        Вызывающему нужно отличать запись от no-op: снимок пишется один раз на
        файл на тред, и только к этому событию имеет смысл цеплять эвикцию.

        ``head_rev`` — ревизия HEAD В МОМЕНТ съёма (``git_ops.head_rev_from_files``
        у рекордера, снимок «до команды» у шелл-канала). По ней лестница
        отсчёта потом решает, затронул ли путь коммит уже в этой сессии
        (``session_head_rev``): признак ФИКСИРУЕТСЯ, а не выводится сравнением
        содержимого — «HEAD отличается от снимка» одинаково верно и для
        файла, грязного ДО сессии, и ступень HEAD объявилась бы там, где
        коммита не было вовсе. ``None`` (не git, HEAD не прочитан) — отметки
        нет, ступени HEAD у пути не будет.

        Отметка переписывается ВМЕСТЕ со снимком и только с ним: no-op
        (снимок уже есть) чужую отметку не трогает, а новый снимок затирает
        отметку прежнего — иначе после эвикции и повторного съёма ревизия
        осталась бы от снимка, которого давно нет.

        Поверх СЧЁТНОЙ базы (``.gitorig``) точная не пишется: счётная значит,
        что путь уже правила шелл-команда без точного «до» (путь был грязен), и
        снятое позже — это состояние ПОСЛЕ команды. Записанное в ``.orig``, оно
        выдало бы промежуточное состояние за начало сессии, и откат «к началу»
        оставил бы правки команды на диске. Правило здесь, а не у канала, —
        одно для рекордера и для шелл-канала.
        """
        dest = self._session_path(path_key)
        if self._shell_session_path(path_key).is_file():
            return False
        if not self._publish_once(dest, content):
            return False
        self._write_session_head_rev(path_key, head_rev)
        return True

    def _write_session_head_rev(self, path_key: str, head_rev: str | None) -> None:
        self._mark(path_key, {_SESSION_HEAD_REV_KEY: head_rev})

    def mark_created(self, path_key: str, *, head_rev: str | None) -> None:
        """Отметить «файл создан здесь при ревизии ``head_rev``» (Д1).

        Отметка без файла байтов: у созданного пути «до» — пустота, и хранить
        её нечем (тот же приём, что у маркера ``accepted_absent``). Ревизия
        нужна затем же, зачем ``session_head_rev`` у ``.orig``: по ней лестница
        отсчёта видит, затронул ли путь коммит уже ПОСЛЕ создания. Безопасность
        не страдает — до создания файла не было, стирать нечего.

        Пишется ОДИН раз на путь (как ``capture_session``): повторное создание
        того же пути ревизию не двигает — победить должен самый ранний момент,
        от которого путь точно не существовал. ``head_rev is None`` (не git,
        HEAD не прочитан) — отметки нет; прежнюю такой вызов не трогает.

        Точный снимок у пути УЖЕ есть — отметки не будет вовсе: ``.orig`` лёг
        РАНЬШЕ и доказывает, что путь существовал (у созданного здесь пути
        порядок обратный — снимок появляется только со ВТОРОЙ правки, и
        отметку, поставленную до него, поздний ``capture_session`` не трогает).
        Так закрыта щель ``append_to_file``: существующему файлу нулевого
        размера тул ставит ``op="create"`` (там решается BOM, а не история
        пути), и путь, усечённый шелл-командой раньше в этой же сессии, отдал
        бы своё честное «до» и записанную с ним ревизию — коммит, случившийся
        между снимком и «созданием», по поздней отметке уже «не виден».
        """
        if (
            head_rev is None
            or self.created_head_rev(path_key) is not None
            or self.has_session_baseline(path_key)
        ):
            return
        self._mark(path_key, {_CREATED_HEAD_REV_KEY: head_rev})

    def created_head_rev(self, path_key: str) -> Optional[str]:
        """Ревизия HEAD в момент создания файла — или ``None`` (отметки нет).

        ``None`` значит «путь здесь не создавали (или ревизия не прочиталась)»:
        ступени HEAD по такому пути от отметки не будет.
        """
        value = self._index_entry(path_key).get(_CREATED_HEAD_REV_KEY)
        return value if isinstance(value, str) and value else None

    def capture_turn(self, path_key: str, content: bytes, turn_id: str) -> None:
        """Записать точный turn-baseline, если для ``turn_id`` он ещё не записан."""
        self._capture_turn_snapshot(self._turn_path(path_key), path_key, content, turn_id)

    def capture_shell_session(self, path_key: str, content: bytes) -> bool:
        """Записать СЧЁТНУЮ session-базу, только если её ещё нет. ``True`` — записали.

        Порода отдельная (см. докстринг модуля): откату эта база не подаётся
        никогда, поэтому и живёт в своём пространстве имён.
        """
        return self._publish_once(self._shell_session_path(path_key), content)

    def capture_shell_turn(self, path_key: str, content: bytes, turn_id: str) -> None:
        """Записать СЧЁТНУЮ turn-базу, если для ``turn_id`` она ещё не записана."""
        self._capture_turn_snapshot(
            self._shell_turn_path(path_key), path_key, content, turn_id
        )

    def _capture_turn_snapshot(
        self,
        dest: Path,
        path_key: str,
        content: bytes,
        turn_id: str,
    ) -> None:
        index_key = _TURN_INDEX_KEYS[dest.suffix]
        if self._pending is None:
            # Проверка отметки и запись — под ОДНИМ замком индекса: файл хода
            # перезаписывается каждым новым ходом, и «без перезаписи» тут не
            # годится. Порознь двое писателей одного пути видели бы оба «ещё
            # не снят», и опоздавший затёр бы базу хода состоянием после чужой
            # правки. Внутри `deferred_index` (пакет шелл-канала) замок на путь
            # вернул бы квадратичную цену — там однократность держит пакет.
            stem = self._stem(path_key)
            with file_lock(self.index_path, timeout=5.0):
                index = self._load_index()
                entry = index.get(stem)
                if _turn_taken(entry if isinstance(entry, dict) else {}, index_key, turn_id):
                    return
                self._write_bytes(dest, content)
                if _apply_marks(index, stem, {index_key: turn_id}):
                    self._save_index(index)
            return
        if _turn_taken(self._index_entry(path_key), index_key, turn_id):
            return
        self._write_bytes(dest, content)
        self._mark(path_key, {index_key: turn_id})

    def _index_entry(self, path_key: str) -> Dict[str, Any]:
        key = self._stem(path_key)
        entry = self._index_snapshot().get(key)
        entry = entry if isinstance(entry, dict) else {}
        pending = self._pending.get(key) if self._pending else None
        if pending:
            # Кэш индекса только для чтения (см. `_index_snapshot`) — накладываем на копию.
            view = {key: dict(entry)}
            _apply_marks(view, key, pending)
            entry = view.get(key, {})
        return entry

    def _index_snapshot(self) -> Dict[str, Any]:
        """Индекс целиком, с кэшем по подписи файла (размер + mtime).

        Читать его под блокировкой на КАЖДЫЙ запрос было дорого: одна пересборка
        вида делает порядка четырёх обращений на путь, и замер дал 3,2 с на
        треде в 800 путей (200 путей — 477 мс). Цена в самой блокировке и
        разборе JSON, а не в объёме.

        Подпись файла ловит и чужую запись (другое окно, CLI): размер или mtime
        меняются, и кэш сбрасывается. Тот же приём уже применён к журналу правок
        в шелл-канале. Чтение по-прежнему берёт блокировку, но один раз на
        подпись, а не на каждый путь.

        Отданный словарь — ТОЛЬКО ДЛЯ ЧТЕНИЯ: он и есть кэш, а копия на каждый
        запрос съела бы ровно то, ради чего кэш заведён. Запись индекса идёт
        своим путём (``_load_index`` под замком → ``_save_index``) и кэш не
        мутирует, а сбрасывает.

        Подпись снимается ПОД замком, вместе с содержимым. Снятая до него, она
        при конкурентной записи легла бы ключом к уже другому содержимому;
        неверного чтения это не давало (следующий ``stat`` дал бы третью
        подпись и промах), но инвариант «ключ описывает то, что под ним лежит»
        лучше держать в коде, чем в рассуждении.
        """
        try:
            st = self.index_path.stat()
            signature = (st.st_size, st.st_mtime_ns)
        except OSError:
            return {}
        if self._index_cache is not None and self._index_cache[0] == signature:
            return self._index_cache[1]
        with file_lock(self.index_path, timeout=5.0):
            index = self._load_index()
            try:
                st = self.index_path.stat()
                signature = (st.st_size, st.st_mtime_ns)
            except OSError:
                return index          # файл унесли — кэшировать нечего
        self._index_cache = (signature, index)
        return index

    # --- baseline reads ---

    def session_baseline(self, path_key: str) -> Optional[bytes]:
        """ТОЧНАЯ база сессии — единственная, что годится откату."""
        p = self._session_path(path_key)
        if not p.is_file():
            return None
        return p.read_bytes()

    def turn_baseline(self, path_key: str, turn_id: str) -> Optional[bytes]:
        """ТОЧНАЯ база хода — единственная, что годится откату."""
        if self._index_entry(path_key).get("turn_id") != turn_id:
            return None
        p = self._turn_path(path_key)
        if not p.is_file():
            return None
        return p.read_bytes()

    def counting_session_baseline(self, path_key: str) -> Optional[bytes]:
        """База для СЧЁТЧИКОВ: из ``.orig`` и ``.gitorig`` — записанная РАНЬШЕ.

        Сравнение по ``st_mtime_ns``; есть только один файл — берётся он; нет
        ни одного — ``None``. Почему не «точная в приоритете» — см. докстринг
        модуля. Откату этот метод не подаётся: он умеет отдать индекс git.
        """
        return _read_earlier(
            self._session_path(path_key), self._shell_session_path(path_key)
        )

    def counting_turn_baseline(self, path_key: str, turn_id: str) -> Optional[bytes]:
        """То же для хода: более ранняя из двух баз ЭТОГО хода.

        Каждая порода отдаётся, только если её отметка в индексе принадлежит
        запрошенному ходу: иначе протухшая база чужого хода выглядела бы
        точной и Р12 нарушился бы молча.
        """
        entry = self._index_entry(path_key)
        candidates = [
            path
            for path, index_key in (
                (self._turn_path(path_key), "turn_id"),
                (self._shell_turn_path(path_key), "shell_turn_id"),
            )
            if entry.get(index_key) == turn_id
        ]
        return _read_earlier(*candidates)

    def has_session_baseline(self, path_key: str) -> bool:
        """Есть ли ТОЧНЫЙ снимок сессии — то есть доступен ли откат."""
        return self._session_path(path_key).is_file()

    def session_head_rev(self, path_key: str) -> Optional[str]:
        """Ревизия HEAD, записанная при съёме ``.orig`` — или ``None``.

        ``None`` значит «признака нет»: ступень HEAD по такому пути не
        строится вовсе (см. ``capture_session``). Так же деградируют снимки,
        снятые версией без отметки, — безопасная сторона: лишние строки на
        экране вместо записи содержимого коммита поверх работы пользователя.
        """
        value = self._index_entry(path_key).get(_SESSION_HEAD_REV_KEY)
        return value if isinstance(value, str) and value else None

    def has_turn_baseline(self, path_key: str, turn_id: str) -> bool:
        """Есть ли ТОЧНЫЙ снимок этого хода — то есть доступен ли откат."""
        return (
            self._index_entry(path_key).get("turn_id") == turn_id
            and self._turn_path(path_key).is_file()
        )

    # --- accepted ---

    def capture_accepted(
        self, path_key: str, content: bytes | None, *, head_oid: str | None
    ) -> None:
        """Записать принятую базу. В ОТЛИЧИЕ от ``capture_session``/``capture_turn``
        ВСЕГДА перезаписывает прежнее принятие — повторное принятие заменяет базу.

        ``content=None`` — принято явное «отсутствие файла»: только маркер в
        ``index.json`` (``accepted_absent``), а не голое отсутствие байтового
        файла. Прежний байтовый снимок, если принятие заменяет снимок «с
        содержимым» на маркер, удаляется, чтобы не остаться сиротой.

        Байты меняются под тем же замком и ПОСЛЕ индекса. Раньше они удалялись
        первыми, и таймаут замка оставлял индекс «принято с содержимым» без
        самих байтов: ступень терялась, откат уходил ниже неё, а экран говорил
        «не изменилось». Теперь упавший индекс не меняет ничего. Упавшие после
        него байты откатывают и индекс: прежнее принятие удалением не имеет
        байтов, и новый индекс «с содержимым» без них потерял бы ступень так же.
        Итог один — либо принятие целиком, либо ничего.
        """
        dest = self._accepted_path(path_key)
        staged = None if content is None else self._stage_bytes(dest, content)
        key = self._stem(path_key)
        try:
            with file_lock(self.index_path, timeout=5.0):
                index = self._load_index()
                current = index.get(key)
                # Ключи принятой базы делят запись индекса с turn-отметками того
                # же стема: перезапись словарём целиком осиротила бы их файлы.
                merged = dict(current) if isinstance(current, dict) else {}
                merged["accepted_ts"] = time.time()
                merged["accepted_absent"] = content is None
                merged["accepted_head_oid"] = head_oid
                index[key] = merged
                self._save_index(index)
                try:
                    if staged is None:
                        dest.unlink(missing_ok=True)
                    else:
                        atomic_replace(staged, dest)
                        staged = None
                except OSError:
                    if current is None:
                        index.pop(key, None)
                    else:
                        index[key] = current
                    self._save_index(index)
                    raise
        finally:
            if staged is not None:
                _drop_quietly(staged)

    def accepted_state(self, path_key: str) -> AcceptedState | None:
        """Принятая база файла, или ``None`` — не принят.

        Контракт §3.1: запись «с содержимым» валидна только при живом файле
        байтов; ``accepted_ts`` без файла и без маркера отсутствия — «не
        принят» (защита от прочтения потерянного снимка как маркера — иначе
        откат удалил бы живой файл).

        ЖИВОЙ файл байтов важнее маркера отсутствия. ``capture_accepted`` меняет
        байты после индекса и при отказе откатывает индекс, но двух файлов
        атомарно не сменить: процесс, прерванный между ними (или неудавшийся
        сам откат), оставляет живые байты рядом с ``accepted_absent``.
        Прочитать эту пару как «принято отсутствие» значило бы предложить
        пользователю УДАЛИТЬ файл, которого никто удалять не просил; порядок
        проверок — защита от этого окна.
        """
        entry = self._index_entry(path_key)
        ts = entry.get("accepted_ts")
        if not isinstance(ts, (int, float)):
            return None
        head_oid = entry.get("accepted_head_oid")
        p = self._accepted_path(path_key)
        if p.is_file():
            return AcceptedState(content=p.read_bytes(), ts=float(ts), head_oid=head_oid)
        if entry.get("accepted_absent"):
            return AcceptedState(content=None, ts=float(ts), head_oid=head_oid)
        return None

    def clear_accepted(self, path_key: str) -> bool:
        """Снять принятие: удалить байтовый снимок (если есть) и ключи индекса.

        ``True`` — было что чистить (снимок или запись в индексе).

        Индекс — первым, под замком, байты — после него (см. ``capture_accepted``):
        таймаут замка больше не оставляет «снято» при экране «не снято». Байты,
        не удалившиеся уже после индекса, — сирота без записи: принятие снято
        по смыслу (``accepted_state`` без ``accepted_ts`` — «не принят»), их
        уберёт вытеснение, и отказом снятия это не считается.
        """
        dest = self._accepted_path(path_key)
        key = self._stem(path_key)
        had_index = False
        with file_lock(self.index_path, timeout=5.0):
            index = self._load_index()
            entry = index.get(key)
            if isinstance(entry, dict):
                for index_key in _ACCEPTED_INDEX_KEYS:
                    if index_key in entry:
                        del entry[index_key]
                        had_index = True
                if had_index:
                    if entry:
                        index[key] = entry
                    else:
                        del index[key]
                    self._save_index(index)
            had_file = dest.is_file()
            if had_file:
                _drop_quietly(dest)
        return had_file or had_index

    # --- eviction ---

    def evict_lru(self, max_files: int) -> None:
        """Вытеснить самые старые снапшоты сверх ``max_files``.

        Снапшоты треда с живым журналом (``edits_<thread_key>.jsonl`` в
        ``threads_dir``) не трогаем. Legacy-файлы (голый ``<sha>.orig`` без
        ``_<thread_key>``) — кандидаты на удаление первыми, независимо от mtime.

        Счётные базы (``.gitorig``/``.gitturn``) считаются наравне с точными:
        они лежат в том же каталоге и растят его так же.

        Здесь же умирают записи индекса мёртвых тредов, за которыми файлов не
        осталось вовсе (см. ``_prune_dead_marks``) — иначе отметка создания,
        у которой файла нет по построению, пережила бы свой тред навсегда.
        """
        if not self.root.is_dir():
            return
        files = [
            p for p in self.root.iterdir()
            if p.is_file() and p.suffix in _SNAPSHOT_SUFFIXES
        ]
        self._prune_dead_marks(files)
        # Ноль — «не вытеснять» (`ctx._MINIMUMS`), но уборка мёртвых отметок
        # выше — не вытеснение, и ноль её не отключает.
        if max_files <= 0:
            return
        overflow = len(files) - max_files
        if overflow <= 0:
            return

        legacy: list[Path] = []
        evictable: list[Path] = []
        for p in files:
            m = _STEM_RE.match(p.stem)
            if m is None:
                legacy.append(p)
            elif not self._has_live_journal(m.group(1)):
                evictable.append(p)
        legacy.sort(key=lambda p: p.stat().st_mtime)
        evictable.sort(key=lambda p: p.stat().st_mtime)

        for p in (legacy + evictable)[:overflow]:
            self._evict_one(p)

    def purge_thread(self) -> int:
        """Снести снимки и записи индекса ЭТОГО треда. Возвращает число файлов.

        Зовётся при сбросе треда (``thread_store.reset_thread_files``). Без неё
        сброс уносил журнал, но не базы: ключ снимков — ``system_id_thread_id``,
        сброс его не меняет, а ``capture_session`` пишет только при отсутствии.
        Новая сессия брала бы за «до» состояние прошлой, и откат записал бы его
        поверх работы, сделанной пользователем между сессиями, — молча (пятый
        раунд ревью, находка 1).

        От эвикции отличается тем, что живой журнал ей не помеха: журнал этого
        треда как раз и удаляют рядом. Чужие треды и legacy-стемы (без
        ``_<thread_key>``) не трогает — у последних тред неизвестен.
        """
        if not self.root.is_dir():
            return 0
        removed = 0
        for p in sorted(self.root.iterdir()):
            if not p.is_file() or p.suffix not in _SNAPSHOT_SUFFIXES:
                continue
            match = _STEM_RE.match(p.stem)
            if match is None or match.group(1) != self.thread_key:
                continue
            try:
                p.unlink()
            except OSError:
                continue
            removed += 1
        with file_lock(self.index_path, timeout=5.0):
            index = self._load_index()
            dead = [
                key for key in index
                if (match := _STEM_RE.match(key)) is not None
                and match.group(1) == self.thread_key
            ]
            if dead:
                for key in dead:
                    del index[key]
                self._save_index(index)
        return removed

    def _prune_dead_marks(self, files: list[Path]) -> None:
        """Записи индекса мёртвых тредов, за которыми не осталось ни файла.

        Файловая эвикция чистит индекс через ``_evict_one`` — по вынесенному
        файлу. Отметка создания файла не имеет вовсе (Д1), и без этой уборки
        она осталась бы в ``index.json`` навсегда, хотя тред давно удалён.

        Запись, за которой ФАЙЛ ЕЩЁ ЕСТЬ, не трогаем, даже у мёртвого треда:
        снимок, переживший вытеснение, потерял бы вместе с записью свою
        turn-отметку и перестал бы отдаваться. Legacy-стемы (без
        ``_<thread_key>``) не разбираются: тред у них неизвестен.
        """
        stems = {p.stem for p in files}
        with file_lock(self.index_path, timeout=5.0):
            index = self._load_index()
            dead = [
                key for key in index
                if key not in stems
                and (match := _STEM_RE.match(key)) is not None
                and not self._has_live_journal(match.group(1))
            ]
            if not dead:
                return
            for key in dead:
                del index[key]
            self._save_index(index)

    def _has_live_journal(self, thread_key: str) -> bool:
        if self.threads_dir is None:
            return True
        return (self.threads_dir / f"edits_{thread_key}.jsonl").exists()

    def _evict_one(self, p: Path) -> None:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            return
        if p.suffix == ".accepted":
            keys: tuple = _ACCEPTED_INDEX_KEYS
        elif p.suffix == ".orig":
            keys = (_SESSION_HEAD_REV_KEY,)
        else:
            turn_key = _TURN_INDEX_KEYS.get(p.suffix)
            keys = (turn_key,) if turn_key is not None else ()
        if not keys:
            return
        key = p.stem
        with file_lock(self.index_path, timeout=5.0):
            index = self._load_index()
            entry = index.get(key)
            if not isinstance(entry, dict):
                return
            # Убираем отметки только вынесенной породы: другие могут быть живы.
            changed = False
            for index_key in keys:
                if index_key in entry:
                    del entry[index_key]
                    changed = True
            if not changed:
                return
            if entry:
                index[key] = entry
            else:
                del index[key]
            self._save_index(index)
