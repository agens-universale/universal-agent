"""CLI-обёртка `python -m gc_server.cleanup_threads`.

Дефолт `--threads-dir` когда-то был якорем от файла скрипта (`<script>/../threads`);
после переезда чистки внутрь пакета на его месте оказалось голое `"threads"`
относительно CWD. В cron с чужим CWD команда печатала «threads_dir not found»
и возвращала 0 — тихий успех, при котором треды копились месяцами.
"""

import os
import sys

import pytest

from gc_server import cleanup_threads as cleanup_mod


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["cleanup_threads", *argv])
    return cleanup_mod.main()


def test_threads_dir_is_required(monkeypatch, tmp_path):
    """Дефолта нет вообще — значит, он не может зависеть от CWD.

    Подмена, которую ловит тест: вернуть `default=None` + `else "threads"` —
    команда молча отработает по каталогу относительно текущего CWD.
    """
    (tmp_path / "threads").mkdir()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch)

    assert exc.value.code == 2


def test_relative_threads_dir_becomes_absolute_in_stats(monkeypatch, tmp_path, capsys):
    """Ненормализованный путь не должен уезжать в статистику."""
    (tmp_path / "threads").mkdir()
    monkeypatch.chdir(tmp_path)

    rc = _run(monkeypatch, "--threads-dir", "threads")

    assert rc == 0
    out = capsys.readouterr().out
    assert os.path.realpath(str(tmp_path / "threads")) in out


def test_missing_threads_dir_is_a_failure_not_a_silent_success(monkeypatch, tmp_path, capsys):
    """Промах по каталогу — ненулевой код возврата, иначе cron считает успехом."""
    rc = _run(monkeypatch, "--threads-dir", str(tmp_path / "nope"))

    assert rc != 0
    assert "not found" in capsys.readouterr().out
