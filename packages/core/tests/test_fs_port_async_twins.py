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
