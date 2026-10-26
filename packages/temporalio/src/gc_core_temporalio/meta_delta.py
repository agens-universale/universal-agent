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
#: Ключ ссылки на целое поддерево meta-базы (запись журнала, раздел) по хэшу
#: содержимого: ``{"__ua_meta_node__": хэш}`` — для записи полной meta
#: (запасной путь), где ссылаться можно только на содержимое, не на место.
NODE_REF_KEY = "__ua_meta_node__"
#: Настоящий словарь meta из одного служебного ключа (совпал с ключом
#: ссылки): в разнице — ``{"__ua_literal__": словарь}``, сборка не примет его
#: за ссылку.
LITERAL_KEY = "__ua_literal__"
_SERVICE_KEYS = frozenset({TEXT_REF_KEY, META_REF_KEY, NODE_REF_KEY})
#: Строки meta-базы короче — без ссылки (ссылка ~55 байт).
META_TEXT_MIN_CHARS = 200
#: Поддеревья легче — без ссылки.
NODE_REF_MIN_BYTES = 256
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


def _node_text(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )


def _nodes(value: Any) -> Iterable[tuple[str, Any]]:
    """Поддеревья (словари, списки) не легче ``NODE_REF_MIN_BYTES`` — с
    хэшем содержимого (без порядка ключей)."""
    if isinstance(value, (dict, list)):
        text = _node_text(value)
        if len(text) >= NODE_REF_MIN_BYTES:
            yield hashlib.sha256(text.encode()).hexdigest()[:32], value
            items = value.values() if isinstance(value, dict) else value
            for item in items:
                yield from _nodes(item)


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
        self._meta_min = TEXT_REF_MIN_CHARS
        self._nodes: set[str] = set()
        #: Индексы, где искать после своего (тексты треда за текстами
        #: операции).
        self.parents: list[TextIndex] = []

    def add_meta(self, meta: Any, *, nodes: bool = False) -> None:
        """Строки meta-базы; ``nodes`` — и целые поддеревья (запасной путь
        полной meta: короткие строки и записи журнала — тоже ссылками)."""
        if nodes:
            self._meta_min = META_TEXT_MIN_CHARS
            self._nodes.update(node_id for node_id, _node in _nodes(meta))
        for _path, text in _strings(meta):
            if len(text) >= self._meta_min:
                self._meta.add(text)

    def has_nodes(self) -> bool:
        return bool(self._nodes) or any(p.has_nodes() for p in self.parents)

    def node_ref(self, value: Any) -> dict[str, Any] | None:
        if not self._nodes:
            for parent in self.parents:
                ref = parent.node_ref(value)
                if ref is not None:
                    return ref
            return None
        text = _node_text(value)
        if len(text) < NODE_REF_MIN_BYTES:
            return None
        node_id = hashlib.sha256(text.encode()).hexdigest()[:32]
        return {NODE_REF_KEY: node_id} if node_id in self._nodes else None

    def add(self, messages: Iterable[Any]) -> None:
        for message in messages or ():
            digest = None
            for path, text in _strings(message):
                if len(text) < TEXT_REF_MIN_CHARS or text in self._exact:
                    continue
                digest = digest or message_hash(message)
                self._exact[text] = (digest, list(path), 0)
                self._texts.append((digest, list(path), text))

    def _parent_ref(self, text: str) -> dict[str, Any] | None:
        for parent in self.parents:
            ref = parent.ref(text)
            if ref is not None:
                return ref
        return None

    def _message_place(self, text: str) -> tuple[str, list, int] | None:
        """Где текст лежит в сообщениях индекса: хэш, путь, начало."""
        found = self._exact.get(text)
        if found is not None:
            return found
        # Запрос пользователя в журнале — текст сообщения без пробелов
        # по краям: часть строки, а не вся строка.
        for digest, path, source in self._texts:
            start = source.find(text)
            if start >= 0:
                return digest, path, start
        return None

    def ref(self, text: str) -> dict[str, Any] | None:
        if len(text) >= self._meta_min and text in self._meta:
            return {META_REF_KEY: _text_id(text)}
        found = (
            self._message_place(text)
            if len(text) >= TEXT_REF_MIN_CHARS
            else None
        )
        if found is None:
            return self._parent_ref(text)
        digest, path, start = found
        return {TEXT_REF_KEY: [digest, path, start, len(text), _digest(text)]}


def _service_like(value: Any) -> bool:
    """Словарь, который сборка приняла бы за служебный (ссылку, литерал)."""
    return (
        isinstance(value, dict)
        and len(value) == 1
        and next(iter(value)) in _SERVICE_KEYS | {LITERAL_KEY}
    )


def _escape(value: Any) -> Any:
    """Без индекса текстов: значение как есть, кроме служебных двойников."""
    if isinstance(value, dict):
        out = {key: _escape(item) for key, item in value.items()}
        return {LITERAL_KEY: out} if _service_like(value) else out
    if isinstance(value, list):
        return [_escape(item) for item in value]
    return value


def _encode_items(value: Any, texts: TextIndex) -> Any:
    if isinstance(value, dict):
        return {key: _encode(item, texts) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item, texts) for item in value]
    return value


