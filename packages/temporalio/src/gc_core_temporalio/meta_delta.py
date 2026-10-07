"""Meta треда в истории Temporal — разницей, как сообщения — ссылками.

Meta треда (журнал намерений, текущий запрос, разделы сессии) хранилище
пишет и отдаёт целиком. Под Temporal каждая запись и каждый ответ —
запись истории (лимит 2 МБ), а журнал растёт с каждым ходом. Поэтому:

* запись несёт разницу с последней известной workflow meta: изменённые
  ключи, хвост списков с номера N (повтор поверх уже записанной meta даёт
  то же — номер абсолютный), длинные строки — ссылкой на текст сообщения
  (той же операции или треда) со сверкой хэша;
* квитанция несёт разницу между тем, что workflow записал, и тем, что
  хранилище сохранило (обычно пусто);
* чтение отдаёт meta частями, если она не помещается в страницу.

Хранилище и ядро получают и отдают полную meta — как в локальном режиме:
воркер собирает операцию целиком до вызова хранилища.

Модуль общий для workflow и воркера: только ``hashlib`` и ``json``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Iterable

from gc_core_temporalio.message_refs import message_hash

#: Ключ ссылки на текст сообщения вместо строки:
#: ``{"__ua_text__": [хэш сообщения, путь, начало, длина, хэш текста]}``.
TEXT_REF_KEY = "__ua_text__"
#: Ключ ссылки на строку, которая уже есть в meta-базе (запрос пользователя
#: переезжает из ``current_user_intent`` в журнал, сжатие пересобирает
#: журнал): ``{"__ua_meta_text__": хэш текста}``. Поиск по содержимому, а не
#: по месту: при повторе записи база — уже записанная meta, где текст есть
#: (он часть новой meta), хотя бы и в другом месте.
META_REF_KEY = "__ua_meta_text__"
#: Короче — строка идёт как есть: ссылка не дешевле.
TEXT_REF_MIN_CHARS = 1024

#: Разница: ``None`` — без изменений; ``{"=": v}`` — новое значение;
#: ``{"+": [n, хвост]}`` — список ``old[:n] + хвост``;
#: ``{"{": {"set", "drop", "sub", "order"}}`` — словарь по ключам (``order`` —
#: набор ключей верхнего уровня, см. ``diff_meta``; порядок не передаётся).
Delta = dict[str, Any] | None


class MetaDeltaError(ValueError):
    """Разницу не собрать: база не та или ссылка не сходится."""


def normalize(value: Any) -> Any:
    """Значение в том виде, в каком его увидит воркер (JSON Temporal):
    кортежи — списками, ключи — строками. Иначе сравнение с базой видело бы
    «изменение» там, где после передачи его нет."""
    from temporalio.converter import AdvancedJSONEncoder

    return json.loads(
        json.dumps(value, cls=AdvancedJSONEncoder, ensure_ascii=False)
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _text_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _strings(value: Any, path: tuple = ()) -> Iterable[tuple[tuple, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


class TextIndex:
    """Длинные тексты (meta-базы и сообщений), на которые разница может
    сослаться."""

    def __init__(self) -> None:
        self._exact: dict[str, tuple[str, list, int]] = {}
        self._texts: list[tuple[str, list, str]] = []
        self._meta: set[str] = set()
        #: Индексы, где искать после своего (тексты треда за текстами
        #: операции).
        self.parents: list[TextIndex] = []

    def add_meta(self, meta: Any) -> None:
        for _path, text in _strings(meta):
            if len(text) >= TEXT_REF_MIN_CHARS:
                self._meta.add(text)

    def add(self, messages: Iterable[Any]) -> None:
        for message in messages or ():
            digest = None
            for path, text in _strings(message):
                if len(text) < TEXT_REF_MIN_CHARS or text in self._exact:
                    continue
                digest = digest or message_hash(message)
                self._exact[text] = (digest, list(path), 0)
                self._texts.append((digest, list(path), text))

    def ref(self, text: str) -> dict[str, Any] | None:
        if len(text) < TEXT_REF_MIN_CHARS:
            return None
        if text in self._meta:
            return {META_REF_KEY: _text_id(text)}
        found = self._exact.get(text)
        if found is None:
            # Запрос пользователя в журнале — текст сообщения без пробелов
            # по краям: часть строки, а не вся строка.
            for digest, path, source in self._texts:
                start = source.find(text)
                if start >= 0:
                    found = (digest, path, start)
                    break
        if found is None:
            for parent in self.parents:
                ref = parent.ref(text)
                if ref is not None:
                    return ref
            return None
        digest, path, start = found
        return {TEXT_REF_KEY: [digest, path, start, len(text), _digest(text)]}


def _encode(value: Any, texts: TextIndex | None) -> Any:
    if texts is None:
        return value
    if isinstance(value, str):
        return texts.ref(value) or value
    if isinstance(value, dict):
        return {key: _encode(item, texts) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item, texts) for item in value]
    return value


def _same(left: Any, right: Any) -> bool:
    """Равенство в JSON-смысле: ``1``, ``1.0`` и ``True`` — разные значения,
    а порядок ключей словаря — не важен (JSON-конвертер Temporal и SQLite
    сортируют ключи, ядро пишет в своём порядке)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_same, left, right))
    return left == right


