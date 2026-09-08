"""Длинный обход ФС не должен замораживать loop вызывающего.

Файловые тулы идут ИНЛАЙН, и docs/async.md обосновывает это микросекундами.
Для чтения и записи это правда, для рекурсивного обхода — нет: он ограничен
ЧИСЛОМ записей (``max_visited`` по умолчанию полмиллиона), а не временем.
Замерено в ревью на реальном дереве: 24 тысячи записей — около 150 мс без
единого тика, то есть весь бюджет приёмки одной операцией; на потолке это
секунды. На develop эти обходы уходили в ``to_thread`` целиком.

**Про форму зонда.** Дерево, на котором обход занял бы сотню миллисекунд, в
тесте не создать: здесь запись стоит доли микросекунды, и понадобилось бы
полмиллиона файлов — минуты только на подготовку. Поэтому цена одной записи
задаётся явно, крошечной блокирующей паузой в итераторе бэкенда: она стоит
ровно за тот масштаб, который мы не можем себе позволить создать. Проверяемое
свойство от этого не меняется — уступает ли цикл обхода управление loop.

Существующий ``test_file_tools_run_inline`` проверяет ровно противоположное
(что тул исполняется на том же потоке) и этой цены не видит: оба свойства
нужны и друг другу не противоречат.

Сторожем накрыт ``fs_list_files``: он идёт питоновским обходом всегда, и на
нём свойство проверяемо. У ``fs_find_files_by_name`` и ``fs_find_in_files``
на машине с нативными ``find``/``grep`` основной обход уходит утилите, и в
питоновский цикл тул почти не заходит — воспроизводимого зонда на них здесь
нет, хотя точка уступки добавлена во все три цикла. Замерзают они там, где
фоллбек и работает: в песочнице без утилит и на Windows.
"""
import asyncio
import time

import pytest

from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_core.tools.fs.list_files import ListFiles
from gc_testkit.helpers.tool_ctx import local_ctx

_DIRS = 40
_FILES_PER_DIR = 40
#: Цена «записи большого дерева». Сотни записей × 0.25 мс — тот же порядок
#: работы, что замерен в ревью на 24 тысячах настоящих файлов.
_COST_PER_ENTRY_SEC = 0.00025
_PULSE_SEC = 0.01
#: Приёмка ветки требует меньше 150 мс; берём вдвое строже, с запасом на шум.
_MAX_GAP_SEC = 0.075


@pytest.fixture(scope="module")
def big_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("bigtree")
    for d in range(_DIRS):
        sub = root / f"dir_{d:03d}"
        sub.mkdir()
        for f in range(_FILES_PER_DIR):
            (sub / f"file_{f:03d}.txt").write_text("x", encoding="utf-8")
    return root


@pytest.fixture
def python_walk(monkeypatch):
    """Заставить тулы идти ПИТОНОВСКИМ обходом, а не нативным `find`.

    Где нативный `find` есть, тул отдаёт ему весь обход и в питоновский цикл
    почти не заходит — тогда и уступать негде. Замерзает ровно фоллбек: там,
    где `find` недоступен (песочница без утилит, Windows) или где обход идёт
    с проверками пути. Ревью и мерило на дереве, которое шло фоллбеком.
    """
    from gc_core.backends.local import LocalFsBackend

    monkeypatch.setattr(LocalFsBackend, "has_find", property(lambda self: False))
    monkeypatch.setattr(LocalFsBackend, "has_grep", property(lambda self: False))


@pytest.fixture
def costly_walk(monkeypatch):
    """Сделать каждую запись обхода дорогой — как на дереве в сотни тысяч.

    Пауза БЛОКИРУЮЩАЯ (``time.sleep``), а не ``asyncio.sleep``: иначе она сама
    уступала бы loop, и тест доказывал бы собственное поведение вместо тулова.
    """
    from gc_core.backends.local import LocalFsBackend

    def _slow(name):
        real = getattr(LocalFsBackend, name)

        def wrapper(self, *a, **kw):
            for item in real(self, *a, **kw):
                time.sleep(_COST_PER_ENTRY_SEC)
                yield item

        return wrapper

    for method in ("iter_walk", "iter_walk_entries"):
        monkeypatch.setattr(LocalFsBackend, method, _slow(method))


async def _worst_gap_while(coro):
    """Худший разрыв пульса, пока идёт *coro*.

    Максимум, а не сумма: сумма скрыла бы одну длинную заморозку среди
    множества нормальных тиков — а заморозка и есть то, что мы ищем.
    """
    gaps = []
    stop = asyncio.Event()

    async def _pulse():
        prev = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(_PULSE_SEC)
            now = time.perf_counter()
            gaps.append(now - prev)
            prev = now

    beat = asyncio.create_task(_pulse())
    try:
        result = await coro
    finally:
        stop.set()
        await beat
    return result, (max(gaps) if gaps else 0.0), len(gaps)


async def test_list_files_lets_the_loop_breathe(big_tree, python_walk, costly_walk):
    tool = ListFiles(ctx=local_ctx())

    _out, worst, ticks = await _worst_gap_while(
        tool.acall(path=str(big_tree), max_entries=100_000, max_depth=10)
    )

    assert ticks > 3, f"обход отработал быстрее пульса: {ticks}"
    assert worst < _MAX_GAP_SEC, f"loop залипал на {worst * 1000:.0f} мс"


async def test_the_yielder_does_not_yield_on_every_entry():
    """Уступка по ВРЕМЕНИ, а не на каждой записи: иначе дёргаем планировщик зря."""
    import gc_core.tools.fs.cooperative as coop

    yields = 0
    real_sleep = asyncio.sleep

    async def _counting(delay, *a, **kw):
        nonlocal yields
        if delay == 0:
            yields += 1
        return await real_sleep(delay, *a, **kw)

    coop.asyncio.sleep = _counting
    try:
        yielder = coop.LoopYielder()
        for _ in range(10_000):
            await yielder.tick()
    finally:
        coop.asyncio.sleep = real_sleep

    assert yields < 500, f"уступали {yields} раз на 10 000 записей — слишком часто"
