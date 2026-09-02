#!/usr/bin/env python3
"""Сетка против трёх классов поломок импорта, переживших кодмод.

Первые два класса вылезли при вливании develop 19.08.2026 и были найдены не
тестами, а разбором 138 падений постфактум — значит порог обнаружения низкий и
нужна проверка, которая гоняется сама. Третий нашло внешнее ревью 28.08.2026:
под обе прежние сетки он не попадал вовсе (A2).

**Класс 1: `import a.b` вместо `from a import b`.** Кодмод переписывал плоский
`import arg_parser` в `import gc_cli.arg_parser`. Но `import a.b` связывает имя
`a`, а не `b`: после такой правки 24 обращения к голому `arg_parser.` в
`cli.py` стали NameError. Живой запуск падал, а набор — нет, потому что тесты
на этот путь патчили модуль целиком.

**Класс 2: имя, которого нет в модуле.** develop приватизировал
`default_config_path` → `_default_config_path`, а разрешение конфликта
сохранило наш путь модуля со старым именем. Ловится только тем, что кто-то
дойдёт до этой строки.

**Класс 3: дореформенное плоское имя в узле импорта.** `from usage.account
import ...` пережил кодмод и остался невидимым для обеих сеток сразу:
`module_literals` знает все старые имена, но обходит только Call/Subscript/
JoinedStr/Compare и до узлов импорта не доходит, а `check_missing_names`
смотрит только на наши НОВЫЕ префиксы. Граница проходила не по префиксу, а по
типу узла.

Проверки идут по AST, без ИСПОЛНЕНИЯ проверяемого модуля, но НЕ без импорта:
`find_spec("gc_core.config")` импортирует пакеты-родители, и за один `scan()`
исполняются `__init__` полусотни с лишним сторонних пакетов (замерено: 57
верхнеуровневых имён — textual, uvicorn, starlette, lxml, trafilatura,
pydantic, mcp, httpx, requests…). Тело проверяемого модуля не исполняется —
по той же причине, по которой `module_literals.verify()` не проверяет
атрибуты: побочные эффекты, разбор argv, сетевые клиенты.

Отсюда же следует, что в окружении без зависимостей часть модулей проверить
нельзя. Такие модули идут в ПРОПУСКИ и попадают в отчёт числом: раньше их
глушил голый `except`, и прогон без зависимостей давал не «0 находок», а 173
ложных срабатывания (N5).

ГРАНИЦА ПРИМЕНИМОСТИ. Класс 1 проверяется точно. Класс 2 — по объявлениям
модульного уровня, поэтому модуль, который лепит имена динамически
(`globals().update`, `setattr(sys.modules[...])`), даст ложное срабатывание;
такие модули пропускаются по признаку `__getattr__` или `import *`. Имя,
приходящее из ветки `try/except ImportError`, видно — оба варианта попадают в
объявления.

Класс 3 судит только узлы импорта с `level == 0` и только по ВЕРХНЕМУ имени:
если сторонний дистрибутив когда-нибудь назовётся `config` или `tools`, его
импорт станет ложным срабатыванием. Сейчас таких нет — прогон по всему дереву
даёт ноль находок на 1364 файлах. Пятнадцать каталогов тестов носят
дореформенные имена (`packages/core/tests/{tools,config,config_modules,
instructions,message_policy,project_memory,security,updater,utils,voice,
voice_runtime}`, `packages/tui/tests/{diff_tui,voice,voice_runtime}`,
`packages/cli/tests/config`), но ни один из них не пакет — `__init__.py` нет
ни в одном, и импортов из них в дереве нет.

Использование:
    uv run python -m gc_testkit.import_health           # отчёт
    uv run python -m gc_testkit.import_health --json    # {"findings": …, "skipped": …}

Машинный вывод 28.08.2026 сменил форму: был список находок, стал объект. Иначе
он скрывал бы пропуски ровно так же, как их скрывал человеческий отчёт (N5), а
скрытый пропуск неотличим от чистого прогона. Прежнюю форму никто не разбирал:
grep по py/toml/md/yml/sh не нашёл ни одного потребителя, CI-конфигов в дереве
нет вовсе, а соседний `module_literals --json` и раньше отдавал объект.

Код возврата: 0 — чисто, 1 — есть находки.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path

from gc_testkit.paths import REPO, package_src_dirs
from gc_testkit.sources import ensure_sources_on_path

#: Каталоги, где ищем питон. Тот же принцип, что в module_literals: список
#: исключений, а не включений, — новый каталог с кодом подхватывается сам.
SKIP_DIRS = {
    ".git", ".venv", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "__pycache__", "dist", "build", "node_modules", "docs",
}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    kind: str      # dotted_bind | missing_name | missing_module | legacy_toplevel
    detail: str


#: Дореформенные плоские имена верхнего уровня — всё, что до разбиения на
#: пакеты лежало в `src/`. Список ЗАМОРОЖЕН здесь константой намеренно: считать
#: его из git (`git ls-tree develop:src`) значит потерять сетку в shallow-клоне
#: и после того, как объект будет удалён сборщиком. `module_map.json` рядом
#: держит те же имена, но отвечает на другой вопрос — куда имя переехало;
#: расхождение между ними ловит `test_legacy_list_covers_module_map`.
LEGACY_TOPLEVEL = frozenset({
    "agent_executor", "app_version", "arg_parser", "backends",
    "bundled_assets", "cli", "config", "config_modules", "config_origin",
    "config_raw", "config_sync", "constants", "diff_tui",
    "execution_workspace", "instructions", "intent_guard", "intent_log",
    "intent_scope", "manual_compaction", "mcp_daemon_autostart",
    "mcp_runtime", "message_format", "message_policy", "message_projection",
    "model_picker", "model_router", "onboarding",
    "onboarding_config_writer", "onboarding_fields", "onboarding_wizard",
    "persistence", "processing_responses", "project_memory",
    "request_backend_policy", "rest_api", "run_context",
    "run_summary_coordinator", "runtime_notices", "security", "serve_entry",
    "server", "server_cleanup_threads", "startup_notices", "states",
    "summarize", "swarm", "thread_store", "tools", "trace_id", "tui",
    "tui_entry", "turn_boundary", "updater", "upgrade_gate", "usage",
    "utils", "voice", "voice_runtime",
})

#: Модули, которые проверить НЕ удалось: имя → причина. Копится за процесс, а
#: не за один `scan()`: ответы кэшированы и от повторного прогона не меняются.
#: Пустой словарь и «пропущено 150» — разные результаты, и отчёт обязан их
#: различать: молчаливый пропуск неотличим от чистого прогона, ровно на этом
#: сетка и молчала (N5).
_SKIPPED: dict[str, str] = {}


def skipped() -> dict[str, str]:
    """Модули, которые не удалось проверить, и почему. Копия, а не сам кэш."""
    return dict(_SKIPPED)


def _missing_is_self(exc: BaseException, mod_name: str) -> bool:
    """`ModuleNotFoundError` — про САМО проверяемое имя или про чужое?

    `find_spec("gc_core.config_modules.env_vars")` импортирует родителя
    `gc_core.config_modules`, поэтому наружу летит и ошибка про чужую
    зависимость (`No module named 'pydantic_settings'`). Отличить два случая
    можно только по `e.name`: своё имя (или его пакет-родитель) — модуля
    правда нет, это находка; чужое — мы просто не смогли проверить, это
    пропуск. Без этого различения прогон в интерпретаторе без зависимостей
    давал 173 ложных срабатывания.
    """
    name = getattr(exc, "name", None)
    if not isinstance(exc, ModuleNotFoundError) or not name:
        return False
    return mod_name == name or mod_name.startswith(f"{name}.")


def _skip(mod_name: str, exc: BaseException) -> None:
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        _SKIPPED.setdefault(mod_name, f"нет зависимости {exc.name!r}")
    else:
        _SKIPPED.setdefault(mod_name, f"{type(exc).__name__}: {exc}")


@lru_cache(maxsize=None)
def _module_exists(mod_name: str) -> bool | None:
    """Есть ли такой модуль. None — проверить не удалось, см. `_SKIPPED`.

    Обёртка живёт ради кэша: за один `scan()` одни и те же имена спрашиваются
    тысячами повторов, а `find_spec` каждый раз тянет импорт пакетов-родителей.
    """
    try:
        return importlib.util.find_spec(mod_name) is not None
    except Exception as exc:
        if _missing_is_self(exc, mod_name):
            return False
        _skip(mod_name, exc)
        return None


def _our_prefixes() -> set[str]:
    """Верхнеуровневые имена наших дистрибутивов: `gc_core`, `gc_tui`, ...

    Каталоги спрашиваются функцией `paths.package_src_dirs()` — тем же
    источником, из которого их берёт бутстрап `sys.path` (B8).
    """
    names: set[str] = set()
    for src in package_src_dirs():
        for child in src.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                names.add(child.name)
    return names


#: Корни обхода. `external/litellm` добавлен явно, а не через `REPO.rglob`:
#: соседний `module_literals` судит его с самого начала (он обходит `external`
#: как каталог верхнего уровня), и расхождение двух сеток на ровном месте —
#: приглашение к ошибке. Остальное содержимое `external/` — вендоренные
#: MCP-серверы, чужой код с собственной историей: судить его нашими правилами
#: (`legacy_toplevel` знает имена `config`, `server`, `utils`) значит красить
#: наш прогон на чужом синке.
_SCAN_ROOTS = ("packages", "tests", "integrations", "external/litellm")

#: Вендоренные MCP-серверы — ЧУЖОЙ код со своей историей, и судить его нашими
#: правилами нельзя: `legacy_toplevel` знает имена `config`, `server`, `utils`,
#: и первый же синк с абсолютным `import config` покрасил бы НАШ прогон.
#:
#: Отдельная проверка нужна с тех пор, как вендоры переехали внутрь пакета
#: (`packages/mcp/external/vendor/`): корень `packages` теперь включает их сам.
#: Правило по СОДЕРЖИМОМУ, а не по месту: где бы каталог `external/vendor` ни
#: оказался при следующем переезде, его содержимое остаётся чужим.
_VENDORED = ("external", "vendor")


def _is_vendored(path: Path) -> bool:
    parts = path.parts
    return any(parts[i:i + 2] == _VENDORED for i in range(len(parts) - 1))


def _python_files() -> list[Path]:
    out: list[Path] = []
    for rel in _SCAN_ROOTS:
        base = REPO / rel
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if SKIP_DIRS & set(p.parts) or _is_vendored(p):
                continue
            out.append(p)
    return sorted(out)


def check_dotted_binds(path: Path, tree: ast.AST) -> list[Finding]:
    """`import a.b` связывает `a`; голое `b.` ниже по файлу — NameError."""
    bound: set[str] = set()
    dotted: dict[str, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound.add(alias.asname)
                elif "." in alias.name:
                    dotted.setdefault(alias.name.split(".")[-1], (alias.name, node.lineno))
                else:
                    bound.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.arg,)):
            bound.add(node.arg)

    found: list[Finding] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        if node.id in bound or node.id not in dotted:
            continue
        full, line = dotted.pop(node.id)
        found.append(Finding(
            str(path.relative_to(REPO)), line, "dotted_bind",
            f"`import {full}` связывает `{full.split('.')[0]}`, "
            f"а строка {node.lineno} зовёт голое `{node.id}` — нужно "
            f"`from {full.rsplit('.', 1)[0]} import {node.id}`",
        ))
    return found


def _target_names(target: ast.AST) -> set[str]:
    """Имена в цели присваивания, включая распаковку `a, b = ...`."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {n for t in target.elts for n in _target_names(t)}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


