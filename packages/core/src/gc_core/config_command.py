"""Команда ``config``: ``gc-cli config …`` и ``gc-ua config …``.

Три подкоманды над тем же конвейером загрузки, что и старт агента
(``load_settings``), — без агента, MCP и сети:

- ``explain`` — значение каждой настройки и откуда оно взялось (слой, файл,
  ``${ENV:VAR}``); секреты маскируются всегда, в том числе в ``--json``;
- ``validate`` — собирается ли конфиг; отказ — то же объяснение, что на старте;
- ``schema`` — JSON Schema ``AppConfig`` (метаданные полей — под ``x-gc``).

Коды выхода: 0 — успех, 1 — конфиг не загружается, 2 — ошибка вызова.

Показывается конфиг уровня файлов и окружения: флаги командной строки
(``--param``, ``--model``…) и правки сессии сюда не входят — их нет до старта.
"""

from __future__ import annotations

import argparse
import difflib
import functools
import itertools
import json
import os
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Optional, Sequence, TextIO

from pydantic import AliasChoices, BaseModel, TypeAdapter
from pydantic.fields import PydanticUndefined

from gc_core.config_modules.field_meta import (
    MASK,
    is_secret_name,
    meta_for,
    redact_free_form,
)
from gc_core.config_modules.path_hints import suggest_paths
from gc_core.config_modules.provenance import DEFAULT, Source

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_TITLE = "Конфиг GigaCode Universal Agent (config.yml)"

CLI_LAYERS_NOTE = (
    "Значения из файлов и окружения; флаги командной строки "
    "(--param, --model, --level…) и правки сессии здесь не учитываются."
)

#: Источник значения, которое отличается от умолчания, но ни один слой его не
#: назвал (например, ключ перенесён compat-слоем со старого имени).
UNTRACKED = Source("вычислено при загрузке")

_ABSENT = object()
_JSONABLE = TypeAdapter(Any)


def to_jsonable_python(value: Any) -> Any:
    """Значение в JSON-вид (Path → str, модель → словарь, SecretStr → звёзды)."""
    return _JSONABLE.dump_python(value, mode="json")


# --- Листья настроек ------------------------------------------------------


@dataclass(frozen=True)
class Leaf:
    """Лист настроек: путь по полям (ключи словарей — как есть), тот же путь
    в схеме (запись словаря моделей — ``*``), написания каждого сегмента в
    файле (имя поля и его alias) и значение."""

    parts: tuple[str, ...]
    schema_parts: tuple[str, ...]
    spellings: tuple[tuple[str, ...], ...]
    value: Any
    #: Умолчание поля в его модели (для записей словарей моделей); нет — _ABSENT.
    field_default: Any = _ABSENT

    @property
    def key(self) -> str:
        return ".".join(self.parts)

    @property
    def secret(self) -> bool:
        # Путь схемы, а не значения: алиас модели с точкой («gpt-4.1») ломает
        # разбор строкового пути, а ``*`` в схеме совпадает с любой записью.
        return meta_for(".".join(self.schema_parts)).secret

    def document_keys(self) -> list[str]:
        """Пути, которыми лист мог быть записан в слое: файлы пишут ключи
        документа (``server-name``), точечные правки — имена полей."""
        return [".".join(keys) for keys in itertools.product(*self.spellings)]


def _spellings(name: str, info: Any) -> tuple[str, ...]:
    names = [name, info.alias, info.serialization_alias]
    validation = info.validation_alias
    if isinstance(validation, str):
        names.append(validation)
    elif isinstance(validation, AliasChoices):
        names.extend(c for c in validation.choices if isinstance(c, str))
    return tuple(dict.fromkeys(n for n in names if n))


def _field_default(info: Any) -> Any:
    try:
        value = info.get_default(call_default_factory=True)
    except Exception:  # фабрика, которой нужны данные модели
        return _ABSENT
    if value is PydanticUndefined:
        return _ABSENT
    return to_jsonable_python(value)


def _model_dict_items(value: Any) -> Optional[dict]:
    """Словарь моделей (``models``, ``mcp.servers``) — раскрывается по записям."""
    if not isinstance(value, dict) or not value:
        return None
    if all(isinstance(item, BaseModel) for item in value.values()):
        return value
    return None


