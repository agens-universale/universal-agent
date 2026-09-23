#!/usr/bin/env python3
"""Локальный аналог правил Sonar, у которых нет эквивалента в ruff.

Зачем отдельный скрипт. «Мелкие» правила отчёта Sonar закрывает ruff, порог
когнитивной сложности (S3776) — complexipy, но часть правил не умеет никто:
дубли литералов, лишний класс в кортеже ``except``, функция с одинаковым
``return``, сложность регулярки. Их и считает этот файл — по тем же порогам,
что и Sonar, чтобы код между сканами не «разъезжался».

Что проверяется (ключ Sonar — что ищем):

* **S1192** — строковый литерал длиной >= MIN_LEN, встречающийся в одном файле
  >= THRESHOLD раз. Докстроки, куски f-строк, строковые аннотации и литералы
  вида идентификатора (``[\\w-]*``) не считаются, как и у Sonar.
* **S6353** — ``[A-Za-z0-9_]`` вместо ``\\w``, ``[0-9]`` вместо ``\\d``
  только в ASCII-режиме: для Unicode эти замены меняют множество символов.
* **S5857** — ленивый квантификатор ``.*?x`` вместо ``[^x]*``.
* **S5843** — сложность регулярки больше REGEX_MAX. Алгоритм откалиброван по
  отчёту Sonar, см. ``regex_complexity``.
* **S5713** — в кортеже ``except (A, B)`` класс уже перекрыт соседним: либо это
  его подкласс, либо тот же объект под другим именем (``asyncio.TimeoutError``
  и ``TimeoutError`` — с 3.11 один и тот же класс).
* **S3516** — у функции >= 2 ``return``, все с одной и той же константой, и
  провалиться в неявный ``return None`` она не может.
* **S3358** — тернарный оператор внутри тернарного.
* **S4144** — две функции-соседки с побайтово одинаковым телом.
* **S108** — пустой блок ``if``/``else``/``for``/``while``/``with``/``try``, в
  котором один ``pass`` и ни одного комментария. ``except`` не проверяется —
  Sonar его тоже не считает.
* **S5527** — выключенная проверка сертификата TLS: ``check_hostname = False``,
  ``verify_mode = ssl.CERT_NONE``, ``_create_unverified_context()``.
* **Web:S5256** — ``<table>`` без ``<caption>``/``aria-label``/
  ``aria-labelledby``/``aria-describedby``/``summary`` (файлы ``.html``).

Правила S6353/S5857 раньше были подписаны ключами S6035/S6019 — это
опечатка: S6035 про совсем другое (группы в регулярках JavaScript).

Три правила отчёта локально НЕ ловятся и остаются за Sonar или тайпчекером:
S2589 (условие всегда истинно), S7507 (доступ к возможно отсутствующему
ключу), S3403 (сравнение значений несовместимых типов). См.
docs/code-quality.md.

Комментарий с ``NOSONAR`` глушит находки на той же строке.

Запуск: ``python scripts/sonar_like_check.py <пути...>``. Ненулевой код выхода
при находках — для CI и pre-commit.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import io
import re
import re._parser as _reparser
import sys
import tokenize
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path

THRESHOLD = 3
MIN_LEN = 5
REGEX_MAX = 20
MIN_IDENTICAL_BODY = 2
# Каталоги, которых нет в sonar.sources: вендорный код и тесты
# (sonar.exclusions=**/tests/**).
SKIP_DIRS = {".venv", "__pycache__", "external", "vendor", "tests", ".git", "node_modules"}
SKIP_FILES = {"conftest.py"}

# Как у Sonar для Python: литералы из одних букв/цифр/подчёркиваний/дефисов
# (ключи словарей, идентификаторы, «utf-8») дублями не считаются.
_IDENTIFIER_LIKE = re.compile(r"[\w-]*")

# Элементы AST ASCII-классов: порядок диапазонов не меняет множество символов.
_WORD_RANGES = frozenset({("RANGE", (65, 90)), ("RANGE", (97, 122)), ("RANGE", (48, 57)), ("LITERAL", 95)})
_DIGIT_RANGES = frozenset({("RANGE", (48, 57))})
# Как у Sonar: ленивый квантификатор перед ОДНИМ символом, которым и заканчивается
# паттерн/группа — только тогда замена на [^x]* эквивалентна.
_RELUCTANT = re.compile(r"\.[*+]\?(\\?[^\\()\[\]{}|^$*+?.])(?=$|\)|\$)")

_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.Module, ast.ClassDef, *_FUNC_DEFS)


# --------------------------------------------------------------------------- #
# S1192 — дубли строковых литералов
# --------------------------------------------------------------------------- #
def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _SCOPES):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _fstring_parts(tree: ast.AST) -> set[int]:
    return {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values}


def _annotations_of(node: ast.AST) -> list[ast.expr | None]:
    """Аннотации узла: возвращаемый тип, параметры, ``x: T = ...``."""
    if isinstance(node, ast.AnnAssign):
        return [node.annotation]
    if not isinstance(node, _FUNC_DEFS):
        return []
    args = node.args
    out: list[ast.expr | None] = [node.returns]
    out += [a.annotation for a in args.args + args.kwonlyargs + args.posonlyargs]
    out += [a.annotation for a in (args.vararg, args.kwarg) if a is not None]
    return out


def _annotation_strings(tree: ast.AST) -> set[int]:
    """Строковые forward-ref аннотации (``-> "Foo | None"``) — не литералы данных."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        for ann in _annotations_of(node):
            if ann is not None:
                ids.update(id(n) for n in ast.walk(ann) if isinstance(n, ast.Constant))
    return ids