def diff(old: Any, new: Any, texts: TextIndex | None = None) -> Delta:
    """Разница ``old`` → ``new`` (оба — уже JSON-вид, см. ``normalize``)."""
    if _same(old, new):
        return None
    if isinstance(old, dict) and isinstance(new, dict):
        drop = [key for key in old if key not in new]
        added = {
            key: _encode(value, texts)
            for key, value in new.items()
            if key not in old
        }
        sub = {
            key: change
            for key, value in new.items()
            if key in old
            and (change := diff(old[key], value, texts)) is not None
        }
        body: dict[str, Any] = {}
        if added:
            body["set"] = added
        if drop:
            body["drop"] = drop
        if sub:
            body["sub"] = sub
        return {"{": body}
    if isinstance(old, list) and isinstance(new, list):
        common = 0
        for left, right in zip(old, new):
            if not _same(left, right):
                break
            common += 1
        if common:
            return {"+": [common, _encode(new[common:], texts)]}
    return {"=": _encode(new, texts)}


def diff_meta(old: dict, new: dict, texts: TextIndex | None = None) -> Delta:
    """Разница meta для записи.

    Хранилище при сохранении само меняет мелкие ключи верхнего уровня
    (``updated_at``, версии схемы, ``created_at``). Повтор записи поверх уже
    сохранённой meta должен собрать ту же операцию (иначе хранилище не узнает
    её по ``operation_id``): поэтому скаляры верхнего уровня идут целиком, а
    набор и порядок ключей — явным списком. Разделы (журнал намерений,
    ``shared``, ``runtime``…) — разницей: ключи, которых workflow не менял
    (кэш ``fetch_url`` локального режима), остаются как в хранилище.
    """
    added: dict[str, Any] = {}
    sub: dict[str, Any] = {}
    for key, value in new.items():
        if key in old and isinstance(value, (dict, list)):
            change = diff(old[key], value, texts)
            if change is not None:
                sub[key] = change
        else:
            added[key] = _encode(value, texts)
    body: dict[str, Any] = {"order": list(new)}
    if added:
        body["set"] = added
    if sub:
        body["sub"] = sub
    return {"{": body}