def iter_setting_leaves(
    model: BaseModel,
    parts: tuple[str, ...] = (),
    schema_parts: tuple[str, ...] = (),
    spellings: tuple[tuple[str, ...], ...] = (),
) -> Iterator[Leaf]:
    """Листья действующих настроек. Вложенная модель с полями раскрывается,
    словарь моделей — по записям; списки, словари значений и модель без
    объявленных полей (``agent_auth``) — сами листья."""
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        path, schema_path = (*parts, name), (*schema_parts, name)
        spelled = (*spellings, _spellings(name, info))
        entries = _model_dict_items(value)
        if isinstance(value, BaseModel) and type(value).model_fields:
            yield from iter_setting_leaves(value, path, schema_path, spelled)
        elif entries is not None:
            for key, item in entries.items():
                yield from iter_setting_leaves(
                    item,
                    (*path, str(key)),
                    (*schema_path, "*"),
                    (*spelled, (str(key),)),
                )
        else:
            yield Leaf(
                path,
                schema_path,
                spelled,
                to_jsonable_python(value),
                _field_default(info),
            )


def mask_secret(value: Any) -> Any:
    """Скрыть значение секрета. Пустое остаётся видимым («секрет не задан» —
    полезная правда), у словаря (заголовки) видны ключи, скрыты значения."""
    if value in (None, "", {}, []):
        return value
    if isinstance(value, dict):
        return {key: mask_secret(item) for key, item in value.items()}
    return MASK


# --- explain ---------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """Строка ``explain``: значение уже замаскировано, если поле секретное."""

    key: str
    value: Any
    sources: tuple[Source, ...]
    is_default: bool
    secret: bool
    default: Any = _ABSENT

    @property
    def source_text(self) -> str:
        return "; ".join(source.describe() for source in self.sources)


@dataclass
class UnknownKey(Exception):
    key: str
    suggestions: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        text = f"неизвестная настройка: {self.key}"
        if self.suggestions:
            text += (
                f". Возможно, имелось в виду: {', '.join(self.suggestions)}"
            )
        return text


def _absolute(source: Source) -> Source:
    """Путь слоя — абсолютный, как в шапке (``--config ./x.yml`` пишется как есть)."""
    if source.path is None:
        return source
    return replace(source, path=os.path.abspath(source.path))


def sources_of(provenance: Any, keys: Sequence[str]) -> tuple[Source, ...]:
    """Источники листа по любому из его написаний. Словарь значений
    (заголовки) задаётся по ключам — тогда источники его ключей, без повторов."""
    for key in keys:
        source = provenance.source(key)
        if source is not DEFAULT:
            return (_absolute(source),)
    found: list[Source] = []
    for key in keys:
        for dotted, source in provenance.items():
            source = _absolute(source)
            if dotted.startswith(key + ".") and source not in found:
                found.append(source)
    return tuple(found) or (DEFAULT,)


def _defaults_by_key(loaded: Any) -> dict[str, Any]:
    """Значения по умолчанию после той же нормализации путей, что у загрузки."""
    from gc_core.config_modules.root import AppConfig, resolve_paths

    config_dir = loaded.runtime.config_dir or os.getcwd()
    defaults = resolve_paths(AppConfig(), config_dir)
    return {leaf.key: leaf.value for leaf in iter_setting_leaves(defaults)}


def _entry(leaf: Leaf, provenance: Any, defaults: dict[str, Any]) -> Entry:
    secret = leaf.secret
    default = defaults.get(leaf.key, leaf.field_default)
    sources = sources_of(provenance, leaf.document_keys())
    untouched = sources == (DEFAULT,)
    is_default = untouched and default is not _ABSENT and default == leaf.value
    if untouched and not is_default:
        sources = (UNTRACKED,)
    hide = mask_secret if secret else redact_free_form
    return Entry(
        key=leaf.key,
        value=hide(leaf.value),
        sources=sources,
        is_default=is_default,
        secret=secret,
        default=default if default is _ABSENT else hide(default),
    )


def _known_names(leaves: list[Leaf]) -> list[str]:
    names: set[str] = set()
    for leaf in leaves:
        for depth in range(1, len(leaf.parts) + 1):
            names.add(".".join(leaf.parts[:depth]))
    return sorted(names)


def _entry_inside_leaf(
    key: str, leaves: list[Leaf], provenance: Any
) -> Optional[Entry]:
    """``backend.chat_completion_headers.X-Api-Key`` — ключ словаря-листа."""
    for leaf in leaves:
        prefix = leaf.key + "."
        if not key.startswith(prefix) or not isinstance(leaf.value, dict):
            continue
        name = key[len(prefix) :]
        if name not in leaf.value:
            continue
        value = leaf.value[name]
        sources = sources_of(
            provenance, [f"{doc}.{name}" for doc in leaf.document_keys()]
        )
        return Entry(
            key=key,
            value=mask_secret(value)
            if leaf.secret or is_secret_name(name)
            else redact_free_form(value),
            sources=sources,
            is_default=sources == (DEFAULT,),
            secret=leaf.secret,
        )
    return None


