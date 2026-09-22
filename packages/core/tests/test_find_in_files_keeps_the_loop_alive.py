"""Python-путь `fs_find_in_files` не морозит loop.

Путь включается, когда нативный `rg`/`grep` не отработал: тогда каждый файл
кандидата читает и разбирает сам агент, инлайн, на loop вызывающего. Точек
уступки в этом цикле не было — loop стоял весь разбор (замерено 79 мс на 260
файлах, и это линейно растёт с деревом). Редкая параллельная ветка (от 20 000
кандидатов) была не лучше: свой `ThreadPoolExecutor` внутри корутины ждали
синхронным `as_completed`, то есть 114 мс на той же выборке.

Почему сторож не таймингом. Обход каталогов ВЫШЕ по коду уступает сам
(`LoopYielder`), и на дереве из мелких файлов он занимает столько же, сколько
разбор. Тайминговая проверка это глотала: код без единой уступки в разборе
укладывался в тот же порог — проверено подстановкой, тест зеленел на дефекте.
Поэтому здесь считаем уступки ИМЕННО во время чтения файлов.
"""

import ast
import pathlib
import threading

import pytest
from gc_core.tools.fs import find_in_files as fif
from gc_core.tools.fs.cooperative import LoopYielder
from gc_testkit.helpers.tool_ctx import local_ctx


async def _no_native(self, **_kw):
    return None, "тест: нативный путь отключён"


def _tree(root: pathlib.Path, files: int = 120) -> pathlib.Path:
    body = ("значение = 1\n" * 400).encode("utf-8")
    for i in range(files):
        (root / f"file_{i:04d}.py").write_bytes(body)
    return root


@pytest.fixture
def _probe(monkeypatch):
    """Считает уступки loop и открытия файлов, храня их порядок."""
    opened: list[str] = []
    yields_during_content: list[int] = []
    threads: set[str] = set()

    orig_tick = LoopYielder.tick

    async def _tick(self):
        if opened:
            yields_during_content.append(len(opened))
        await orig_tick(self)

    orig_open = fif.FindInFiles._open_decode_text

    def _open(self, abs_path, *, file_size):
        opened.append(abs_path)
        threads.add(threading.current_thread().name)
        return orig_open(self, abs_path, file_size=file_size)

    monkeypatch.setattr(LoopYielder, "tick", _tick)
    monkeypatch.setattr(fif.FindInFiles, "_open_decode_text", _open)
    monkeypatch.setattr(fif.FindInFiles, "_try_native_grep_directory", _no_native)
    return opened, yields_during_content, threads


async def test_inline_scan_yields_the_loop_between_files(tmp_path, _probe):
    """Основная ветка: разбор инлайн, но с уступками по ходу.

    Уступка ПОСЛЕ первого открытого файла — именно то, чего не было: раньше
    все они приходились на обход каталогов, а сам разбор шёл одним куском.
    """
    opened, yields, threads = _probe
    root = _tree(tmp_path)

    raw = await fif.FindInFiles(ctx=local_ctx()).acall(
        path=str(root), pattern="такого-текста-нет", limit=300
    )

    assert '"search_backend": "python"' in raw, "проверяли не тот путь"
    assert len(opened) >= 100, f"разбор не состоялся: открыто {len(opened)}"
    assert yields, "за весь разбор loop не получил управление ни разу"


async def test_parallel_branch_also_yields_the_loop(tmp_path, monkeypatch, _probe):
    """Редкая ветка (от 20 000 кандидатов): несколько разборщиков, но не потоки.

    Потоки отсюда ушли вместе с синхронной формой файлового порта: чтение
    файла у бэкенда теперь корутина, а поток её ждать не умеет. Для
    ЛОКАЛЬНОГО бэкенда потери нет — там чтение инлайн и GIL никто не
    отпускает, два потока разбирали ровно столько же (замерено 124 мс против
    129 мс). Для бэкенда по сети выигрыш наоборот появился: ожидания
    накладываются, чего поток на ``run_coroutine_threadsafe`` не умел.

    Сторожим то, что осталось важным: разбор идёт на loop-потоке и при этом
    уступает управление.
    """
    opened, yields, threads = _probe
    root = _tree(tmp_path)
    orig = fif.parse_find_in_files_config
    monkeypatch.setattr(
        fif,
        "parse_find_in_files_config",
        lambda *a, **kw: orig(*a, **kw).model_copy(
            update={"content_workers_threshold": 1}
        ),
    )

    raw = await fif.FindInFiles(ctx=local_ctx()).acall(
        path=str(root), pattern="такого-текста-нет", limit=300
    )

    assert '"search_backend": "python"' in raw
    assert len(opened) >= 100
    assert threads == {"MainThread"}, f"разбор ушёл в поток: {threads}"
    assert yields, "за весь разбор loop не получил управление ни разу"


def test_the_python_path_opens_no_thread_pool_of_its_own():
    """Свой ``ThreadPoolExecutor`` внутри корутины — это и есть заморозка.

    Потоки тут законны (чтение файла и regexp по нему), но ждать их обязан
    ``await``, а не ``as_completed`` на loop'е. Один этот сторож заменяет
    тайминговый: он ловит именно ту форму ожидания, которая морозила loop.
    """
    src = pathlib.Path(fif.__file__).read_text(encoding="utf-8")
    used = {
        node.func.attr if isinstance(node.func, ast.Attribute)
        else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
    }
    assert "ThreadPoolExecutor" not in used
    assert "as_completed" not in used
