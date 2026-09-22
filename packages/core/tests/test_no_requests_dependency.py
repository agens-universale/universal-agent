"""Гейт: ядро не расширяет зависимость от ``requests``.

Уход с requests был отдельной целью — не ради асинхронности (у httpx есть
синхронный ``Client`` и переезд был механическим), а ради одной зависимости
вместо двух на один и тот же HTTP. Ветка её достигла: не осталось ни одного
модуля.

При вливании пакетов 08.09.2026 develop принёс обратно три модуля на requests:
витрину уведомлений и оба клиента MCP-порта. Гейт тогда сменил форму на СПИСОК,
замороженный по именам, чтобы зависимость не расползалась дальше.

**Список снова пуст.** Клиенты порта переехали вместе с переводом легаси-MCP на
`httpx.AsyncClient`; витрине уведомлений requests был нужен ровно ради одной
строки — `isinstance(exc, requests.exceptions.HTTPError)`, — а у httpx
исключение той же формы. Зависимость снята из манифеста, и первый тест ниже
следит, чтобы она туда не вернулась молча.

Тест сторожит откат с двух сторон: манифест и код. Проверка на импорт
сделана статически, а не через ``import requests`` в подпроцессе: в dev-окружении
requests всё равно стоит транзитивно (его тянут соседние пакеты), и
динамическая проверка там ничего не поймала бы.
"""

import ast
import tomllib

import pytest
from gc_testkit.paths import REPO

_SRC = REPO / "packages" / "core" / "src" / "gc_core"
_MANIFEST = REPO / "packages" / "core" / "pyproject.toml"


#: Модули ядра, которым requests пока законен. Список ЗАМОРОЖЕН: он существует,
#: чтобы зависимость не расползалась дальше, пока её не сняли совсем.
_REQUESTS_ALLOWED: set[str] = set()


def test_manifest_declares_requests_only_while_the_list_is_not_empty():
    """Зависимость в манифесте и список модулей обязаны сходиться.

    Пустой список означает, что переводить больше нечего — и тогда `requests`
    обязан уйти из зависимостей, иначе он останется висеть навсегда.
    """
    manifest = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    deps = manifest["project"]["dependencies"]
    declared = [d for d in deps if d.split("[")[0].strip().lower() == "requests"]
    if _REQUESTS_ALLOWED:
        assert declared, (
            "модули из _REQUESTS_ALLOWED импортируют requests, а в зависимостях его нет — установка развалится"
        )
    else:
        assert not declared, f"переводить нечего, а requests остался: {declared}"


def _modules_importing_requests() -> list[str]:
    """Модули ядра, где в КОДЕ есть import requests (не в комментарии)."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — синтаксис ловит другой гейт
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "requests" or n.startswith("requests.") for n in names):
                offenders.append(str(path.relative_to(_SRC)))
                break
    return sorted(set(offenders))


def test_no_new_module_imports_requests():
    """Список модулей на requests не растёт — и не протухает.

    AST, а не поиск по тексту: слово «requests» законно встречается в
    докстроках, где объяснено, как было раньше и почему уехали.

    Проверка двусторонняя. Лишнее имя — расползание зависимости. Отсутствующее —
    модуль уже перевели, а имя в списке забыли снять: тогда список перестаёт
    показывать реальный объём долга.
    """
    offenders = set(_modules_importing_requests())
    extra = sorted(offenders - _REQUESTS_ALLOWED)
    stale = sorted(_REQUESTS_ALLOWED - offenders)
    assert not extra, f"requests импортируется в новых модулях: {extra}"
    assert not stale, f"модули уже без requests, снимите их со списка _REQUESTS_ALLOWED: {stale}"


@pytest.mark.parametrize(
    "module",
    [
        "gc_core.aio",
        "gc_core.utils.http_session",
        "gc_core.utils.connect_backend",
        "gc_core.utils.no_proxy",
        "gc_core.utils.http_proxy",
        "gc_core.utils.backend_errors",
        "gc_core.utils.prod_auth",
        "gc_core.model_picker",
        "gc_core.runtime_notices",
        "gc_core.tools.connect_mcp.api",
        "gc_core.tools.connect_mcp.transport",
        "gc_core.tools.connect_mcp.registry",
    ],
)
def test_key_modules_import_cleanly(module):
    """Модули, которые раньше сидели на requests, поднимаются как есть."""
    import importlib

    assert importlib.import_module(module) is not None