class TextSource:
    """Сторона воркера: откуда брать тексты по ссылкам разницы —
    сообщения по хэшу и строки meta-базы по хэшу содержимого."""

    def __init__(
        self,
        messages: Callable[[str], Any] | None = None,
        meta: Any = None,
    ) -> None:
        self._messages = messages
        self._meta = {
            _text_id(text): text
            for _path, text in _strings(meta)
            if len(text) >= TEXT_REF_MIN_CHARS
        }

    def meta_text(self, text_id: str) -> str:
        text = self._meta.get(text_id)
        if text is None:
            raise MetaDeltaError("Текста meta нет в базе разницы")
        return text

    def message_text(self, ref: list) -> str:
        digest, path, start, length, text_digest = ref
        message = self._messages(digest) if self._messages else None
        if message is None:
            raise MetaDeltaError("Сообщение для текста meta не найдено")
        value = message
        try:
            for step in path:
                value = value[step]
        except (KeyError, IndexError, TypeError):
            raise MetaDeltaError(
                "Путь текста meta не найден в сообщении"
            ) from None
        text = (
            value[start : start + length] if isinstance(value, str) else None
        )
        if text is None or _digest(text) != text_digest:
            raise MetaDeltaError("Текст meta не совпал с сообщением")
        return text


def _decode(value: Any, texts: TextSource | None) -> Any:
    if isinstance(value, dict):
        if len(value) == 1 and TEXT_REF_KEY in value:
            return (texts or TextSource()).message_text(value[TEXT_REF_KEY])
        if len(value) == 1 and META_REF_KEY in value:
            return (texts or TextSource()).meta_text(value[META_REF_KEY])
        return {key: _decode(item, texts) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item, texts) for item in value]
    return value


def apply(old: Any, delta: Delta, texts: TextSource | None = None) -> Any:
    """``old`` + разница → новое значение (``old`` не меняется)."""
    if delta is None:
        return old
    if "=" in delta:
        return _decode(delta["="], texts)
    if "+" in delta:
        if not isinstance(old, list):
            raise MetaDeltaError("Хвост списка meta поверх не-списка")
        common, tail = delta["+"]
        if len(old) < common:
            raise MetaDeltaError("Список meta короче базы разницы")
        return old[:common] + _decode(tail, texts)
    body = delta["{"]
    if not isinstance(old, dict):
        raise MetaDeltaError("Разница словаря meta поверх не-словаря")
    result = dict(old)
    for key in body.get("drop", ()):
        result.pop(key, None)
    for key, change in body.get("sub", {}).items():
        if key not in result:
            raise MetaDeltaError("Ключа meta нет в базе разницы")
        result[key] = apply(result[key], change, texts)
    for key, value in body.get("set", {}).items():
        result[key] = _decode(value, texts)
    order = body.get("order")
    if order is not None:
        if any(key not in result for key in order):
            raise MetaDeltaError("Ключа meta нет после разницы")
        result = {key: result[key] for key in order}
    return result


def canonical(value: Any) -> Any:
    """Тот же JSON с ключами по алфавиту на всех уровнях: так meta отдаётся
    хранилищу (как и раньше через конвертер Temporal) — сборка из разницы не
    зависит от порядка ключей базы."""
    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def meta_hash(meta: Any) -> str:
    """Хэш meta без порядка ключей и без кэша ``fetch_url`` (он живёт в meta
    хранилища, а workflow видит его пустым): итог сборки на воркере сверяется
    с тем, что записал workflow."""
    if isinstance(meta, dict) and isinstance(meta.get("shared"), dict):
        shared = {
            key: value
            for key, value in meta["shared"].items()
            if key != "fetch_url_cache"
        }
        meta = {**meta, "shared": shared}
    encoded = json.dumps(
        meta, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def text_refs(delta: Any) -> set[str]:
    """Хэши сообщений, на тексты которых ссылается разница."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if len(value) == 1 and TEXT_REF_KEY in value:
                found.add(value[TEXT_REF_KEY][0])
                return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(delta)
    return found


__all__ = [
    "META_REF_KEY",
    "TEXT_REF_KEY",
    "TEXT_REF_MIN_CHARS",
    "MetaDeltaError",
    "TextIndex",
    "TextSource",
    "apply",
    "canonical",
    "diff",
    "diff_meta",
    "meta_hash",
    "normalize",
    "text_refs",
]
