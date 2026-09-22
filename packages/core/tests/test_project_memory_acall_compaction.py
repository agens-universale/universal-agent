"""``project_memory`` инлайн не должен ломаться на ветке компакции.

Тул ходит в файлы, поэтому он в наборе «инлайн»
(``test_file_tools_run_inline.py``). Но у него есть вторая, редкая ветка: когда
запись не влезает в ``max_chars``, содержимое ужимает LLM
(``project_memory.py``, overflow path → ``compaction.acompact_text``).

Эта ветка — единственная в ``append``, которая ходит по сети, и ждать её надо
``await``'ом. Пока тул уезжал в поток из пула, разница не была видна: там можно
было крутить свой ``asyncio.run``. Инлайн на loop вызывающего так уже нельзя.

Тест дорог тем, что его отсутствие ничего не подсветило: полный набор ядра
проходит зелёным и с дефектом — ветку переполнения через ``acall`` не трогает
ни один другой тест.
"""

import json

import pytest
from gc_core.project_memory import compaction
from gc_core.tools.project.project_memory import ProjectMemory
from gc_testkit.helpers.tool_ctx import local_ctx


@pytest.fixture
def memory_tool(tmp_path, monkeypatch):
    """Тул с крошечным лимитом — любая вторая запись уходит в компакцию."""
    path = tmp_path / "project_memory.md"
    path.write_text("уже записанное содержимое\n", encoding="utf-8")

    tool = ProjectMemory(ctx=local_ctx(), state=None)
    monkeypatch.setattr(tool, "_path", path, raising=False)
    monkeypatch.setattr(tool, "_max_chars", 40, raising=False)
    monkeypatch.setattr(tool, "_max_append_chars", 10_000, raising=False)
    monkeypatch.setattr(tool, "_backup", None, raising=False)
    return tool


@pytest.fixture
def fake_compaction(monkeypatch):
    """Подменяем ТОЛЬКО сетевой лист: сам путь к нему проверяем настоящий."""
    seen = {}

    async def _acompact(text, ctx, state=None):
        seen["called"] = "async"
        return compaction.CompactionResult(ok=True, text="ужато", error_type=None, error_message=None)

    monkeypatch.setattr(compaction, "acompact_text", _acompact)
    return seen


async def test_acall_awaits_compaction_natively(memory_tool, fake_compaction):
    """Главная проверка: инлайн-вход ждёт корутинную компакцию."""
    out = json.loads(await memory_tool.acall(action="append", content="ещё один длинный кусок текста"))

    assert out["ok"] is True, out
    assert fake_compaction["called"] == "async"


async def test_acall_compaction_does_not_start_a_nested_loop(memory_tool, monkeypatch):
    """Компакция не поднимает свой event loop поверх живого.

    Именно так дефект выглядит у пользователя: не «выбран не тот двойник», а
    исключение из середины тула. Зонд — по ``asyncio.run``: он из-под живого
    loop падает, и если ветка снова начнёт его звать, тест это увидит.
    """
    import asyncio

    def _forbidden(*_a, **_kw):
        raise AssertionError("компакция подняла вложенный asyncio.run")

    monkeypatch.setattr(asyncio, "run", _forbidden)

    async def _acompact(text, ctx, state=None):
        return compaction.CompactionResult(ok=True, text="ужато", error_type=None, error_message=None)

    monkeypatch.setattr(compaction, "acompact_text", _acompact)

    out = json.loads(await memory_tool.acall(action="append", content="ещё один длинный кусок текста"))
    assert out["ok"] is True, out


def test_tool_has_no_sync_entry(memory_tool):
    """Синхронного входа нет: форма одна, звать компакцию мимо ``await`` нечем."""
    assert not callable(memory_tool), "у project_memory вернулся синхронный вход"


async def test_acall_read_path_needs_no_compaction(memory_tool, fake_compaction):
    """Чтение компакцию не трогает — обычная файловая операция инлайн."""
    out = json.loads(await memory_tool.acall(action="read", content=""))

    assert out["ok"] is True, out
    assert "called" not in fake_compaction