def check_duplicate_literals(path: Path, tree: ast.AST) -> list[str]:
    skip = _docstring_nodes(tree) | _fstring_parts(tree) | _annotation_strings(tree)
    seen: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or id(node) in skip:
            continue
        if len(node.value) >= MIN_LEN and not _IDENTIFIER_LIKE.fullmatch(node.value):
            seen.setdefault(node.value, []).append(node.lineno)
    out = []
    for value, lines in seen.items():
        if len(lines) >= THRESHOLD:
            out.append(
                f"{path}:{lines[0]}: S1192 literal {value!r} duplicated {len(lines)} times "
                f"(lines {', '.join(map(str, lines))}); define a constant"
            )
    return out


# --------------------------------------------------------------------------- #
# S6353 / S5857 / S5843 — регулярки
# --------------------------------------------------------------------------- #
_REGEX_FLAG_POSITION = {
    "compile": 1,
    "search": 2,
    "match": 2,
    "fullmatch": 2,
    "findall": 2,
    "finditer": 2,
    "split": 3,
    "sub": 4,
    "subn": 4,
}


def _regex_flags(node: ast.expr) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re":
        flag = re.RegexFlag.__members__.get(node.attr)
        return int(flag) if flag is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _regex_flags(node.left), _regex_flags(node.right)
        return left | right if left is not None and right is not None else None
    return None


def _regex_arguments(node: ast.Call, position: int) -> tuple[ast.expr | None, int | None]:
    keywords = {kw.arg: kw.value for kw in node.keywords}
    if None in keywords:  # **kwargs могут задавать неизвестные flags
        return None, None
    pattern = node.args[0] if node.args else keywords.get("pattern")
    flags = node.args[position] if len(node.args) > position else keywords.get("flags", ast.Constant(value=0))
    return pattern, _regex_flags(flags)