def explain_entries(
    loaded: Any,
    keys: Sequence[str] = (),
    *,
    include_defaults: bool = False,
) -> list[Entry]:
    """Строки ``explain``. Без ``keys`` — заданные настройки (с
    ``include_defaults`` — все); ключ-секция раскрывается во все свои листья.
    Ключ — путь через точку; неизвестный (в том числе прежнее плоское имя
    ``max_steps``) — ``UnknownKey``: пути с тем же последним сегментом
    (``agent.max_steps``), затем близкие варианты."""
    leaves = list(iter_setting_leaves(loaded.settings))
    defaults = _defaults_by_key(loaded)
    by_key = {leaf.key: leaf for leaf in leaves}
    provenance = loaded.provenance
    if not keys:
        entries = [_entry(leaf, provenance, defaults) for leaf in leaves]
        if not include_defaults:
            entries = [entry for entry in entries if not entry.is_default]
        return entries

    entries: list[Entry] = []
    for key in keys:
        entries.extend(
            _entries_for_key(key, leaves, by_key, provenance, defaults)
        )
    return entries


def _entries_for_key(
    key: str,
    leaves: list,
    by_key: dict,
    provenance: Any,
    defaults: Any,
) -> list[Entry]:
    """Строки одного ключа: лист, секция целиком или путь внутри листа;
    иначе ``UnknownKey`` с подсказками."""
    if key in by_key:
        return [_entry(by_key[key], provenance, defaults)]
    section = [leaf for leaf in leaves if leaf.key.startswith(key + ".")]
    if section:
        return [_entry(leaf, provenance, defaults) for leaf in section]
    inner = _entry_inside_leaf(key, leaves, provenance)
    if inner is not None:
        return [inner]
    close = difflib.get_close_matches(
        key, _known_names(leaves), n=3, cutoff=0.6
    )
    raise UnknownKey(
        key, list(dict.fromkeys([*suggest_paths(key), *close]))[:3]
    )


def config_files(loaded: Any) -> dict[str, Optional[str]]:
    """Файлы, из которых собран конфиг: base, managed, project, профиль."""
    from gc_core.config_modules import env_vars

    managed = env_vars.managed_config_path()
    project = env_vars.project_local_config_path()
    ref = getattr(loaded.profile_meta, "ref", None)
    return {
        "base": loaded.runtime.config_path,
        "managed": str(managed) if managed and managed.is_file() else None,
        "project": os.path.abspath(project) if project else None,
        "profile": str(ref.path) if ref is not None else None,
    }


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def format_explain(
    loaded: Any,
    entries: Sequence[Entry],
    *,
    show_defaults: bool = False,
    program: str = "gc-cli",
) -> str:
    """Человекочитаемый вывод ``explain``: шапка (профиль, файлы) и значения,
    сгруппированные по секциям."""
    lines = [f"Конфиг {program}"]
    lines.append(f"Профиль: {loaded.runtime.active_profile or '(нет)'}")
    lines.append("Файлы:")
    for layer, path in config_files(loaded).items():
        lines.append(f"  {layer:<8} {path or '—'}")
    lines.append(CLI_LAYERS_NOTE)
    if not entries:
        lines += ["", "Все настройки — по умолчанию (полный список: --all)."]
        return "\n".join(lines) + "\n"
    section = None
    for entry in entries:
        head = entry.key.split(".", 1)[0]
        if head != section:
            section = head
            lines += ["", f"[{head}]"]
        line = (
            f"  {entry.key} = {_render(entry.value)}    ← {entry.source_text}"
        )
        if (
            show_defaults
            and not entry.is_default
            and entry.default is not _ABSENT
        ):
            line += f"    (по умолчанию: {_render(entry.default)})"
        lines.append(line)
    return "\n".join(lines) + "\n"


