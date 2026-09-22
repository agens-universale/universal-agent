"""Тесты для точки входа `python -m gc_server.swarm.cleanup` (main()).

Соседний test_swarm_cleanup.py покрывает только `cleanup_threads()` — саму
TTL-логику. У main() тестов не было вовсе, и именно поэтому в нём дожила
опечатка: печать `stats['orphan_sandboxes_removed']`, ключа, которого
`cleanup_threads()` не кладёт с тех пор, как чистка тредов и песочниц
расцепилась.

Почему её не ловили раньше: файл унаследован байт-в-байт из
scripts/cleanup_swarm_threads.py, а scripts/ не попадал ни в одно колесо — из
установки запускать было нечего. Переезд в пакет положил модуль в колесо, и
документированный в его же докстринге вызов стал падать KeyError'ом ПОСЛЕ
удаления каталогов: exit 1 при выполненной работе, cron ретраит.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from gc_server.swarm import cleanup as cleanup_mod


def _aged_dir(parent, name: str, age_hours: float):
    """Создать каталог и состарить его mtime на age_hours."""
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    path.mkdir()
    (path / "marker").write_text("x", encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (old, old))
    return path


@pytest.fixture
def aged_tree(tmp_path):
    """threads/<id> и sandboxes/<id> возрастом двое суток — оба под удаление."""
    threads = tmp_path / "threads"
    sandboxes = tmp_path / "sandboxes"
    _aged_dir(threads, "swarm_g_t1", age_hours=48)
    _aged_dir(sandboxes, "swarm_g_t1", age_hours=48)
    return threads, sandboxes


def _argv(threads, sandboxes, *extra):
    return [
        "cleanup",
        "--threads-dir", str(threads),
        "--sandbox-root-dir", str(sandboxes),
        "--ttl-hours", "24",
        *extra,
    ]


def test_main_returns_zero_and_reports_counts(aged_tree, monkeypatch, capsys):
    """Штатный прогон: работа сделана, отчёт напечатан, код возврата 0.

    До правки эта же строка обращалась к отсутствующему ключу и роняла main()
    KeyError'ом — уже ПОСЛЕ rmtree.
    """
    threads, sandboxes = aged_tree
    monkeypatch.setattr(sys, "argv", _argv(threads, sandboxes))

    assert cleanup_mod.main() == 0

    out = capsys.readouterr().out
    assert "removed 1 threads" in out
    assert "1 sandboxes" in out
    assert "kept 0 active" in out
    assert "errors 0" in out
    assert not (threads / "swarm_g_t1").exists()
    assert not (sandboxes / "swarm_g_t1").exists()


def test_main_prints_only_keys_cleanup_threads_returns(aged_tree, monkeypatch, capsys):
    """Отчёт не должен упоминать счётчиков, которых нет в stats.

    Сетка держит именно рассинхрон печати с контрактом `cleanup_threads()`:
    любой новый `stats['...']` в main() обязан быть заведён в самом
    `cleanup_threads()`, иначе KeyError.
    """
    threads, sandboxes = aged_tree
    monkeypatch.setattr(sys, "argv", _argv(threads, sandboxes))
    cleanup_mod.main()

    stats = cleanup_mod.cleanup_threads(str(threads), str(sandboxes), 24.0)
    assert set(stats) == {"threads_removed", "sandboxes_removed", "kept", "errors"}
    # «orphan» и «matching» — словарь той версии, где песочница удалялась парой
    # со своим тредом. Проходы давно расцеплены, слова обязаны были уйти вместе
    # с ключом.
    out = capsys.readouterr().out
    assert "orphan" not in out
    assert "matching" not in out


def test_dry_run_reports_without_deleting(aged_tree, monkeypatch, capsys):
    """--dry-run падал ровно так же: KeyError на печати, хотя удалять нечего."""
    threads, sandboxes = aged_tree
    monkeypatch.setattr(sys, "argv", _argv(threads, sandboxes, "--dry-run"))

    assert cleanup_mod.main() == 0

    out = capsys.readouterr().out
    assert out.startswith("[DRY RUN] ")
    assert "removed 1 threads" in out
    assert (threads / "swarm_g_t1").exists()
    assert (sandboxes / "swarm_g_t1").exists()


def test_documented_module_invocation_succeeds(aged_tree):
    """`python -m gc_server.swarm.cleanup` — то, что обещает докстринг модуля.

    Только этот вызов и был у cron'а; в отдельном процессе он падал с exit 1
    при выполненной работе, и cron ретраил уже удалённое.
    """
    threads, sandboxes = aged_tree
    proc = subprocess.run(
        [sys.executable, "-m", "gc_server.swarm.cleanup",
         "--threads-dir", str(threads),
         "--sandbox-root-dir", str(sandboxes),
         "--ttl-hours", "24"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "swarm cleanup:" in proc.stdout
    assert "Traceback" not in proc.stderr