def _regex_call_patterns(tree: ast.AST) -> Iterator[tuple[int, str, int]]:
    """Статические pattern/flags у API re, принимающих паттерн; не re.escape."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_re = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "re"
        if not is_re or func.attr not in _REGEX_FLAG_POSITION:
            continue
        pattern, flags = _regex_arguments(node, _REGEX_FLAG_POSITION[func.attr])
        if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str) and flags is not None:
            yield pattern.lineno, pattern.value, flags


_REPEATS = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})
_ASSERTS = frozenset({"ASSERT", "ASSERT_NOT"})


def _structural_cost(seq: Iterable[tuple], level: int) -> int:
    """Стоимость структуры паттерна по дереву ``re._parser``.

    Уровень вложенности стартует с 1. Повтор и lookaround стоят «уровень», их
    содержимое считается на уровень глубже; альтернатива — «уровень» за первую
    ``|`` и +1 за каждую следующую, ветви остаются на том же уровне; обратная
    ссылка — +1. Классы символов здесь НЕ считаются: парсер схлопывает ``[^>]``
    в NOT_LITERAL, поэтому их отдельно считает ``_count_char_classes``.
    """
    total = 0
    for op, av in seq:
        name = op.name
        if name in _REPEATS:
            total += level + _structural_cost(av[2], level + 1)
        elif name in _ASSERTS:
            total += level + _structural_cost(av[1], level + 1)
        elif name == "BRANCH":
            branches = av[1]
            total += level + max(0, len(branches) - 2)
            total += sum(_structural_cost(b, level) for b in branches)
        elif name == "SUBPATTERN":
            total += _structural_cost(av[3], level)
        elif name == "ATOMIC_GROUP":
            total += _structural_cost(av, level)
        elif name == "GROUPREF":
            total += 1
        elif name == "GROUPREF_EXISTS":
            total += level + sum(_structural_cost(b, level + 1) for b in av[1:] if b)
    return total


def _skip_class_head(pattern: str, i: int) -> int:
    """``^`` сразу за ``[`` и ``]`` первым символом класса — литералы, не конец."""
    if i < len(pattern) and pattern[i] == "^":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    return i


def _count_char_classes(pattern: str) -> int:
    """Сколько в исходнике паттерна классов ``[...]`` — каждый стоит +1."""
    count = 0
    i = 0
    inside = False
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if not inside and char == "[":
            count += 1
            inside = True
            i = _skip_class_head(pattern, i + 1)
            continue
        if inside and char == "]":
            inside = False
        i += 1
    return count


def regex_complexity(pattern: str, flags: int = 0) -> int:
    """Сложность регулярки по правилу Sonar S5843 (эталон: 24 — см. тесты).

    ГРАНИЦА: ``re._parser`` схлопывает альтернативу из односимвольных ветвей
    (``(a|b)``) в класс символов, и такая ``|`` в счёт не попадает — в отличие
    от Sonar. Счёт получается заниженным, то есть чекер скорее промолчит, чем
    соврёт; на калибровочных регулярках отчёта расхождения нет.
    """
    return _structural_cost(_reparser.parse(pattern, flags), 1) + _count_char_classes(pattern)


def _scoped_regex_flags(flags: int, added: int, removed: int) -> int:
    type_flags = re.ASCII | re.UNICODE | re.LOCALE
    if added & type_flags:
        flags &= ~type_flags
    return (flags | added) & ~removed


def _regex_children(name: str, value, flags: int) -> Iterator[tuple[Iterable, int]]:
    if name == "SUBPATTERN":
        yield value[3], _scoped_regex_flags(flags, value[1], value[2])
    elif name in _REPEATS:
        yield value[2], flags
    elif name in _ASSERTS:
        yield value[1], flags
    elif name == "BRANCH":
        yield from ((branch, flags) for branch in value[1])
    elif name == "ATOMIC_GROUP":
        yield value, flags
    elif name == "GROUPREF_EXISTS":
        yield from ((branch, flags) for branch in value[1:] if branch)


def _ascii_classes(seq: Iterable, flags: int) -> Iterator[frozenset]:
    for op, value in seq:
        if op.name == "IN" and flags & re.ASCII:
            yield frozenset((kind.name, item) for kind, item in value)
        for child, child_flags in _regex_children(op.name, value, flags):
            yield from _ascii_classes(child, child_flags)


def _regex_hint(pattern: str, flags: int = 0) -> str | None:
    parsed = _reparser.parse(pattern, flags)
    bodies = list(_ascii_classes(parsed, parsed.state.flags))
    if _WORD_RANGES in bodies:
        return "S6353 use '\\w' instead of '[A-Za-z0-9_]'"
    if _DIGIT_RANGES in bodies:
        return "S6353 use '\\d' instead of '[0-9]'"
    match = _RELUCTANT.search(pattern)
    if match:
        char = match.group(1).lstrip("\\")
        return f"S5857 replace reluctant quantifier before {char!r} with '[^{char}]*'"
    return None


def check_regex(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for lineno, pattern, flags in _regex_call_patterns(tree):
        try:
            hint = _regex_hint(pattern, flags)
            score = regex_complexity(pattern, flags)
        except (re.error, ValueError):
            continue
        if hint:
            out.append(f"{path}:{lineno}: {hint} in {pattern!r}")
        if score > REGEX_MAX:
            out.append(
                f"{path}:{lineno}: S5843 simplify this regular expression to reduce its complexity "
                f"from {score} to the {REGEX_MAX} allowed"
            )
    return out


# --------------------------------------------------------------------------- #
# S5713 — лишний класс в кортеже except
# --------------------------------------------------------------------------- #
def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Узлы одной области: тела вложенных функций/классов разбираются отдельно."""
    stack = list(scope.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_SCOPES, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _bound_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (*_FUNC_DEFS, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return node.names
    return []


def _import_bindings(node: ast.AST) -> Iterator[tuple[str, str | None]]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            yield alias.asname or root, alias.name if alias.asname else root
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            target = f"{node.module}.{alias.name}" if node.module and node.level == 0 else None
            yield alias.asname or alias.name, target


def _scope_bindings(scope: ast.AST) -> dict[str, set]:
    """Все связывания имени в одной области, без тел вложенных определений."""
    bindings: dict[str, set] = {}
    direct = set(scope.body)
    for node in _scope_nodes(scope):
        for name in _bound_names(node):
            bindings.setdefault(name, set()).add(None)
        for name, target in _import_bindings(node):
            binding = (target, node.lineno) if node in direct and target else None
            bindings.setdefault(name, set()).add(binding)
    return bindings


def _scope_aliases(scope: ast.AST, inherited: dict) -> dict:
    """Только однозначные импорты. Присваивания и условные импорты затемняют имя."""
    bindings = _scope_bindings(scope)
    if isinstance(scope, _FUNC_DEFS):
        for arg in ast.walk(scope.args):
            if isinstance(arg, ast.arg):
                bindings.setdefault(arg.arg, set()).add(None)
    aliases = inherited.copy()
    aliases.update((name, next(iter(values)) if len(values) == 1 else None) for name, values in bindings.items())
    return aliases


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _import_stdlib(dotted: str) -> object | None:
    """Импортировать ``a.b.C`` — только если ``a`` из стандартной библиотеки."""
    parts = dotted.split(".")
    if parts[0] not in sys.stdlib_module_names:
        return None
    rest: list[str] = []
    module = None
    for cut in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        rest = parts[cut:]
        break
    if module is None:
        return None
    obj: object = module
    for attr in rest:
        obj = getattr(obj, attr, None)
        if obj is None:
            return None
    return obj


def _resolve_exception(node: ast.expr, aliases: dict) -> type | None:
    if "*" in aliases:
        return None
    dotted = _dotted_name(node)
    if dotted is None:
        return None
    head, _, tail = dotted.partition(".")
    binding = aliases.get(head, (head, 0))
    if binding is None or binding[1] > node.lineno:
        return None
    full = binding[0] + (f".{tail}" if tail else "")
    obj = getattr(builtins, full, None) if "." not in full else None
    if obj is None:
        obj = _import_stdlib(full)
    return obj if isinstance(obj, type) and issubclass(obj, BaseException) else None


def _redundant_pair(classes: list[tuple[ast.expr, type]], index: int) -> tuple[str, str] | None:
    """Перекрыт ли ``index``-й класс каким-то из соседей в том же кортеже."""
    node, cls = classes[index]
    for other_index, (other_node, other) in enumerate(classes):
        if other_index == index:
            continue
        same_object = cls is other and other_index < index
        if same_object or (cls is not other and issubclass(cls, other)):
            return _dotted_name(node) or cls.__name__, _dotted_name(other_node) or other.__name__
    return None


def _scope_handlers(scope: ast.AST, inherited: dict) -> Iterator[tuple[ast.ExceptHandler, dict]]:
    aliases = _scope_aliases(scope, inherited)
    for node in _scope_nodes(scope):
        if isinstance(node, ast.ExceptHandler):
            yield node, aliases
        elif isinstance(node, (*_FUNC_DEFS, ast.ClassDef)):
            # Атрибуты класса не являются замыканием для его методов.
            parent = inherited if isinstance(scope, ast.ClassDef) else aliases
            yield from _scope_handlers(node, parent)


def check_redundant_except(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for node, aliases in _scope_handlers(tree, {}):
        if not isinstance(node.type, ast.Tuple):
            continue
        classes = [(el, cls) for el in node.type.elts if (cls := _resolve_exception(el, aliases)) is not None]
        for index, (element, _) in enumerate(classes):
            pair = _redundant_pair(classes, index)
            if pair:
                out.append(
                    f"{path}:{element.lineno}: S5713 remove this redundant exception class {pair[0]!r}; "
                    f"it derives from {pair[1]!r} which is already caught"
                )
    return out


# --------------------------------------------------------------------------- #
# S3516 — функция всегда возвращает одно и то же
# --------------------------------------------------------------------------- #
def _own_nodes(func: ast.AST) -> Iterator[ast.AST]:
    """Узлы функции без вложенных функций и лямбд — у тех свои ``return``."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (*_FUNC_DEFS, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _endless_loop_falls_through(loop: ast.While | ast.For | ast.AsyncFor) -> bool:
    endless = isinstance(loop, ast.While) and isinstance(loop.test, ast.Constant) and bool(loop.test.value)
    if not endless:
        return True
    return any(isinstance(node, ast.Break) for node in _own_nodes(loop))


def _falls_through(body: list[ast.stmt]) -> bool:
    """Может ли выполнение дойти до конца блока (то есть до неявного None)."""
    if not body:
        return True
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return False
    if isinstance(last, ast.If):
        return _falls_through(last.body) or _falls_through(last.orelse)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        # __exit__/__aexit__ может подавить исключение даже в теле с return.
        return True
    if isinstance(last, ast.Try):
        branches = [last.body + last.orelse, *(h.body for h in last.handlers)]
        return _falls_through(last.finalbody) and any(_falls_through(b) for b in branches)
    if isinstance(last, (ast.While, ast.For, ast.AsyncFor)):
        return _endless_loop_falls_through(last)
    return True


def _same_constant_returns(func: ast.AST) -> ast.Constant | None:
    """Единственная непустая константа, если все ``return`` возвращают её."""
    values = [n.value for n in _own_nodes(func) if isinstance(n, ast.Return)]
    if len(values) < 2 or any(v is None or not isinstance(v, ast.Constant) for v in values):
        return None
    first = values[0]
    same = all(v.value == first.value and type(v.value) is type(first.value) for v in values)
    return first if first.value is not None and same else None


def check_same_return(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for func in ast.walk(tree):
        if not isinstance(func, _FUNC_DEFS):
            continue
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own_nodes(func)):
            continue
        constant = _same_constant_returns(func)
        if constant is not None and not _falls_through(func.body):
            out.append(
                f"{path}:{func.lineno}: S3516 refactor this method to not always return the same value "
                f"({constant.value!r})"
            )
    return out


# --------------------------------------------------------------------------- #
# S3358 — тернарный внутри тернарного
# --------------------------------------------------------------------------- #
def check_nested_conditional(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.IfExp):
            continue
        for inner in (node.body, node.orelse, node.test):
            if isinstance(inner, ast.IfExp):
                out.append(
                    f"{path}:{inner.lineno}: S3358 extract this nested conditional expression "
                    f"into an independent statement"
                )
    return out


# --------------------------------------------------------------------------- #
# S4144 — одинаковые тела функций
# --------------------------------------------------------------------------- #
def _body_fingerprint(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) < MIN_IDENTICAL_BODY:
        return None
    return "".join(ast.dump(stmt) for stmt in body)


def _sibling_functions(tree: ast.AST) -> Iterator[list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Функции модуля между собой и методы одного класса между собой.

    Сравнивать что-то ещё нельзя: одинаковые по телу замыкания внутри разных
    функций — обычный приём, а не дубль.
    """
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.Module, ast.ClassDef)):
            yield [n for n in scope.body if isinstance(n, _FUNC_DEFS)]


def check_identical_bodies(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for group in _sibling_functions(tree):
        seen: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for func in group:
            key = _body_fingerprint(func)
            if key is None:
                continue
            twin = seen.setdefault(key, func)
            if twin is not func:
                out.append(
                    f"{path}:{func.lineno}: S4144 update this function so that its implementation "
                    f"is not identical to {twin.name} on line {twin.lineno}"
                )
    return out


# --------------------------------------------------------------------------- #
# S108 — пустой блок
# --------------------------------------------------------------------------- #
_BLOCK_OWNERS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)


def _blocks_of(node: ast.AST) -> Iterator[tuple[str, list[ast.stmt]]]:
    """Блоки, которые Sonar считает пустыми. ``except`` он не проверяет."""
    for field in ("body", "orelse", "finalbody"):
        block = getattr(node, field, None)
        if block:
            yield field, block


def _block_start(node: ast.AST, field: str, block: list[ast.stmt], headers: list[tokenize.TokenInfo]) -> int:
    if field == "body":
        return node.lineno
    keyword = "finally" if field == "finalbody" else "else"
    return max(
        (
            tok.start[0]
            for tok in headers
            if tok.string == keyword
            and tok.start[1] == node.col_offset
            and node.lineno <= tok.start[0] <= block[0].lineno
        ),
        default=block[0].lineno,
    )


def check_empty_blocks(path: Path, tree: ast.AST, source: str) -> list[str]:
    comment_lines = _comment_lines(source)
    headers = [tok for tok in _python_tokens(source) if tok.type == tokenize.NAME and tok.string in {"else", "finally"}]
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, _BLOCK_OWNERS):
            continue
        for field, block in _blocks_of(node):
            only_pass = len(block) == 1 and isinstance(block[0], ast.Pass)
            start = _block_start(node, field, block, headers)
            if only_pass and not (comment_lines & set(range(start, block[0].lineno + 1))):
                out.append(f"{path}:{block[0].lineno}: S108 either remove or fill this block of code")
    return out


