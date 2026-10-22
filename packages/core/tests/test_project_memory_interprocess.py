"""Память проекта на файлах: запись со сверкой версии держит и между
процессами.

Сверка версии и запись шли под ``threading.Lock`` — замок одного процесса.
Два процесса (TUI и gc-cli в одном репозитории) могли оба увидеть одну
версию и оба записать: второй молча затирал первого.
"""

import subprocess
import sys
import textwrap

_WORKERS = 4
_STEPS = 40

_WORKER = textwrap.dedent(
    """
    import asyncio, sys
    from gc_core.agent_store import FileAgentStore, MemoryConflict, ProjectKey

    async def main(root):
        store = FileAgentStore()
        project = ProjectKey(id="race", root=root)
        for _ in range({steps}):
            while True:
                memory = await store.read_memory(project)
                count = int(memory.text) if memory else 0
                try:
                    await store.write_memory(
                        project,
                        str(count + 1),
                        expected_version=memory.version if memory else None,
                    )
                    break
                except MemoryConflict:
                    continue

    asyncio.run(main(sys.argv[1]))
    """
).format(steps=_STEPS)


def test_concurrent_processes_lose_no_update(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    workers = [
        subprocess.Popen([sys.executable, "-c", _WORKER, str(root)])
        for _ in range(_WORKERS)
    ]
    for worker in workers:
        assert worker.wait(timeout=120) == 0
    text = (root / ".gc" / "project_memory.md").read_text(encoding="utf-8")
    assert int(text) == _WORKERS * _STEPS


async def test_memory_lock_is_the_file_and_does_not_block_the_loop(tmp_path):
    """Ревью, раунд 2 (R18): замок — у файла памяти, а не у ``project_id``
    (два процесса с разным id на одном репозитории друг друга исключают), и
    ожидание замка не останавливает event loop (сервер держит все
    поверхности на одном loop)."""
    import asyncio
    import threading
    import time

    from gc_core.agent_store import FileAgentStore, ProjectKey
    from gc_core.agent_store.file_store import _memory_lock

    root = tmp_path / "repo"
    root.mkdir()
    holder = ProjectKey(id="one", root=str(root))
    writer = ProjectKey(id="two", root=str(root))
    held = threading.Event()

    def hold() -> None:
        with _memory_lock(holder):
            held.set()
            time.sleep(0.5)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker = asyncio.create_task(tick())
    started = time.monotonic()
    await FileAgentStore().write_memory(writer, "x", expected_version=None)
    waited = time.monotonic() - started
    ticker.cancel()
    thread.join()
    assert waited >= 0.3, "запись не дождалась замка того же файла"
    assert ticks >= 10, "ожидание замка остановило event loop"
