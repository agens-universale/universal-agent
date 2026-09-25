"""CLI-обёртка `python -m gc_server.cleanup_threads`.

Что случилось. Пока чистка лежала в `scripts/`, дефолт `--threads-dir` считался
якорем от файла скрипта (`<script>/../threads`) и не зависел от того, откуда
команду запустили. Переезд внутрь пакета (`28fdc67c`) этот якорь обессмыслил, и
на его месте оказалось голое `"threads"` — путь относительно CWD. В cron с чужим
CWD это давало «threads_dir not found» и код возврата 0: тихий успех, при котором
треды копились месяцами.

Починка — НЕ восстановление прежнего якоря: каталога `<репо>/threads` не
существовало никогда, ни в дереве, ни в истории, это был рудимент. Дефолт берётся
оттуда же, откуда его берёт фоновый janitor того же сервера, — иначе две половины
одной чистки расходятся во мнении о том, что они чистят.

Зависимость дефолта от CWD при этом остаётся и дефектом не является: у каждого
рабочего каталога свой стор тредов (`project_slug()`), ровно как у CLI и TUI.
Поэтому здесь проверяется не «путь одинаков откуда угодно», а «путь тот же, что у
janitor'а».
"""

import os
import sys

from gc_core.thread_store import resolve_threads_dir
from gc_server import cleanup_threads as cleanup_mod
from gc_server.rest_api.server_config import (
    build_agent_config_for_server,
    load_server_config,
)


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["cleanup_threads", *argv])
    return cleanup_mod.main()


def test_default_is_the_same_dir_the_janitor_cleans():
    """Дефолт совпадает с тем, что резолвит фоновая чистка сервера.

    Подмена, которую ловит тест: вернуть `"threads"` (или любой путь от CWD) —
    дефолт разъедется с janitor'ом, и CLI начнёт чистить не тот каталог.
    """
    expected = resolve_threads_dir(build_agent_config_for_server(load_server_config()))

    assert cleanup_mod._default_threads_dir() == expected
    assert os.path.isabs(expected), "janitor обязан отдавать абсолютный путь"


def test_default_does_not_look_under_cwd(monkeypatch, tmp_path):
    """Каталог `./threads` рядом с CWD дефолтом больше не считается.

    Ровно тот случай из cron: рядом лежит похожий по имени каталог, и старый код
    выбирал бы его вместо настоящего стора.
    """
    decoy = tmp_path / "threads"
    decoy.mkdir()
    monkeypatch.chdir(tmp_path)

    resolved = cleanup_mod._default_threads_dir()

    # Проверять только `!= str(decoy)` мало: относительное "threads" не равно
    # абсолютному пути приманки, и подмена прошла бы мимо сетки.
    assert os.path.isabs(resolved), f"дефолт обязан быть абсолютным, получено {resolved!r}"
    assert not os.path.realpath(resolved).startswith(os.path.realpath(str(tmp_path)))


def test_explicit_relative_dir_becomes_absolute_in_stats(monkeypatch, tmp_path, capsys):
    """Явный относительный путь нормализуется — иначе по выводу не понять, что чистили."""
    (tmp_path / "threads").mkdir()
    monkeypatch.chdir(tmp_path)

    rc = _run(monkeypatch, "--threads-dir", "threads")

    assert rc == 0
    # Статистика печатается словарём, то есть repr: на Windows слеши в нём
    # удвоены, поэтому ищем repr пути, а не сам путь.
    assert repr(os.path.join(os.getcwd(), "threads")) in capsys.readouterr().out


def test_explicit_dir_does_not_touch_the_config(monkeypatch, tmp_path):
    """С явным каталогом конфиг не разбирается вовсе.

    Иначе команда падала бы на битом `config_server.yml` там, где он ей не нужен.
    """
    (tmp_path / "threads").mkdir()

    def _boom() -> str:
        raise AssertionError("конфиг не должен читаться при явном --threads-dir")

    monkeypatch.setattr(cleanup_mod, "_default_threads_dir", _boom)

    assert _run(monkeypatch, "--threads-dir", str(tmp_path / "threads")) == 0


def test_missing_dir_reports_the_resolved_path(monkeypatch, tmp_path, capsys):
    """Промах по каталогу печатает АБСОЛЮТНЫЙ путь и возвращает 0, как на develop.

    Ноль здесь — сознательно: делать его ненулевым значит менять поведение шире,
    чем требовал разбор ревью. Тишину убирает не код возврата, а то, что в выводе
    виден полный путь.
    """
    rc = _run(monkeypatch, "--threads-dir", str(tmp_path / "nope"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "not found" in out
    assert str(tmp_path / "nope") in out