# --------------------------------------------------------------------------- #
# S5527 — выключенная проверка сертификата TLS
# --------------------------------------------------------------------------- #
_TLS_OFF_ATTRS = {"check_hostname": False, "verify_mode": "CERT_NONE"}


def _tls_assign(node: ast.Assign) -> str | None:
    for target in node.targets:
        if not isinstance(target, ast.Attribute) or target.attr not in _TLS_OFF_ATTRS:
            continue
        expected = _TLS_OFF_ATTRS[target.attr]
        value = node.value
        if isinstance(value, ast.Constant) and value.value is expected:
            return f"{target.attr} = False"
        if expected == "CERT_NONE" and (_dotted_name(value) or "").endswith("CERT_NONE"):
            return f"{target.attr} = ssl.CERT_NONE"
    return None


def _disables_tls(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        name = _dotted_name(node.func) or ""
        return "_create_unverified_context()" if name.endswith("_create_unverified_context") else None
    return _tls_assign(node) if isinstance(node, ast.Assign) else None


def check_tls_verification(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        what = _disables_tls(node)
        if what:
            out.append(
                f"{path}:{node.lineno}: S5527 enable server hostname verification on this SSL/TLS connection ({what})"
            )
    return out


# --------------------------------------------------------------------------- #
# Web:S5256 — таблица без описания
# --------------------------------------------------------------------------- #
_TABLE_LABELS = frozenset({"aria-label", "aria-labelledby", "aria-describedby", "summary", "title"})


class _TableAudit(HTMLParser):
    """Таблицы без ``<caption>`` и без ARIA-описания: список их строк."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_tables: list[tuple[int, bool]] = []
        self.bad: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            described = any(name in _TABLE_LABELS and value for name, value in attrs)
            self.open_tables.append((self.getpos()[0], described))
        elif tag == "caption" and self.open_tables:
            line, _ = self.open_tables[-1]
            self.open_tables[-1] = (line, True)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.open_tables:
            line, described = self.open_tables.pop()
            if not described:
                self.bad.append(line)


def check_html_tables(path: Path, text: str) -> list[str]:
    audit = _TableAudit()
    audit.feed(text)
    audit.close()
    unclosed = [line for line, described in audit.open_tables if not described]
    return [
        f"{path}:{line}: Web:S5256 add a description to this table (<caption>, aria-label or summary)"
        for line in sorted(audit.bad + unclosed)
    ]


# --------------------------------------------------------------------------- #
# Обход файлов и запуск
# --------------------------------------------------------------------------- #
HTML_SUFFIX = ".html"
SUFFIXES = (".py", HTML_SUFFIX)


def _is_skipped(path: Path) -> bool:
    return bool(SKIP_DIRS.intersection(path.parts)) or path.name in SKIP_FILES


def iter_files(paths: list[str]) -> Iterator[Path]:
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found = (p for suffix in SUFFIXES for p in path.rglob(f"*{suffix}"))
            yield from sorted(p for p in found if not _is_skipped(p))
        elif path.suffix in SUFFIXES and not _is_skipped(path):
            yield path


class _HtmlSuppressions(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.lines: set[int] = set()

    def handle_comment(self, data: str) -> None:
        start = self.getpos()[0]
        self.lines.update(start + i for i, line in enumerate(data.splitlines()) if "NOSONAR" in line)


def _python_tokens(source: str) -> Iterator[tokenize.TokenInfo]:
    try:
        yield from tokenize.generate_tokens(io.StringIO(source).readline)
    except (tokenize.TokenError, SyntaxError):
        return  # Ошибку исходника отдельно сообщает ast.parse; не угадываем комментарии.


def _nosonar_lines(source: str, *, html: bool = False) -> set[int]:
    """Подавление только настоящими комментариями, не литералами/атрибутами."""
    if html:
        parser = _HtmlSuppressions()
        parser.feed(source)
        return parser.lines
    return {tok.start[0] for tok in _python_tokens(source) if tok.type == tokenize.COMMENT and "NOSONAR" in tok.string}


def _comment_lines(source: str) -> set[int]:
    """Строки с настоящими комментариями — по токенам, а не по виду строки.

    Хвостовой комментарий (``pass  # ждём следующего хода``) для Sonar тоже
    объяснение пустого блока, а ``#`` внутри строкового литерала — нет.
    """
    return {tok.start[0] for tok in _python_tokens(source) if tok.type == tokenize.COMMENT}


def _line_of(finding: str) -> int:
    try:
        return int(finding.split(":", 2)[1])
    except (IndexError, ValueError):
        return 0


def check_python(path: Path, source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return [f"{path}: cannot parse: {exc}"]
    return [
        *check_duplicate_literals(path, tree),
        *check_regex(path, tree),
        *check_redundant_except(path, tree),
        *check_same_return(path, tree),
        *check_nested_conditional(path, tree),
        *check_identical_bodies(path, tree),
        *check_empty_blocks(path, tree, source),
        *check_tls_verification(path, tree),
    ]


def check_file(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: cannot read: {exc}"]
    findings = check_html_tables(path, source) if path.suffix == HTML_SUFFIX else check_python(path, source)
    nosonar = _nosonar_lines(source, html=path.suffix == HTML_SUFFIX)
    return [f for f in findings if _line_of(f) not in nosonar]


def main(argv: list[str]) -> int:
    findings: list[str] = []
    for path in iter_files(argv or ["."]):
        findings += check_file(path)
    for line in findings:
        print(line)
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