@lru_cache(maxsize=None)
def _module_bindings(mod_name: str) -> frozenset[str] | None:
    """Имена модульного уровня по AST. None — модуль непрозрачен, не судим.

    Кэшируется по той же причине, что и `_module_exists`, и возвращает
    `frozenset`: значение общее для всех вызывающих, портить его нельзя.
    """
    try:
        spec = importlib.util.find_spec(mod_name)
    except Exception as exc:
        if not _missing_is_self(exc, mod_name):
            _skip(mod_name, exc)      # чужая зависимость — пропуск, не находка
        return None
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return None
    try:
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "__getattr__":
                return None          # модуль сам решает, что у него есть
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    return None      # реэкспорт россыпью, состав неизвестен
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names |= _target_names(t)
        elif isinstance(node, ast.AnnAssign):
            names |= _target_names(node.target)
        elif isinstance(node, (ast.If, ast.Try)):
            # try/except ImportError и `if TYPE_CHECKING` — объявления внутри
            # всё равно попадают в модуль, обходим их тела на один уровень.
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(sub, ast.ImportFrom):
                    for alias in sub.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        names |= _target_names(t)
    return frozenset(names)


def check_missing_names(path: Path, tree: ast.AST, ours: set[str]) -> list[Finding]:
    """`from gc_x.y import name` — существует ли `name` в модуле."""
    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        if node.module.split(".")[0] not in ours:
            continue
        bindings = _module_bindings(node.module)
        if bindings is None:
            # `None` от `_module_bindings` означает «модуль непрозрачен», и это
            # обычно законно: namespace-пакет, C-расширение, неустановленная
            # чужая зависимость. Но ТОТ ЖЕ `None` приходит и когда модуля просто
            # НЕТ, — а это самая очевидная поломка импорта, ради которой сетка и
            # написана. Она проходила мимо всех трёх проверок:
            #
            #     from gc_core.nosuch_xyz import thing        → 0 находок
            #     from gc_core.config import nosuch_name_xyz  → 1 находка
            #
            # Различаем ровно тем же приёмом, каким уже различаются «своё
            # отсутствующее имя» и «чужая зависимость»: спрашиваем модуль
            # отдельно. `False` — уверенный ответ «его нет»; `None` — не смогли
            # проверить, и тогда по-прежнему молчим.
            if _module_exists(node.module) is False:
                found.append(Finding(
                    str(path.relative_to(REPO)), node.lineno, "missing_module",
                    f"модуля `{node.module}` не существует",
                ))
            continue
        for alias in node.names:
            if alias.name == "*" or alias.name in bindings:
                continue
            # имя может быть подмодулем пакета; None — проверить не удалось,
            # и тогда это ПРОПУСК, а не находка (раньше `except: pass` ронял
            # управление прямо сюда и объявлял существующий файл несуществующим)
            if _module_exists(f"{node.module}.{alias.name}") is not False:
                continue
            found.append(Finding(
                str(path.relative_to(REPO)), node.lineno, "missing_name",
                f"в `{node.module}` нет имени `{alias.name}`",
            ))
    return found


