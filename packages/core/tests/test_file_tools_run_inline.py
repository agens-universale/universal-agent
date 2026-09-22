"""Файловые тулы исполняются на loop, а не в потоке из пула.

У работы с файлами нет нативной асинхронной формы. Всё, что выглядит как
«async file», — обёртка над тем же пулом: у ``anyio`` каждый метод
``AsyncFile`` это буквально ``await to_thread.run_sync(self._fp.read, …)``
(``anyio/_core/_fileio.py``), у ``aiofiles`` — ``run_in_executor``.
Единственный настоящий нативный путь, ``io_uring``, существует только на Linux
и требует C-расширения.

Значит выбор для файлового тула не «поток или корутина», а «поток или инлайн».
Инлайн: обычные чтение и запись отвечают за микросекунды, и передача в пул и
обратно стоит дороже самой операции.

Поэтому файловые тулы объявляют ``acall``. Диспетчер предпочитает его потоку
(``tools/__init__.py``, ветка ``await acall``), и файловая операция идёт прямо
на loop.

Список закрытый НАМЕРЕННО. Первая правка перечисляла тулы по каталогу
``tools/fs/`` и пропустила ``project_memory`` и четыре скилловых — они лежат в
других каталогах, но в файлы ходят точно так же. Новый файловый тул без
``acall`` молча уедет в пул, и заметить это по поведению будет нечем.

Чужих синхронных тулов это НЕ касается: у них ``acall`` нет, диспетчер
по-прежнему уводит их в поток, и обещание docs/async.md остаётся в силе —
сторожит ``test_apply_tool_async_dispatch.py::
test_aapply_tool_offloads_sync_tool_to_thread``.

``skills_run_script`` в этот набор не входит: он читает файл скрипта, но потом
запускает subprocess на весь таймаут. У запуска процесса нативная асинхронная
форма ЕСТЬ, и он идёт через неё — сторожит ``test_skills_run_script_native.py``.
"""

import json
import threading

import pytest
from gc_core.tools import aapply_tool

from gc_core.tools.exec.calculator import Calculator
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.file_info import FileInfo
from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_core.tools.fs.list_files import ListFiles
from gc_core.tools.fs.read_file import ReadFile
from gc_core.tools.fs.save_base64_file import SaveBase64File
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_core.tools.project.project_memory import ProjectMemory
from gc_core.tools.session.read_user_intent_log import ReadUserIntentLog
from gc_core.tools.session.stored_outputs.read_stored_tool_output import (
    ReadStoredToolOutput,
)
from gc_core.tools.session.think import Think
from gc_core.tools.session.todo import SessionTodo
from gc_core.tools.session.working_memory import WorkingMemory
from gc_core.tools.skills.skills_tools import (
    LoadAssetTool,
    LoadReferenceTool,
    LoadSkillTool,
    SkillsListTool,
)
from gc_core.tools.turn.intermediate_reply import IntermediateReply
from gc_core.tools.turn.submit_solution import SubmitSolution
from gc_testkit.helpers.tool_ctx import local_ctx

#: Тул → как его собрать без реального окружения. Тело всё равно подменяется
#: зондом, конструктору важно лишь не упасть.
FILE_TOOLS = [
    # tools/fs/ — девять штук, ходят в ФС через self._backend.
    (ReadFile, lambda cls: cls(ctx=None)),
    (WriteToFile, lambda cls: cls(ctx=None)),
    (AppendToFile, lambda cls: cls(ctx=None)),
    (ApplyFileEdits, lambda cls: cls(ctx=None)),
    (SearchAndReplace, lambda cls: cls(ctx=None)),
    (ListFiles, lambda cls: cls(ctx=None)),
    (FileInfo, lambda cls: cls(ctx=None)),
    # Тоже только ФС: обход дерева, процессов нет. Соседний find_in_files в
    # этот набор НЕ входит — он зовёт grep и идёт через aexec.
    (FindFilesByName, lambda cls: cls(ctx=None)),
    (SaveBase64File, lambda cls: cls(ctx=None)),
    # project_memory в этом наборе НЕТ: его acall — не сквозной проброс, а
    # разбор действия (у ветки append есть LLM-компакция, её надо ждать
    # нативно). Он проверяется отдельно, ниже и в
    # test_project_memory_acall_compaction.py.
    # Скилловые читатели — self._backend.read и пересканы каталога в registry.py.
    # Первый позиционный аргумент конструктора — SkillsRegistry.
    (SkillsListTool, lambda cls: cls(None)),
    (LoadSkillTool, lambda cls: cls(None)),
    (LoadReferenceTool, lambda cls: cls(None)),
    (LoadAssetTool, lambda cls: cls(None)),
    # Не файловые, но той же категории: работы на 0.001-0.014 мс, нативной
    # асинхронной формы нет. Переход в поток стоит 27.6 мкс — дороже самой
    # работы в 2-14 раз, поэтому они тоже идут инлайн.
    (Calculator, lambda cls: cls(ctx=None)),
    (Think, lambda cls: cls(ctx=None)),
    (SessionTodo, lambda cls: cls(ctx=None)),
    (WorkingMemory, lambda cls: cls(ctx=None)),
    (SubmitSolution, lambda cls: cls(ctx=None)),
    (IntermediateReply, lambda cls: cls(ctx=None)),
    (ReadStoredToolOutput, lambda cls: cls(ctx=None)),
    (ReadUserIntentLog, lambda cls: cls(ctx=None)),
]


