"""Асинхронные двойники файлового порта: инлайн для локального, поток для моста.

Зачем они вообще. Тулы зовут файловые методы ИНЛАЙН внутри `acall` — у файлов
нет нативной асинхронной формы, и поход в пул стоит дороже самой работы
(40 мкс против микросекунд). Для `LocalFsBackend` это верно и остаётся.

Но «файл» у бэкенда не обязан быть файлом. У `HarborBackend` `exists()` — это
`test -e` по сети, `read()` — `env.download_file`, и оба ЖДУТ корутину чужого
loop через `run_coroutine_threadsafe(...).result()`. Позови такой метод инлайн
с того же loop — поток заблокирует сам себя навсегда. Проверено зависанием,
когда агента попробовали вести на loop Harbor.
"""
import asyncio
import threading

import pytest

from gc_core.backends.local import LocalFsBackend


def _spy_threads():
    """Считать СОЗДАНИЕ потоков: короткий поток умирает до замера активных."""
    spawned: list = []
    real_start = threading.Thread.start

    def _counting(self):
        spawned.append(self.name)
        return real_start(self)

    return spawned, real_start, _counting


async def test_local_backend_reads_inline_without_a_thread(tmp_path):
    """Локальные файлы — инлайн: поток дороже самой работы."""
    f = tmp_path / "a.txt"
    f.write_text("привет", encoding="utf-8")
    backend = LocalFsBackend()

    spawned, real_start, counting = _spy_threads()
    threading.Thread.start = counting
    try:
        text = await backend.aread(str(f))
        assert await backend.aexists(str(f)) is True
    finally:
        threading.Thread.start = real_start

    assert text == "привет"
    assert spawned == [], f"локальное чтение ушло в поток: {spawned}"


def test_bridging_backend_never_blocks_its_own_loop():
    """Мостящий бэкенд уводит операцию с текущего потока — иначе дедлок.

    Мост воспроизводится честно: синхронный метод ждёт корутину на ТОМ ЖЕ
    loop, с которого его позвали. Именно так устроен `HarborBackend` —
    `run_coroutine_threadsafe(env.download_file(...), harbor_loop).result()`,
    а агент работает на `harbor_loop`.

    Сценарий вынесен в ОТДЕЛЬНЫЙ поток, и это не стиль. Инлайн здесь блокирует
    сам loop, поэтому `asyncio.wait_for` внутри него не сработает: таймеру
    некому дать ход. Без внешнего сторожевого потока регрессия не роняла бы
    тест, а вешала весь прогон — проверено подменой.
    """
    result: dict = {}

    def worker():
        async def body():
            class _Bridged(LocalFsBackend):
                bridges_to_loop = True

                def read(self, path):     # noqa: D401 — мост, не чтение
                    async def _remote():
                        await asyncio.sleep(0.05)
                        return "из контейнера"

                    # Мост НА СВОЙ loop: единственная форма, дающая дедлок.
                    # Ссылку держим явно — из потока-исполнителя (куда уводит
                    # `_afile`) текущего loop нет вовсе.
                    return asyncio.run_coroutine_threadsafe(
                        _remote(), self._bridged_loop
                    ).result()

            backend = _Bridged()
            backend._bridged_loop = asyncio.get_running_loop()
            result["text"] = await backend.aread("/whatever")

        asyncio.run(body())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=10)

    assert not t.is_alive(), (
        "мостящий бэкенд позвал синхронный метод инлайн на своём loop — "
        "поток заблокировал сам себя, и это вечный дедлок"
    )
    assert result.get("text") == "из контейнера"


def test_harbor_declares_itself_bridging():
    """Флаг стоит там, где мост действительно есть."""
    from gc_core.backends.harbor import HarborBackend

    assert HarborBackend.bridges_to_loop is True
    assert LocalFsBackend.bridges_to_loop is False, (
        "локальный бэкенд объявил себя мостящим — все файловые операции уедут "
        "в пул, хотя стоят микросекунды"
    )


# --- генераторные двойники у мостящего бэкенда ----------------------------


class _BridgingBackend:
    """Мостящий бэкенд-двойник: синхронный обход блокирует поток.

    Так ведёт себя ``HarborBackend``: его ``walk`` внутри делает
    ``run_coroutine_threadsafe(...).result()`` на чужом loop, то есть держит
    поток, пока идёт сетевой обход.
    """

    bridges_to_loop = True

    def __init__(self):
        self.thread_of_work = None

    def iter_walk(self, path, *, should_descend=None):
        import threading

        for i in range(3):
            # Работа происходит ЗДЕСЬ, при итерации, а не при вызове функции.
            self.thread_of_work = threading.current_thread().name
            yield (f"f{i}", False)



async def test_generator_twin_does_the_work_off_the_calling_thread():
    """Работа генератора обязана уйти с потока вызывающего.

    Регрессия, которую нашло ревью: двойник уводил в поток ВЫЗОВ генераторной
    функции, а тот возвращается мгновенно, отдав генератор. Настоящая работа
    шла потом, при итерации, на loop вызывающего — то есть ровно та блокировка,
    которую примесь обещает снять. Снаружи выглядело как рабочий async-двойник.
    """
    import threading

    from gc_core.backends.protocol import AsyncFileOps

    class _Backend(AsyncFileOps, _BridgingBackend):
        # Флаг ставим ЗДЕСЬ: у `AsyncFileOps` он объявлен False, и в MRO
        # примесь идёт раньше — значение базы иначе бы победило.
        bridges_to_loop = True

    backend = _Backend()
    _BridgingBackend.__init__(backend)
    here = threading.current_thread().name

    result = await backend.aiter_walk("/x")
    items = list(result)

    assert items == [("f0", False), ("f1", False), ("f2", False)], items
    assert backend.thread_of_work is not None, "генератор так и не проитерировался"
    assert backend.thread_of_work != here, (
        f"обход исполнился на потоке вызывающего ({here}) — двойник уводит в "
        f"поток только вызов функции, а не саму работу"
    )


async def test_local_backend_keeps_the_lazy_generator():
    """Локальному бэкенду материализация не нужна — он остаётся ленивым."""
    import inspect

    from gc_core.backends.local import LocalFsBackend

    result = await LocalFsBackend().aiter_walk(".")

    assert inspect.isgenerator(result), (
        "локальный бэкенд начал материализовать обход — это лишняя память "
        "там, где блокировки нет"
    )