def check_legacy_toplevel(path: Path, tree: ast.AST) -> list[Finding]:
    """Импорт по дореформенному плоскому имени: `from usage.account import ...`.

    Класс 3 (A2). До 28.08.2026 такой узел не судила ни одна из двух сеток:
    `module_literals` знает все старые имена, но её `collect()` обходит
    Call/Subscript/JoinedStr/Compare и до `Import`/`ImportFrom` не доходит, а
    `check_missing_names` смотрит только на наши НОВЫЕ префиксы. Подсадка
    `from tools.registry import ...` оставляла прогон зелёным.

    `level != 0` пропускаем: относительный импорт разрешается внутри пакета и
    к плоским именам отношения не имеет.
    """
    found: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in LEGACY_TOPLEVEL:
                    found.append(Finding(
                        str(path.relative_to(REPO)), node.lineno, "legacy_toplevel",
                        f"`import {alias.name}` — дореформенное плоское имя "
                        f"`{top}`; после разбиения на пакеты оно живёт внутри "
                        f"дистрибутива (куда именно — module_map.json)",
                    ))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            top = node.module.split(".")[0]
            if top in LEGACY_TOPLEVEL:
                found.append(Finding(
                    str(path.relative_to(REPO)), node.lineno, "legacy_toplevel",
                    f"`from {node.module} import …` — дореформенное плоское имя "
                    f"`{top}`; после разбиения на пакеты оно живёт внутри "
                    f"дистрибутива (куда именно — module_map.json)",
                ))
    return found


