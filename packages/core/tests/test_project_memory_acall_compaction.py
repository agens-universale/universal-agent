"""``project_memory`` инлайн не должен ломаться на ветке компакции.

Тул ходит в файлы, поэтому он в наборе «инлайн»
(``test_file_tools_run_inline.py``). Но у него есть вторая, редкая ветка: когда
запись не влезает в ``max_chars``, содержимое ужимает LLM
(``project_memory.py``, overflow path → ``compaction.compact_text``).

Синхронный ``compact_text`` — шим через портал
(``project_memory/compaction.py:100-104``: ``get_portal().run(...)``), а портал
из-под живого event loop поднимает ``SyncApiInsideEventLoopError``
(``utils/portal.py``) — намеренно, чтобы не блокировать чужой loop молча.

Пока тул уезжал в поток из пула, живого loop под ним не было и портал работал.
Инлайн на loop эта же ветка падает. Поэтому ``acall`` не может быть просто
``self(**kwargs)``: компакцию он обязан ждать НАТИВНО, через ``acompact_text``.

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
        return compaction.CompactionResult(
            ok=True, text="ужато", error_type=None, error_message=None
        )

    def _compact(*args, **kwargs):
        # Настоящий синхронный двойник уходит в портал; здесь важно лишь
        # зафиксировать, что тул выбрал ЕГО, а не корутину.
        seen["called"] = "sync-portal"
        return compaction.CompactionResult(
            ok=True, text="ужато", error_type=None, error_message=None
        )

    monkeypatch.setattr(compaction, "acompact_text", _acompact)
    monkeypatch.setattr(compaction, "compact_text", _compact)
    return seen


async def test_acall_awaits_compaction_natively(memory_tool, fake_compaction):
    """Главная проверка: инлайн-вход берёт корутинную компакцию, не портал."""
    out = json.loads(
        await memory_tool.acall(action="append", content="ещё один длинный кусок текста")
    )

    assert out["ok"] is True, out
    assert fake_compaction["called"] == "async", (
        "acall ушёл в синхронный compact_text — это портал, и из-под живого "
        "loop он поднимет SyncApiInsideEventLoopError"
    )


async def test_acall_compaction_does_not_raise_on_a_live_loop(memory_tool, monkeypatch):
    """То же самое, но с НАСТОЯЩИМ порталом на пути — без подмен.

    Именно так дефект выглядит у пользователя: не «выбран не тот двойник», а
    исключение из середины тула. Сетевой лист заглушаем на уровне
    ``acompact_text``, чтобы тест не ходил в модель.
    """
    async def _acompact(text, ctx, state=None):
        return compaction.CompactionResult(
            ok=True, text="ужато", error_type=None, error_message=None
        )

    monkeypatch.setattr(compaction, "acompact_text", _acompact)

    out = json.loads(
        await memory_tool.acall(action="append", content="ещё один длинный кусок текста")
    )
    assert out["ok"] is True, out


def test_sync_call_still_uses_the_portal_shim(memory_tool, fake_compaction):
    """Синхронный вход не тронут: вне loop портал — правильный путь."""
    out = json.loads(
        memory_tool(action="append", content="ещё один длинный кусок текста")
    )

    assert out["ok"] is True, out
    assert fake_compaction["called"] == "sync-portal"


async def test_acall_read_path_needs_no_compaction(memory_tool, fake_compaction):
    """Чтение компакцию не трогает — обычная файловая операция инлайн."""
    out = json.loads(await memory_tool.acall(action="read", content=""))

    assert out["ok"] is True, out
    assert "called" not in fake_compaction
