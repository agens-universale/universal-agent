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