def _encode(value: Any, texts: TextIndex | None) -> Any:
    if texts is None:
        return _escape(value)
    if _service_like(value):
        return {LITERAL_KEY: _encode_items(value, texts)}
    if isinstance(value, str):
        return texts.ref(value) or value
    if isinstance(value, (dict, list)) and texts.has_nodes():
        ref = texts.node_ref(value)
        if ref is not None:
            return ref
    return _encode_items(value, texts)


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


def _diff_dict(old: dict, new: dict, texts: TextIndex | None) -> dict:
    """Тело разницы словарей: ``set``/``drop``/``sub`` (пустые — без ключа)."""
    drop = [key for key in old if key not in new]
    added = {
        key: _encode(value, texts)
        for key, value in new.items()
        if key not in old
    }
    sub = {
        key: change
        for key, value in new.items()
        if key in old and (change := diff(old[key], value, texts)) is not None
    }
    parts = (("set", added), ("drop", drop), ("sub", sub))
    return {name: part for name, part in parts if part}


def _common_prefix(old: list, new: list) -> int:
    common = 0
    for left, right in zip(old, new):
        if not _same(left, right):
            break
        common += 1
    return common


def diff(old: Any, new: Any, texts: TextIndex | None = None) -> Delta:
    """Разница ``old`` → ``new`` (оба — уже JSON-вид, см. ``normalize``)."""
    if _same(old, new):
        return None
    if isinstance(old, dict) and isinstance(new, dict):
        return {"{": _diff_dict(old, new, texts)}
    if isinstance(old, list) and isinstance(new, list):
        common = _common_prefix(old, new)
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
        self._meta_source = meta
        self._meta = {
            _text_id(text): text
            for _path, text in _strings(meta)
            if len(text) >= META_TEXT_MIN_CHARS
        }
        self._node_index: dict[str, Any] | None = None

    def meta_node(self, node_id: str) -> Any:
        if self._node_index is None:
            self._node_index = dict(_nodes(self._meta_source))
        node = self._node_index.get(node_id)
        if node is None:
            raise MetaDeltaError("Поддерева meta нет в базе разницы")
        return json.loads(_node_text(node))

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


#: Ссылка разницы → метод ``TextSource``, который отдаёт её содержимое.
# Ключ ссылки → чтение из источника текстов (утиная типизация: подойдёт любой
# объект с этими методами, не только TextSource).
_REF_READERS = {
    TEXT_REF_KEY: lambda texts, ref: texts.message_text(ref),
    META_REF_KEY: lambda texts, ref: texts.meta_text(ref),
    NODE_REF_KEY: lambda texts, ref: texts.meta_node(ref),
}


def _decode_dict(value: dict, texts: TextSource | None) -> Any:
    if len(value) == 1:
        ((key, item),) = value.items()
        if key == LITERAL_KEY:
            return {name: _decode(part, texts) for name, part in item.items()}
        reader = _REF_READERS.get(key)
        if reader is not None:
            return reader(texts or TextSource(), item)
    return {key: _decode(item, texts) for key, item in value.items()}


def _decode(value: Any, texts: TextSource | None) -> Any:
    if isinstance(value, dict):
        return _decode_dict(value, texts)
    if isinstance(value, list):
        return [_decode(item, texts) for item in value]
    return value


def _apply_tail(old: Any, change: list, texts: TextSource | None) -> list:
    if not isinstance(old, list):
        raise MetaDeltaError("Хвост списка meta поверх не-списка")
    common, tail = change
    if len(old) < common:
        raise MetaDeltaError("Список meta короче базы разницы")
    return old[:common] + _decode(tail, texts)


def _apply_dict(old: Any, body: dict, texts: TextSource | None) -> dict:
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
    if order is None:
        return result
    if any(key not in result for key in order):
        raise MetaDeltaError("Ключа meta нет после разницы")
    return {key: result[key] for key in order}


def apply(old: Any, delta: Delta, texts: TextSource | None = None) -> Any:
    """``old`` + разница → новое значение (``old`` не меняется)."""
    if delta is None:
        return old
    if "=" in delta:
        return _decode(delta["="], texts)
    if "+" in delta:
        return _apply_tail(old, delta["+"], texts)
    return _apply_dict(old, delta["{"], texts)


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


def _walk_text_refs(value: Any, found: set[str]) -> None:
    if isinstance(value, list):
        items = value
    elif not isinstance(value, dict):
        return
    elif len(value) == 1 and LITERAL_KEY in value:
        # Сам литерал — данные; ссылки бывают только внутри значений.
        items = value[LITERAL_KEY].values()
    elif len(value) == 1 and TEXT_REF_KEY in value:
        found.add(value[TEXT_REF_KEY][0])
        return
    else:
        items = value.values()
    for item in items:
        _walk_text_refs(item, found)


def text_refs(delta: Any) -> set[str]:
    """Хэши сообщений, на тексты которых ссылается разница."""
    found: set[str] = set()
    _walk_text_refs(delta, found)
    return found


__all__ = [
    "LITERAL_KEY",
    "META_REF_KEY",
    "NODE_REF_KEY",
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
