"""Журнал правок агента: append-only JSONL рядом с тредом.

Модель одной правки (``EditRecord``) и I/O поверх файла (``EditsJournal``).
Доменный слой данных: без UI, без чтения конфигов, без git — пишут
fs-тулы, читают трейс и экран /diff (в других модулях).

Форма поля ``hunks`` зависит от КАНАЛА записи — валидны обе, а признак канала
— ``call_id`` (у шелл-делты он всегда ``None``), а не ``op``: запись
``op="create"`` пишут оба канала:

* рекордер fs-тулов (``call_id`` заполнен; ``op`` = ``edit``/``write``/
  ``append``/``create``/``delete``) — вывод ``difflib.unified_diff``: пустые
  шапки ``--- ``/``+++ `` первыми двумя строками и дальше ``@@``-хэнки; путь
  файла внутри диффа не упоминается вообще (он в поле ``path``);
* шелл-делта (``call_id is None``; ``op="shell"``, а у файла, созданного самой
  командой, — ``op="create"``) — полный вывод ``git diff``: заголовок
  ``diff --git a/<path> b/<path>``, строки ``index``/``--- a/``/``+++ b/``
  (для созданного файла — ``--- /dev/null``) и только потом ``@@``-хэнки.

Карманный подканал того же шелл-канала (``call_id is None`` так же) даёт
гибрид: синтетический заголовок ``diff --git a/<path> b/<path>`` без
``index``/``--- a/``/``+++ b/``, а дальше — тело ``difflib.unified_diff``, как
у рекордера. Запись удаления со строками (оба подканала,
``delete_records.delete_record``) — в форме git: ``diff --git``,
``deleted file mode``, ``--- a/<path>``, ``+++ /dev/null`` и тело difflib.

У любой записи с флагом ``no_diff`` поле ``hunks`` пусто. Род содержимого —
именно ФЛАГ, а не вид правки: ``op`` отвечает на вопрос «что сделал тул»
(создал, дописал, заменил, удалил), и затирать его бинарностью нельзя — вместе
с видом правки терялось создание файла, а с ним и право на откат удалением
(пятый раунд ревью). ``op="binary"`` встречается в журналах, написанных прежним
кодом, и читатели обязаны понимать оба варианта.

Ключ ``path`` бывает ДВУХ форм, и обе валидны: относительный POSIX у файла
внутри проекта, абсолютный — у файла вне его (``agent_edits.paths.canonical_path_key``).
Прежде журнал физически не мог содержать абсолютный ключ, и потребители
опирались на это молча — так карманный подканал шелл-делты чуть не начал
снимать точные базы чужих файлов. Теперь правило явное: **журнал МОЖЕТ
содержать внешние ключи, и потребитель, которому нужна база, обязан их
отфильтровать** (``agent_edits.paths.is_external_key``). Исключение одно и
намеренное — индекс по ``call_id`` для ленты: внешние записи он видеть ДОЛЖЕН,
блок диффа базы не требует.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gc_core.utils.file_lock import file_lock

_logger = logging.getLogger(__name__)

# Флаг записи шелл-канала: путь, который команда создала и сама закоммитила.
# ``op`` у такой записи ``"shell"``: ``"create"`` дал бы откат удалением, а
# коммит в окне команды мог быть чужим. Флаг нужен только глаголу экрана.
CREATED_FLAG = "created"


@dataclass
class EditRecord:
    """Одна запись о правке файла в рамках хода (turn).

    ``path`` — ключ ``canonical_path_key`` ДВУХ форм: относительный POSIX
    внутри проекта или абсолютный POSIX вне его; внешний ключ распознаёт
    ``agent_edits.paths.is_external_key`` (см. докстринг модуля).
    """

    path: str
    turn_id: str
    ts: float
    call_id: Optional[str]
    op: str  # edit|write|append|create|delete|shell (binary — legacy, см. выше)
    hunks: str  # difflib-форма; у шелл-канала (call_id is None) — git-форма
    add: int
    del_: int
    before_sha256: Optional[str]
    after_sha256: Optional[str]
    flags: List[str]
    # Сколько путей тронула шелл-команда, если их больше потолка записи
    # (``shell_delta.MAX_PATHS_PER_COMMAND``) — стоит на каждой записи такой
    # команды. По нему экран изменений признаётся, что список неполон: пути
    # сверх потолка не записаны, не видны и не откатываются. ``None`` — команда
    # не урезана (и все записи журналов до поля).
    command_paths: Optional[int] = None
    # Вызов команды, которая тронула файл (шелл-канал). ``call_id`` у таких
    # записей по-прежнему ``None`` — признак канала; это поле лишь говорит,
    # какой вызов команды сделал правку. ``None`` — запись fs-тула, команда
    # вне вызова тула или журнал до поля.
    command_call_id: Optional[str] = None
    # Размер файла после правки у записи без диффа (флаг ``no_diff``: бинарь
    # или файл сверх капа исходника). ``None`` — запись с диффом, удаление,
    # файл пропал между пробами или журнал до поля.
    size_bytes: Optional[int] = None
    # Режим файла (``stat.S_IMODE``) у записи удаления: по нему откат
    # восстанавливает права. ``None`` — не удаление, режим неизвестен или
    # журнал до поля.
    mode: Optional[int] = None

    def to_json_obj(self) -> Dict[str, Any]:
        obj = {
            "path": self.path,
            "turn_id": self.turn_id,
            "ts": self.ts,
            "call_id": self.call_id,
            "op": self.op,
            "hunks": self.hunks,
            "add": self.add,
            "del": self.del_,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "flags": list(self.flags),
        }
        if self.command_paths is not None:
            obj["command_paths"] = self.command_paths
        if self.command_call_id is not None:
            obj["command_call_id"] = self.command_call_id
        if self.size_bytes is not None:
            obj["size_bytes"] = self.size_bytes
        if self.mode is not None:
            obj["mode"] = self.mode
        return obj

    @classmethod
    def from_json_obj(cls, obj: Dict[str, Any]) -> EditRecord:
        return cls(
            path=obj.get("path", ""),
            turn_id=obj.get("turn_id", ""),
            ts=obj.get("ts", 0.0),
            call_id=obj.get("call_id"),
            op=obj.get("op", ""),
            hunks=obj.get("hunks", ""),
            add=obj.get("add", 0),
            del_=obj.get("del", 0),
            before_sha256=obj.get("before_sha256"),
            after_sha256=obj.get("after_sha256"),
            flags=list(obj.get("flags") or []),
            command_paths=obj.get("command_paths"),
            command_call_id=obj.get("command_call_id"),
            size_bytes=obj.get("size_bytes"),
            mode=obj.get("mode"),
        )


class EditsJournal:
    """Append-only JSONL журнал правок одного треда.

    Хранит записи обеих форм ключа — см. докстринг модуля: читатель, которому
    нужна база файла, обязан отсеять внешние ключи сам.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._cache_key: Optional[Tuple[int, int]] = None
        self._cache_records: List[EditRecord] = []

    def append(self, rec: EditRecord) -> None:
        with file_lock(self._path, timeout=5.0):
            with open(self._path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    json.dumps(rec.to_json_obj(), ensure_ascii=False) + "\n"
                )

    def read_all(self) -> List[EditRecord]:
        try:
            st = self._path.stat()
        except OSError:
            self._cache_key = None
            self._cache_records = []
            return []
        key = (st.st_size, st.st_mtime_ns)
        if key != self._cache_key:
            records = self._read_from_disk()
            if records is None:
                # Чтение не удалось по причине, которая пройдёт сама (антивирус
                # держит файл, кончились дескрипторы). Кэш НЕ трогаем: ключ
                # кэша — размер и время файла, и запомнив под ним пустоту, мы
                # отдавали бы пустой журнал до следующей правки — хотя файл цел
                # и полон. Отдаём последнее удачно прочитанное.
                return list(self._cache_records)
            self._cache_records = records
            self._cache_key = key
        return list(self._cache_records)

    def _read_from_disk(self) -> Optional[List[EditRecord]]:
        """Записи с диска; ``None`` — прочитать не удалось, и это не пустота.

        Исчезнувший журнал — законный ПУСТОЙ набор, а не падение: его сносят
        из-под читателя (``gc-cli -t X -n``, второе окно TUI), а между
        проверкой и открытием окно есть всегда, и наружу это вылетало в экран
        изменений, где ловить некому.

        Прочие отказы (файл занят, кончились дескрипторы) пустотой НЕ являются:
        журнал цел и полон, прочитать его мы не смогли сейчас. Разница видна
        вызывающему, потому что он кэширует результат по размеру и времени
        файла — запомнив под этим ключом пустоту, он отдавал бы её до
        следующей правки.
        """
        records: List[EditRecord] = []
        try:
            with open(self._path, encoding="utf-8") as fh:
                for line in fh:
                    row = line.strip()
                    if not row:
                        continue
                    try:
                        obj = json.loads(row)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    records.append(EditRecord.from_json_obj(obj))
        except FileNotFoundError:
            return []  # журнал снесли — законная пустота
        except OSError:
            _logger.warning(
                "журнал правок не прочитан: %s", self._path, exc_info=True
            )
            return None  # прочесть не смогли — пустотой это не считается
        return records

    def last_turn_id(self) -> Optional[str]:
        records = self.read_all()
        return records[-1].turn_id if records else None

    def turn_records(self, turn_id: str) -> List[EditRecord]:
        return [r for r in self.read_all() if r.turn_id == turn_id]
