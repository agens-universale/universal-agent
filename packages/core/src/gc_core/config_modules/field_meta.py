"""Метаданные полей настроек — одно место вместо разрозненных списков.

Флаги живут рядом с полем модели (``Field(..., json_schema_extra=gc_meta(...))``)
и попадают в JSON Schema под ключом ``x-gc``. Флаг секции наследуют все её
листья: секрет у ``agent_auth`` — значит, секрет каждое поле внутри.

- ``secret`` — значение не показывается: ``config explain``, дампы, логи.
- ``worker_only`` — не уходит в durable-снимок (Temporal): адреса, учётные
  данные, пути и настройки фронтенда, которые воркеру не нужны.
- ``expert`` — нет в шаблоне бутстрапа (полный справочник — demo_config.yml).
- ``restart`` — применяется только при старте: hot-reload его не подхватит.

Скалярный секрет хранится как ``SecretStr`` (тип ``SecretText``): ``repr``/``str``
настроек и ``model_dump(mode="json")`` его не показывают. Сырое значение —
``.get_secret_value()`` у поля или дамп с ``context=REVEAL_SECRETS``
(``plain_dump``) — для тех, кому оно правда нужно: транспорт, запись, дайджест.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from functools import lru_cache
from typing import Annotated, Any, Iterator, Mapping, get_args, get_origin

from pydantic import (
    BaseModel,
    PlainSerializer,
    SecretStr,
    SerializationInfo,
)

GC_META_KEY = "x-gc"
MASK = "***"
#: ``model_dump(context=REVEAL_SECRETS)`` — дамп с сырыми значениями ``SecretText``.
REVEAL_SECRETS: dict[str, bool] = {"reveal_secrets": True}


def _serialize_secret(value: SecretStr, info: SerializationInfo) -> Any:
    """``SecretText`` в дампе: по запросу ``REVEAL_SECRETS`` — сырое значение;
    иначе как у ``SecretStr`` (json — ``**********``/пусто, python — сам объект)."""
    context = info.context
    if isinstance(context, Mapping) and context.get("reveal_secrets"):
        return value.get_secret_value()
    if info.mode_is_json():
        return str(value)
    return value


#: Скалярный секрет настроек: ``SecretStr``, который раскрывается дампом
#: с ``context=REVEAL_SECRETS`` (стандартный ``SecretStr`` раскрыть так нельзя).
SecretText = Annotated[
    SecretStr, PlainSerializer(_serialize_secret, return_type=Any)
]


def secret_value(value: SecretStr | str | None) -> str | None:
    """Сырое значение секрета (``None`` остаётся ``None``)."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def plain_dump(model: BaseModel, **kwargs: Any) -> dict[str, Any]:
    """``model_dump`` с сырыми значениями секретов — для транспорта, записи
    и сравнения; показывать человеку — ``redacted_dump``."""
    return model.model_dump(context=REVEAL_SECRETS, **kwargs)


@dataclass(frozen=True)
class FieldMeta:
    secret: bool = False
    worker_only: bool = False
    expert: bool = False
    restart: bool = False

    def merged(self, child: FieldMeta) -> FieldMeta:
        """Флаги предка + свои: установленный флаг не снимается ниже по пути."""
        return replace(
            child,
            **{f.name: True for f in fields(self) if getattr(self, f.name)},
        )