def explain_document(
    loaded: Any,
    entries: Sequence[Entry],
    *,
    show_defaults: bool = False,
) -> dict[str, Any]:
    """``explain --json``: те же данные, секреты так же замаскированы."""
    settings = []
    for entry in entries:
        item: dict[str, Any] = {
            "key": entry.key,
            "value": entry.value,
            "source": entry.source_text,
            "sources": [
                {
                    "layer": source.layer,
                    "path": source.path,
                    "env": list(source.env),
                }
                for source in entry.sources
            ],
            "default": entry.is_default,
            "secret": entry.secret,
        }
        if show_defaults and entry.default is not _ABSENT:
            item["default_value"] = entry.default
        settings.append(item)
    return {
        "profile": loaded.runtime.active_profile or None,
        "files": config_files(loaded),
        "note": CLI_LAYERS_NOTE,
        "settings": settings,
    }


# --- validate / schema ------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    config_path: str
    profile: Optional[str] = None
    notices: tuple[str, ...] = ()
    error: Optional[str] = None
    explanation: Optional[str] = None


def _load(config: Optional[str], profile: Optional[str]) -> Any:
    from gc_core.config import load_settings

    return load_settings(config, profile_override=profile)


def _broken_explanation(
    config: Optional[str],
    profile: Optional[str],
    exc: Exception,
    program: str,
) -> str:
    """То же объяснение, что на старте агента; не собралось само — текст ошибки."""
    from gc_core.config import resolve_config_path
    from gc_core.onboarding import explain_broken_config

    try:
        return explain_broken_config(
            resolve_config_path(config),
            exc,
            program=program,
            profile_override=profile,
        )
    except Exception:  # объяснение — украшение; сама ошибка важнее
        return f"[gc] Конфиг не загружается: {exc}"


def validate_config(
    config: Optional[str] = None,
    profile: Optional[str] = None,
    *,
    program: str = "gc-cli",
) -> ValidationResult:
    """Собрать конфиг тем же ``load_settings``, что и старт: ``ok`` + notices
    или текст отказа (``ValueError``; ``ValidationError`` pydantic — его
    подкласс)."""
    from gc_core.config import resolve_config_path

    try:
        loaded = _load(config, profile)
    except ValueError as exc:
        return ValidationResult(
            ok=False,
            config_path=os.path.abspath(resolve_config_path(config)),
            profile=profile,
            error=str(exc),
            explanation=_broken_explanation(config, profile, exc, program),
        )
    return ValidationResult(
        ok=True,
        config_path=loaded.runtime.config_path or "",
        profile=loaded.runtime.active_profile or None,
        notices=tuple(loaded.runtime.notices),
    )


def schema_document() -> dict[str, Any]:
    """JSON Schema ``AppConfig`` (ключи документа — alias, как в config.yml)."""
    from gc_core.config_modules.root import AppConfig

    schema = AppConfig.model_json_schema(by_alias=True, mode="validation")
    schema.pop("title", None)
    return {"$schema": JSON_SCHEMA_DIALECT, "title": SCHEMA_TITLE, **schema}


def schema_text() -> str:
    """Схема текстом: ключи отсортированы — вывод не зависит от порядка
    объявления и одинаков от запуска к запуску."""
    return (
        json.dumps(
            schema_document(), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )


# --- argparse ---------------------------------------------------------------


class _Exit(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class _Formatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(
            usage,
            actions,
            groups,
            "использование: " if prefix is None else prefix,
        )


class _Parser(argparse.ArgumentParser):
    """Парсер, который пишет в переданные потоки и не зовёт ``sys.exit``."""

    def __init__(self, *args: Any, out: TextIO, err: TextIO, **kwargs: Any):
        kwargs.setdefault("formatter_class", _Formatter)
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self._out, self._err = out, err
        self._positionals.title = "аргументы"
        self._optionals.title = "параметры"
        self.add_argument(
            "-h", "--help", action="help", help="показать справку и выйти"
        )

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            (self._out if file is sys.stdout else self._err).write(message)

    def exit(self, status: int = 0, message: Optional[str] = None):
        if message:
            self._err.write(message)
        raise _Exit(status)

    def error(self, message: str):
        self.print_usage(self._err)
        self.exit(EXIT_USAGE, f"{self.prog}: ошибка: {message}\n")


def build_parser(program: str, out: TextIO, err: TextIO) -> _Parser:
    make = functools.partial(_Parser, out=out, err=err)
    parser = make(
        prog=f"{program} config",
        description=(
            "Конфиг агента без запуска агента: откуда взялось значение, "
            "собирается ли конфиг, JSON Schema. Коды выхода: 0 — успех, "
            "1 — конфиг не загружается, 2 — ошибка вызова."
        ),
    )
    # Общие параметры подкоманд — родитель без своего -h (его даёт подкоманда).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Путь к config.yml (как у основной команды)",
    )
    common.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="Профиль поверх указателя profile: из config.yml",
    )
    sub = parser.add_subparsers(
        dest="command",
        title="команды",
        metavar="{explain,validate,schema}",
        parser_class=make,
    )

    explain = sub.add_parser(
        "explain",
        help="значение и источник настроек (слой, файл, ${ENV:…})",
        description=(
            "Значение и источник каждой настройки. Без KEY — заданные "
            "настройки (не умолчания), по секциям. Секреты всегда скрыты. "
            + CLI_LAYERS_NOTE
        ),
        parents=[common],
    )
    explain.add_argument(
        "keys",
        nargs="*",
        metavar="KEY",
        help="Путь настройки (agent.max_steps) или секция (agent)",
    )
    explain.add_argument(
        "--all",
        action="store_true",
        help="Показать и настройки со значением по умолчанию",
    )
    explain.add_argument("--json", action="store_true", help="Вывод в JSON")
    explain.add_argument(
        "--show-defaults",
        action="store_true",
        help="Рядом с заданным значением показать значение по умолчанию",
    )

    validate = sub.add_parser(
        "validate",
        help="проверить, что конфиг собирается (агент не запускается)",
        description=(
            "Собрать конфиг тем же загрузчиком, что и старт агента. "
            "0 — ok, 1 — конфиг не загружается."
        ),
        parents=[common],
    )
    validate.add_argument("--json", action="store_true", help="Вывод в JSON")

    schema = sub.add_parser(
        "schema",
        help="JSON Schema конфига",
        description=(
            "JSON Schema config.yml (AppConfig); метаданные полей — под x-gc."
        ),
    )
    schema.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="Записать в файл вместо stdout",
    )
    return parser


