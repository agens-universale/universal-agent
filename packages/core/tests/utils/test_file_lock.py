import subprocess
import sys
import textwrap
import time
from pathlib import Path

from gc_testkit.paths import package_dir

from gc_core.utils.file_lock import file_lock

# Корень репозитория в тестах берётся ТОЛЬКО у тест-кита: `parents[N]` молча
# ломается при переносе файла, а тут он попадает в скрипт дочернего процесса —
# тот не упал бы, а повис на чтении из пустого пайпа.
SRC = package_dir("gc_core").parent


def test_lock_creates_and_releases(tmp_path):
    target = tmp_path / "data.jsonl"
    with file_lock(target):
        assert (tmp_path / "data.jsonl.lock").exists()
    with file_lock(target):  # повторный захват после выхода работает
        pass


def test_lock_blocks_second_process(tmp_path):
    target = tmp_path / "data.jsonl"
    code = textwrap.dedent("""
        import sys
        sys.path.insert(0, {src!r})
        from gc_core.utils.file_lock import file_lock
        import time
        with file_lock({target!r}):
            print("locked", flush=True)
            time.sleep(3)
    """).format(src=str(SRC), target=str(target))

    holder = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "locked"

        t0 = time.monotonic()
        raised = False
        try:
            with file_lock(target, timeout=0.5):
                pass
        except TimeoutError:
            raised = True
        assert raised and time.monotonic() - t0 < 3
    except Exception:
        holder.kill()
        raise
    holder.wait(timeout=10)