def gc_meta(
    *,
    secret: bool = False,
    worker_only: bool = False,
    expert: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """Значение ``json_schema_extra`` для поля настроек."""
    flags = {
        "secret": secret,
        "worker_only": worker_only,
        "expert": expert,
        "restart": restart,
    }
    return {GC_META_KEY: {name: True for name, on in flags.items() if on}}


def _own_meta(info: Any) -> FieldMeta:
    extra = info.json_schema_extra
    if not isinstance(extra, Mapping):
        return FieldMeta()
    flags = extra.get(GC_META_KEY) or {}
    return FieldMeta(**{name: bool(flags.get(name)) for name in flags})


def _nested_model(annotation: Any) -> tuple[str, type[BaseModel] | None]:
    """``("model", M)`` для вложенной модели, ``("dict", M)`` для словаря
    моделей (ключ — имя записи), иначе ``("leaf", None)``."""
    for candidate in (annotation, *get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return "model", candidate
        if get_origin(candidate) is dict:
            args = get_args(candidate)
            if (
                len(args) == 2
                and isinstance(args[1], type)
                and issubclass(args[1], BaseModel)
            ):
                return "dict", args[1]
    return "leaf", None


def iter_leaves(
    model: type[BaseModel],
    prefix: str = "",
    inherited: FieldMeta = FieldMeta(),
) -> Iterator[tuple[str, FieldMeta]]:
    """Листья схемы с итоговыми флагами. Запись словаря моделей — ``*``:
    ``models.*.token``. Секция без листьев (словарь значений) — сама лист;
    секция с произвольными ключами (``extra="allow"``, ``agent_auth``) — тоже
    лист: её ключи схеме не известны, флаги им даёт сама секция."""
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        meta = inherited.merged(_own_meta(info))
        kind, nested = _nested_model(info.annotation)
        if kind == "model":
            if nested.model_config.get("extra") == "allow":
                yield path, meta
            yield from iter_leaves(nested, path + ".", meta)
        elif kind == "dict":
            yield from iter_leaves(nested, path + ".*.", meta)
        else:
            yield path, meta


@lru_cache(maxsize=1)
def leaf_meta() -> dict[str, FieldMeta]:
    """``{путь листа: флаги}`` для ``AppConfig``."""
    from gc_core.config_modules.root import AppConfig

    return dict(iter_leaves(AppConfig))


def _matches(pattern: list[str], parts: list[str]) -> bool:
    """Сегменты пути совпадают; ``*`` в шаблоне — любое имя записи словаря."""
    return len(pattern) == len(parts) and all(
        p in ("*", q) for p, q in zip(pattern, parts)
    )


def meta_for(dotted: str) -> FieldMeta:
    """Флаги по пути: лист, ключ внутри словаря-листа (флаги листа) или
    секция (флаг, общий для всех её листьев: секрет секции — секрет каждого).

    Имя записи с точкой (алиас модели ``gpt-4.1``) строкой не выразить — для
    обхода документа есть ``meta_for_parts``."""
    return meta_for_parts(dotted.split("."))


def meta_for_parts(parts: list[str]) -> FieldMeta:
    """``meta_for`` по уже разобранным сегментам пути."""
    parts = list(parts)
    table = [(path.split("."), meta) for path, meta in leaf_meta().items()]
    for depth in range(len(parts), 0, -1):
        for pattern, meta in table:
            if _matches(pattern, parts[:depth]):
                return meta
    below = [
        meta
        for pattern, meta in table
        if len(pattern) > len(parts) and _matches(pattern[: len(parts)], parts)
    ]
    if not below:
        return FieldMeta()
    return FieldMeta(
        **{
            f.name: all(getattr(m, f.name) for m in below)
            for f in fields(FieldMeta)
        }
    )


def paths_where(flag: str) -> frozenset[str]:
    """Пути листьев с установленным флагом (``"expert"``, ``"restart"``...)."""
    return frozenset(
        path for path, meta in leaf_meta().items() if getattr(meta, flag)
    )


def _mask(value: Any) -> Any:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if value in (None, "", {}, []):
        return value
    if isinstance(value, Mapping):
        return {key: _mask(item) for key, item in value.items()}
    return MASK


def masked_repr_args(
    model: BaseModel, *, all_fields: bool = False
) -> Iterator[tuple[str | None, Any]]:
    """``__repr_args__`` модели без значений её полей-секретов (по своим флагам
    поля; ``all_fields`` — секрет вся модель, напр. ``agent_auth``).

    ``SecretStr`` прячет себя сам; это — для секретов, которые остаются ``str``
    и ``dict``: заголовки (ключи видны), прокси, секция с произвольными ключами."""
    fields_info = type(model).model_fields
    for name, value in BaseModel.__repr_args__(model):
        info = fields_info.get(name) if name is not None else None
        if all_fields or (info is not None and _own_meta(info).secret):
            value = _mask(value)
        yield name, value


def redact(document: Any, parts: tuple[str, ...] = ()) -> Any:
    """Копия дампа настроек (``model_dump``) с замаскированными секретами.

    Пустое значение остаётся видимым: «секрет не задан» — полезная правда,
    а у словаря-секрета (заголовки) видны ключи, скрыты значения. Путь идёт
    сегментами, а не строкой: ключ записи может содержать точку.
    """
    if isinstance(document, Mapping):
        out = {}
        for key, value in document.items():
            path = (*parts, str(key))
            if meta_for_parts(list(path)).secret:
                out[key] = _mask(value)
            else:
                out[key] = redact(value, path)
        return out
    if isinstance(document, list):
        return [redact(item, parts) for item in document]
    return document


def redacted_dump(settings: BaseModel) -> dict[str, Any]:
    """``model_dump(mode="json")`` настроек без секретов — для показа и логов."""
    return redact(settings.model_dump(mode="json"))


__all__ = [
    "GC_META_KEY",
    "MASK",
    "REVEAL_SECRETS",
    "FieldMeta",
    "SecretText",
    "gc_meta",
    "iter_leaves",
    "leaf_meta",
    "masked_repr_args",
    "meta_for",
    "meta_for_parts",
    "paths_where",
    "plain_dump",
    "redact",
    "redacted_dump",
    "secret_value",
]
