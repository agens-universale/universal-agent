"""Файловый порт: одна форма, корутинная, и без потока там, где он не нужен.

До этой ветки форм было две: синхронные методы порта и примесь `AsyncFileOps`
с двадцатью восемью двойниками поверх них. Двойников не звал НИКТО, кроме
этого файла, — тулы шли синхронным путём. Для `LocalFsBackend` это было
безобидно (у файла нет нативной асинхронной формы, работа идёт за
микросекунды), но «файл» у бэкенда не обязан быть файлом: у `HarborBackend`
`exists()` — это `test -e` по сети, а `read()` — скачивание, и синхронная
форма ждала их через `run_coroutine_threadsafe(...).result()`, блокируя
поток. На loop самого Harbor это дедлок: поток блокировал сам себя.

Здесь сторожим три вещи, ради которых пары и снимали:

* форма ОДНА — синхронных двойников у порта нет;
* локальное чтение по-прежнему инлайн: поток дороже самой работы;
* мостящий бэкенд ждёт НАТИВНО, не занимая ни поток, ни свой loop.
"""

import asyncio
import inspect
import threading

from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import FsBackend

IO_METHODS = (
    "exists",
    "lexists",
    "is_file",
    "is_dir",
    "is_symlink",
    "getsize",
    "stat_info",
    "readable",
    "writable",
    "realpath",
    "is_hidden",
    "read",
    "read_bytes",
    "read_head_bytes",
    "sha256_bytes",
    "write",
    "write_bytes",
    "append",
    "copy",
    "makedirs",
    "listdir",
    "walk",
    "walk_entries",
)
GENERATORS = ("iter_lines", "iter_walk", "iter_files", "iter_walk_entries")


def _spy_threads():
    """Считать СОЗДАНИЕ потоков: короткий поток умирает до замера активных."""
    spawned: list = []
    real_start = threading.Thread.start

    def _counting(self):
        spawned.append(self.name)
        return real_start(self)

    return spawned, real_start, _counting


def test_the_port_has_exactly_one_form():
    """Ни синхронных методов, ни `a`-двойников: и то и другое — вторая форма."""
    for name in IO_METHODS:
        assert inspect.iscoroutinefunction(getattr(FsBackend, name)), (
            f"{name} перестал быть корутиной — вернулась синхронная форма"
        )
    for name in IO_METHODS + GENERATORS:
        assert not hasattr(FsBackend, "a" + name), (
            f"вернулся двойник a{name}: у порта снова две формы"
        )
    assert not hasattr(FsBackend, "bridges_to_loop"), (
        "флаг моста жил при двойниках; развилку теперь делает сам бэкенд"
    )


async def test_local_backend_reads_inline_without_a_thread(tmp_path):
    """Локальные файлы — инлайн: поток дороже самой работы.

    Корутинная форма НЕ означает поток. У локального бэкенда тело осталось
    прежним, синхронным внутри; `await` тут стоит микросекунды и платится
    ради одной поверхности с сетевым бэкендом.
    """
    f = tmp_path / "a.txt"
    f.write_text("привет", encoding="utf-8")
    backend = LocalFsBackend()

    spawned, real_start, counting = _spy_threads()
    threading.Thread.start = counting
    try:
        text = await backend.read(str(f))
        assert await backend.exists(str(f)) is True
    finally:
        threading.Thread.start = real_start

    assert text == "привет"
    assert spawned == [], f"локальное чтение ушло в поток: {spawned}"


async def test_local_backend_keeps_the_lazy_generator(tmp_path):
    """`iter_files` остаётся ленивым: обход не материализуется целиком."""
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    backend = LocalFsBackend()

    seen = []
    async for rel in backend.iter_files(str(tmp_path)):
        seen.append(rel)
        if len(seen) == 2:
            break

    assert len(seen) == 2, "обход отдал больше, чем спросили"


async def test_bridging_backend_waits_natively_without_a_thread():
    """Бэкенд поверх ЧУЖОГО loop ждёт нативно — ни потока, ни блокировки.

    Так устроен `HarborBackend`: его `env` привязан к loop Harbor, а агент
    может работать на другом. Синхронная форма ждала через
    `run_coroutine_threadsafe(...).result()` — поток блокировался, а на самом
    loop Harbor блокировал сам себя. Корутинная ставит задачу на loop
    владельца и ждёт `wrap_future`: у вызывающего это обычный `await`.
    """
    owner_loop = asyncio.new_event_loop()
    started = threading.Event()

    def _run_owner():
        asyncio.set_event_loop(owner_loop)
        owner_loop.call_soon(started.set)
        owner_loop.run_forever()

    owner_thread = threading.Thread(target=_run_owner, daemon=True)
    owner_thread.start()
    started.wait(5)

    class _Bridged:
        """Тело файловой операции живёт на loop владельца."""

        def __init__(self, loop):
            self._loop = loop

        async def _aawait(self, coro):
            if asyncio.get_running_loop() is self._loop:
                return await coro
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, self._loop)
            )

        async def read(self, path):
            async def _remote():
                await asyncio.sleep(0.01)
                return f"содержимое {path}"

            return await self._aawait(_remote())

    backend = _Bridged(owner_loop)
    spawned, real_start, counting = _spy_threads()
    threading.Thread.start = counting
    try:
        # Таймер обязан сработать: если бы ожидание блокировало наш loop,
        # `wait_for` не получил бы управление и тест повис бы.
        text = await asyncio.wait_for(backend.read("/x"), timeout=5)
    finally:
        threading.Thread.start = real_start
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        owner_thread.join(5)

    assert text == "содержимое /x"
    assert spawned == [], f"нативное ожидание завело поток: {spawned}"
