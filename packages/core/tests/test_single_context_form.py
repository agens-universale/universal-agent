"""Сборка контекста — одна, и она корутинная.

Форм было две: синхронная `create_run_context` и `acreate_run_context`. docs
объясняли синхронную привязкой MCP-сессий к loop'у, и объяснение было неверным
— синхронная форма уводила дискавери в `run_coro_blocking`, то есть на такой
же одноразовый loop, умиравший до первого хода.

Настоящая причина была проще: фабрика исполнителя в TUI — обычная функция без
своего loop. Теперь она заводит его сама (`asyncio.run`), ровно как уже делает
на каждый ход, а вторая сборка снята.

Проверяется свойство, а не наличие строк: имени `create_run_context` не должно
быть ни в экспорте ядра, ни в вызовах фронтендов.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FRONTENDS = ("packages/tui/src", "packages/cli/src", "packages/server/src")


def test_core_exports_one_form():
    import gc_core.run_context as rc

    assert hasattr(rc, "acreate_run_context")
    assert not hasattr(rc, "create_run_context"), (
        "вернулась вторая нативная сборка контекста"
    )
    assert "create_run_context" not in rc.__all__


def test_the_form_carries_its_real_signature():
    """Публичная корутина обязана называть параметры, а не прятать их в kwargs.

    Пока рядом жила синхронная форма, публичной была `**kwargs`-обёртка над
    приватным телом. Встраивающий не мог прочитать контракт интроспекцией, а
    `inspect.signature` показывал два параметра вместо шестнадцати.
    """
    import inspect

    from gc_core.run_context import acreate_run_context

    params = inspect.signature(acreate_run_context).parameters
    assert "kwargs" not in params, "сигнатура снова спрятана за **kwargs"
    for name in (
        "thread_store",
        "tools_cache",
        "os_backend",
        "choice_provider",
    ):
        assert name in params, f"параметр {name} пропал из публичной сигнатуры"


@pytest.mark.parametrize("pkg", FRONTENDS)
def test_no_frontend_builds_the_context_synchronously(pkg):
    """Фронтенды собирают контекст только корутинной формой."""
    offenders = []
    for path in (REPO / pkg).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = (
                f.attr
                if isinstance(f, ast.Attribute)
                else getattr(f, "id", None)
            )
            if name == "create_run_context":
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], f"синхронная сборка контекста: {offenders}"


def test_docs_no_longer_claim_a_second_form():
    """docs обещали вторую сборку и объясняли её неверно — обещание снято."""
    text = (REPO / "docs/async.md").read_text(encoding="utf-8")

    assert "Единственное исключение — `create_run_context`" not in text
    assert (
        "MCP-сессии привязаны к тому loop'у, на котором\nзаведены" not in text
    )