def _run_explain(args: Any, program: str, out: TextIO, err: TextIO) -> int:
    try:
        loaded = _load(args.config, args.profile)
    except ValueError as exc:
        err.write(
            _broken_explanation(args.config, args.profile, exc, program) + "\n"
        )
        return EXIT_INVALID
    try:
        entries = explain_entries(loaded, args.keys, include_defaults=args.all)
    except UnknownKey as exc:
        err.write(f"{program} config explain: {exc}\n")
        return EXIT_USAGE
    if args.json:
        document = explain_document(
            loaded, entries, show_defaults=args.show_defaults
        )
        out.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    else:
        out.write(
            format_explain(
                loaded,
                entries,
                show_defaults=args.show_defaults,
                program=program,
            )
        )
    return EXIT_OK


def _run_validate(args: Any, program: str, out: TextIO, err: TextIO) -> int:
    result = validate_config(args.config, args.profile, program=program)
    if args.json:
        document = {
            "ok": result.ok,
            "config": result.config_path,
            "profile": result.profile,
            "notices": list(result.notices),
            "error": result.error,
        }
        out.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    elif result.ok:
        profile = f", профиль {result.profile}" if result.profile else ""
        out.write(f"ok: {result.config_path}{profile}\n")
        for notice in result.notices:
            out.write(f"  • {notice}\n")
    else:
        err.write((result.explanation or result.error or "") + "\n")
    return EXIT_OK if result.ok else EXIT_INVALID


def _run_schema(args: Any, out: TextIO, err: TextIO) -> int:
    text = schema_text()
    if args.output is None:
        out.write(text)
        return EXIT_OK
    try:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        err.write(f"не удалось записать схему в {args.output}: {exc}\n")
        return EXIT_INVALID
    out.write(f"Схема записана: {args.output}\n")
    return EXIT_OK


def main(
    argv: Sequence[str],
    *,
    program: str,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    """``<program> config <команда> …``; ``argv`` — аргументы после ``config``."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = build_parser(program, out, err)
    try:
        args = parser.parse_args(list(argv))
    except _Exit as exc:
        return exc.status
    if args.command is None:
        parser.print_help(err)
        return EXIT_USAGE
    if args.command == "explain":
        return _run_explain(args, program, out, err)
    if args.command == "validate":
        return _run_validate(args, program, out, err)
    return _run_schema(args, out, err)


__all__ = [
    "EXIT_INVALID",
    "EXIT_OK",
    "EXIT_USAGE",
    "Entry",
    "Leaf",
    "UnknownKey",
    "ValidationResult",
    "build_parser",
    "config_files",
    "explain_document",
    "explain_entries",
    "format_explain",
    "iter_setting_leaves",
    "main",
    "mask_secret",
    "schema_document",
    "schema_text",
    "validate_config",
]