def scan() -> list[Finding]:
    """Общая точка для CLI и теста.

    Каталоги исходников кладёт на `sys.path` `gc_testkit.sources` — та же
    функция, что зовёт плагин и соседняя сетка `module_literals`. Своя копия
    цикла здесь была третьей и расходилась с копией в `module_literals`
    источником и порядком (B8).
    """
    ensure_sources_on_path()

    ours = _our_prefixes()
    findings: list[Finding] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        findings += check_dotted_binds(path, tree)
        findings += check_missing_names(path, tree, ours)
        findings += check_legacy_toplevel(path, tree)
    return findings


def _print_skips(skips: dict[str, str]) -> None:
    """Пропущенные модули с причинами — печатаются при ЛЮБОМ исходе.

    Одного счётчика мало: он говорит, что прогон неполон, но не говорит, что
    доставить в окружение. Причины обрезаются, потому что при пустом окружении
    их полторы сотни и все они об одном.
    """
    if not skips:
        return
    print()
    print(f"пропущены (окружение неполное, проверить не смогли): {len(skips)}")
    for name, why in sorted(skips.items())[:20]:
        print(f"  {name} — {why}")
    if len(skips) > 20:
        print(f"  … и ещё {len(skips) - 20}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args()

    findings = scan()
    skips = skipped()
    if args.json:
        print(json.dumps({"findings": [asdict(f) for f in findings],
                          "skipped": skips},
                         ensure_ascii=False, indent=1))
        return 1 if findings else 0

    # Хвост про пропуски — часть ОБОИХ вердиктов: «импорты здоровы» при
    # полутора сотнях непроверенных модулей это ложный зелёный (N5).
    tail = f"; ПРОПУЩЕНО модулей: {len(skips)}" if skips else ""
    if not findings:
        print("импорты здоровы: битых имён и потерянных связываний нет" + tail)
        _print_skips(skips)
        return 0
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    print(f"НАХОДОК: {len(findings)}{tail}")
    for kind, n in sorted(by_kind.items()):
        print(f"  {kind:14s} {n:3d}")
    print()
    for f in findings:
        print(f"  {f.file}:{f.line}  {f.detail}")
    _print_skips(skips)
    return 1


if __name__ == "__main__":
    sys.exit(main())