@pytest.mark.parametrize("tool_cls,build", FILE_TOOLS, ids=lambda v: v.__name__ if isinstance(v, type) else "")
async def test_file_tool_acall_runs_body_on_the_calling_thread(tool_cls, build, tmp_path):
    """``acall`` исполняет тело инлайн, не отдавая его в пул.

    Зонд следит за ПУЛОМ, а не за телом. Прежний наследовал тул и подменял
    ``__call__``, но формы ``__call__`` у тулов больше нет — она была второй,
    синхронной, и её убрали. Слежка за пулом вдобавок проверяет свойство
    НАПРЯМУЮ («в пул не ходили»), а не через посредника («тело исполнилось на
    том же потоке»), и переживёт следующую перестройку так же, как эту.

    Аргументы подставляются по сигнатуре, и это несущая деталь. Первая
    редакция звала ``acall()`` без них — и падала на сигнатуре, НЕ ДОЙДЯ до
    тела: тест был пустым для каждого тула с обязательными параметрами и
    зеленел бы при любом уходе в пул (проверено подменой).

    Исход тела по-прежнему не важен — важен маршрут: ушла ли работа в пул.
    """
    import asyncio
    import inspect

    tool = build(tool_cls)
    args = {}
    for name, param in inspect.signature(tool.acall).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        low = name.lower()
        if "path" in low or "dir" in low or "file" in low:
            args[name] = str(tmp_path / "probe.txt")
        elif "content" in low or "text" in low or "data" in low:
            args[name] = "x"
        else:
            args[name] = "x"
    assert args or not [
        p
        for p in inspect.signature(tool.acall).parameters.values()
        if p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ], f"{tool_cls.__name__}: не удалось собрать аргументы — тест стал бы пустым"

    went_to_pool = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn, /, *a, **kw):
        went_to_pool.append(getattr(fn, "__name__", repr(fn)))
        return await real_to_thread(fn, *a, **kw)

    asyncio.to_thread = _spy
    try:
        try:
            await tool.acall(**args)
        except Exception:
            pass  # исход тела не важен — важен маршрут
    finally:
        asyncio.to_thread = real_to_thread

    assert went_to_pool == [], (
        f"{tool_cls.__name__}.acall увёл работу в пул ({went_to_pool}) — "
        "у файловой операции нет нативной асинхронной формы, и поход в пул "
        "стоит дороже самой работы"
    )


async def test_project_memory_acall_runs_file_work_on_the_calling_thread():
    """``project_memory`` тоже инлайн — просто вход у него не сквозной.

    Он лежит в ``tools/project/``, а не в ``tools/fs/``, и первая правка его
    поэтому пропустила. В файлы он ходит наравне с остальными:
    ``storage.read`` / ``_atomic_write`` / ``shutil.copy2``.
    """
    seen = {}

    class _Probe(ProjectMemory):
        def _dispatch_sync(self, action, content):
            seen["thread"] = threading.get_ident()
            return "ok"

    probe = _Probe(ctx=None)
    probe._path = object()  # не None — иначе тул откажет до диспетчеризации

    assert await probe.acall(action="read") == "ok"
    assert seen["thread"] == threading.get_ident(), "project_memory.acall увёл файловую работу в другой поток"


def _tools_data(tool_cls):
    spec = tool_cls.tool_description()
    return {spec["name"]: {"tool_fabric": tool_cls, "spec": spec, "description": ""}}


async def test_dispatcher_reads_file_on_the_loop_thread(tmp_path, monkeypatch):
    """Сквозная проверка: диспетчер выбирает ``acall`` у настоящего тула.

    Параметризованный тест выше проверяет сам ``acall``; этот — что до него
    доходит очередь. Без него правка, вернувшая ``to_thread`` в диспетчер,
    осталась бы зелёной.
    """
    target = tmp_path / "a.txt"
    target.write_text("привет\n", encoding="utf-8")

    import asyncio

    went_to_pool = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn, /, *a, **kw):
        went_to_pool.append(getattr(fn, "__name__", repr(fn)))
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _spy)

    out = await aapply_tool(
        ReadFile.registered_tool_name(),
        {"path": str(target)},
        _tools_data(ReadFile),
        ctx=local_ctx(),
        config_dict={},
    )

    assert json.loads(out)["ok"] is True
    assert went_to_pool == [], (
        f"fs_read_file уехал в пул ({went_to_pool}) — диспетчер снова уводит тул в поток вместо того, чтобы звать acall"
    )
