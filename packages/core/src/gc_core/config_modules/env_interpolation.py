"""Резолв ${ENV:VAR} и ${FILE:path} в строках конфига.

Применяется к file-конфигу (каждый слой — сразу после чтения, до мержа и
валидации) и к CLI-override значениям.

Синтаксис:

- ``${ENV:VAR}`` — значение переменной окружения; ОБЯЗАТЕЛЬНАЯ ссылка;
- ``${ENV:VAR:-умолчание}`` — необязательная: переменная не задана или пуста →
  подставляется умолчание (как ``${VAR:-x}`` в shell); ``${ENV:VAR:-}`` —
  умолчание пустое;
- ``${FILE:path}`` / ``${FILE:path:-умолчание}`` — содержимое файла (хвостовой
  перевод строки срезается); с умолчанием — отсутствующий/нечитаемый файл даёт
  умолчание.

Два режима неразрешимой обязательной ссылки:

- мягкий (умолчание ``resolve_env_placeholders``, витрины и пробы TUI) —
  пустая строка + warning с именем/путём (никогда с содержимым);
- строгий (старт: ``config.load_settings``, ``--param``, конфиг native-воркера) —
  вызывающий передаёт список ``unresolved`` и получает в нём ``UnresolvedRef``
  на каждую ссылку (файл + dotted-ключ), а затем бросает
  ``UnresolvedPlaceholdersError`` (`raise_unresolved`) со всеми сразу: конфиг,
  в котором токен молча стал пустой строкой, падает позже и непонятнее —
  401 от бэкенда вместо «не задана переменная X».
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional

_logger = logging.getLogger(__name__)

# group(1) — имя переменной / путь файла; group(2) — умолчание после ``:-``
# (None — умолчания нет, ссылка обязательная; "" — пустое умолчание).
_ENV_MARKER = re.compile(r"\$\{ENV:([A-Za-z_]\w*)(?::-([^}]*))?\}", re.ASCII)
# Умолчание отделяет ПОСЛЕДНИЙ ``:-``: путь с ``:-`` внутри работает, если у
# ссылки есть умолчание; без умолчания — если файл с таким именем есть
# (тогда вся строка — путь, см. ``repl_file``).
_FILE_MARKER = re.compile(r"\$\{FILE:([^}]+?)(?::-((?:(?!:-)[^}])*))?\}")
_MAX_FILE_BYTES = 65536  # креды маленькие; защита от указания на большой файл

_OPTIONAL_HINT = "${ENV:VAR:-умолчание}"


@dataclass(frozen=True)
class UnresolvedRef:
    """Обязательная ссылка, которую нечем разрешить.

    ``key`` — dotted-путь значения внутри документа (элемент списка — ``[i]``);
    ``source`` — откуда документ (путь файла, ``--param``); ``layer`` — слой
    стека (base/managed/project/profile:<имя>), если вызывающий его знает."""

    kind: str  # "env" | "file"
    name: str  # имя переменной или путь файла (как записан)
    reason: str  # человекочитаемая причина, без содержимого
    key: str = ""
    source: str = ""
    layer: str = ""

    @property
    def placeholder(self) -> str:
        tag = "ENV" if self.kind == "env" else "FILE"
        return f"${{{tag}:{self.name}}}"

    def describe(self) -> str:
        where = ": ".join(part for part in (self.source, self.key) if part)
        what = (
            f"переменная окружения {self.name} не задана"
            if self.kind == "env"
            else f"файл {self.name} {self.reason}"
        )
        return f"{what} — {where}" if where else what


class UnresolvedPlaceholdersError(ValueError):
    """Конфиг ссылается на незаданные переменные/нечитаемые файлы.

    Подкласс ``ValueError`` — точки входа (`cli`, `tui.main`, лестница починки)
    уже ловят его как «конфиг не загружается» и печатают объяснение;
    ``refs`` — разбор для объяснения (`onboarding.explain_broken_config`)."""

    def __init__(self, refs: Iterable[UnresolvedRef]) -> None:
        self.refs: tuple[UnresolvedRef, ...] = tuple(refs)
        super().__init__(_unresolved_message(self.refs))

    @property
    def env_names(self) -> list[str]:
        names: list[str] = []
        for ref in self.refs:
            if ref.kind == "env" and ref.name not in names:
                names.append(ref.name)
        return names


def _unresolved_message(refs: tuple[UnresolvedRef, ...]) -> str:
    lines = ["конфиг ссылается на незаданные источники:"]
    lines += [f"- {ref.describe()}" for ref in refs]
    hint = []
    if any(ref.kind == "env" for ref in refs):
        hint.append("задайте переменные окружения (export VAR=…)")
    if any(ref.kind == "file" for ref in refs):
        hint.append("создайте файлы или поправьте пути")
    lines.append(
        f"Что сделать: {' / '.join(hint)}; необязательное значение пометьте "
        f"умолчанием: {_OPTIONAL_HINT} (пустое — ${{ENV:VAR:-}})."
    )
    return "\n".join(lines)


def raise_unresolved(refs: Iterable[UnresolvedRef]) -> None:
    """Бросить ``UnresolvedPlaceholdersError``, если список не пуст (повторы
    одной и той же ссылки в одном месте схлопываются)."""
    unique: list[UnresolvedRef] = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    if unique:
        raise UnresolvedPlaceholdersError(unique)


def _warn_if_lax_permissions(path: str, path_raw: str) -> None:
    """POSIX: файл с кредом доступен group/other → warning (аналогично ssh)."""
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & 0o077:
        _logger.warning(
            "credential file is group/world accessible, consider chmod 600: %s",
            path_raw,
        )


def _read_secret_file(
    path_raw: str, cache: dict[str, tuple[Optional[str], str]]
) -> tuple[Optional[str], str]:
    """Прочитать файл-кред: ``(значение, "")`` либо ``(None, причина)``.

    Кэш по пути — файл читается один раз на вызов резолвера."""
    if path_raw in cache:
        return cache[path_raw]
    path = os.path.expanduser(path_raw)
    result: tuple[Optional[str], str]
    try:
        too_large = os.path.getsize(path) > _MAX_FILE_BYTES
    except OSError:
        too_large = (
            False  # нет файла/нет доступа — причину назовёт чтение ниже
        )
    if too_large:
        result = (None, f"больше {_MAX_FILE_BYTES} байт")
    else:
        try:
            _warn_if_lax_permissions(path, path_raw)
            with open(path, encoding="utf-8-sig") as fd:
                result = (fd.read().rstrip("\r\n"), "")
        except FileNotFoundError:
            result = (None, "не найден")
        except (OSError, UnicodeDecodeError) as exc:
            result = (None, f"не читается ({type(exc).__name__})")
    cache[path_raw] = result
    return result


def _join_key(prefix: str, part: Any) -> str:
    if isinstance(part, int):
        return f"{prefix}[{part}]"
    return f"{prefix}.{part}" if prefix else str(part)


class _Resolver:
    """Один проход резолва: кэш файлов, сбор неразрешённого / warning'и."""

    def __init__(self, unresolved: Optional[list], source: str) -> None:
        self.unresolved = unresolved
        self.source = source
        self.missing_env: list[str] = []
        self.missing_files: dict[str, str] = {}
        self.file_cache: dict[str, tuple[Optional[str], str]] = {}

    def _miss(self, kind: str, name: str, reason: str, key: str) -> str:
        """Подстановка вместо неразрешённой ссылки.

        Строгий режим ставит метку с номером ссылки: загрузчик после мержа
        слоёв видит, пережило ли значение перекрытие (``settle_unresolved``).
        """
        if self.unresolved is not None:
            self.unresolved.append(
                UnresolvedRef(kind, name, reason, key=key, source=self.source)
            )
            return _UNRESOLVED_MARK.format(len(self.unresolved) - 1)
        if kind == "env":
            if name not in self.missing_env:
                self.missing_env.append(name)
        else:
            self.missing_files.setdefault(name, reason)
        return ""

    def string(self, value: str, key: str) -> str:
        def repl_env(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            current = os.environ.get(var)
            if default is not None:
                return current or default
            if current is not None:
                return current
            return self._miss("env", var, "не задана", key)

        def repl_file(m: re.Match[str]) -> str:
            path_raw, default = m.group(1), m.group(2)
            whole = m.group(0)[len("${FILE:") : -1]
            if default is not None and os.path.isfile(
                os.path.expanduser(whole)
            ):
                # Файл с ``:-`` в имени существует: это путь, а не умолчание.
                path_raw, default = whole, None
            value, reason = _read_secret_file(path_raw, self.file_cache)
            if value is not None:
                return value
            if default is not None:
                return default
            return self._miss("file", path_raw, reason, key)

        return _FILE_MARKER.sub(repl_file, _ENV_MARKER.sub(repl_env, value))

    def walk(self, obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            return {k: self.walk(v, _join_key(key, k)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, _join_key(key, i)) for i, v in enumerate(obj)]
        if isinstance(obj, str):
            return self.string(obj, key)
        return obj

    def warn(self) -> None:
        for var in sorted(self.missing_env):
            _logger.warning(
                "env var not set, substituted empty string: %s", var
            )
        for path_raw, reason in self.missing_files.items():
            _logger.warning(
                "credential file %s, substituted empty string: %s",
                _english_reason(reason),
                path_raw,
            )


def _english_reason(reason: str) -> str:
    """Короткая причина для лога (лог — на английском, как прежде)."""
    if reason.startswith("больше"):
        return f"too large (> {_MAX_FILE_BYTES} bytes)"
    return "not readable"


def resolve_env_placeholders(
    obj: Any,
    *,
    unresolved: Optional[list] = None,
    source: str = "",
) -> Any:
    """Рекурсивная немутирующая копия с подстановкой ``${ENV:VAR}`` и ``${FILE:path}``.

    Без ``unresolved`` (мягкий режим) — отсутствующий обязательный источник →
    ``""`` + один warning на уникальное имя/путь. С ``unresolved`` (строгий
    режим) — та же подстановка ``""``, но без warning: в список добавляется
    ``UnresolvedRef`` с dotted-ключом и ``source``, а решение (обычно —
    ``raise_unresolved``) за вызывающим. Ссылки с умолчанием (``:-``)
    неразрешёнными не бывают."""
    resolver = _Resolver(unresolved, source)
    resolved = resolver.walk(obj)
    resolver.warn()
    return resolved


def resolve_env_placeholders_strict(obj: Any, *, source: str = "") -> Any:
    """``resolve_env_placeholders`` в строгом режиме: любая неразрешимая
    обязательная ссылка → ``UnresolvedPlaceholdersError`` со всеми сразу."""
    unresolved: list[UnresolvedRef] = []
    resolved = resolve_env_placeholders(
        obj, unresolved=unresolved, source=source
    )
    raise_unresolved(unresolved)
    return resolved


#: Метка неразрешённой ссылки в строгом режиме (``\x00`` в YAML не встречается).
_UNRESOLVED_MARK = "\x00gc-unresolved:{}\x00"
_UNRESOLVED_RE = re.compile("\x00gc-unresolved:(\\d+)\x00")


def strip_unresolved_marks(obj: Any, found: Optional[set] = None) -> Any:
    """Копия ``obj`` без меток строгого режима (метка → ``""``).

    В ``found`` — номера ссылок, чьи метки встретились."""
    if isinstance(obj, dict):
        return {k: strip_unresolved_marks(v, found) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_unresolved_marks(v, found) for v in obj]
    if isinstance(obj, tuple):
        return tuple(strip_unresolved_marks(v, found) for v in obj)
    if isinstance(obj, str) and "\x00gc-unresolved:" in obj:

        def drop(m: re.Match[str]) -> str:
            if found is not None:
                found.add(int(m.group(1)))
            return ""

        return _UNRESOLVED_RE.sub(drop, obj)
    return obj


def settle_unresolved(
    merged: Any, refs: list
) -> tuple[Any, list[UnresolvedRef]]:
    """Итоговый документ без меток и ссылки, значения которых пережили мерж.

    Ссылка, чьё значение перекрыто верхним слоем (проект, профиль), загрузку
    не роняет: незаданная переменная там ни на что не влияет."""
    found: set[int] = set()
    clean = strip_unresolved_marks(merged, found)
    return clean, [refs[i] for i in sorted(found) if i < len(refs)]


def tag_layer(
    refs: list, start: int, *, layer: str, source: Optional[str] = None
) -> None:
    """Проставить слой (и, если дан, источник) ссылкам ``refs[start:]`` — тем,
    что добавил резолв одного документа."""
    for i in range(start, len(refs)):
        refs[i] = replace(
            refs[i],
            layer=layer,
            source=refs[i].source if source is None else source,
        )


def _resolve_env_only(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve_env_only(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_only(v) for v in obj]
    if isinstance(obj, str):
        return _ENV_MARKER.sub(_env_only_repl, obj)
    return obj


def _env_only_repl(m: re.Match[str]) -> str:
    current = os.environ.get(m.group(1))
    if m.group(2) is not None:
        return current or m.group(2)
    return current or ""


def resolve_env_only(obj: Any) -> Any:
    """Рекурсивная немутирующая копия с подстановкой ТОЛЬКО ``${ENV:VAR}``.

    ``${FILE:path}`` остаётся ссылкой как есть — файловый резолвер сюда не входит
    вообще, ни чтением, ни статом. Для мест, где раскрытое значение нужно
    посчитать на UI-потоке (`tui/editors/model_entry_form.py::_as_validator_sees`):
    там, где `resolve_env_placeholders` читал бы файл-кред с диска синхронно на
    каждое нажатие клавиши, эта функция гарантированно не тронет диск.

    Без warning вовсе (не только без дедупа) и без строгости: вызывающая сторона
    такого места сама называет причину человеку текстом на экране, и лог на
    каждый набранный символ был бы шумом, а не сигналом. Умолчания ``:-``
    понимает так же, как полный резолвер."""
    return _resolve_env_only(obj)


def env_ref_names(value: Any, *, required_only: bool = False) -> list[str]:
    """Имена переменных ``${ENV:…}`` в сыром значении (в порядке появления);
    ``required_only`` — только обязательные (без умолчания ``:-``)."""
    if not isinstance(value, str):
        return []
    return [
        m.group(1)
        for m in _ENV_MARKER.finditer(value)
        if not (required_only and m.group(2) is not None)
    ]


__all__ = [
    "UnresolvedPlaceholdersError",
    "UnresolvedRef",
    "env_ref_names",
    "raise_unresolved",
    "resolve_env_only",
    "resolve_env_placeholders",
    "resolve_env_placeholders_strict",
    "tag_layer",
]
