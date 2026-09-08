"""Пути принадлежат бэкенду, а не машине, где запущен агент.

Предусловие «путь абсолютный» и нормализация делались хостовыми
``os.path.isabs`` / ``os.path.normpath``. Для песочницы с POSIX-путями это
неверно: на Windows-хосте ``/work/skip`` превращается в ``\\work\\skip``, и
``exclude_dirs`` молча перестаёт работать; а на CPython 3.13 хостовый
``isabs`` отвергает любой контейнерный путь.

Тесты проверяют не версию интерпретатора, а маршрутизацию: тул обязан
слушаться бэкенда.
"""

import json

from gc_testkit.harbor_fakes import FakeBackend, bind_os_backend
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_core.tools.fs.list_files import ListFiles

_FILES = {
    "/work/app.py": "needle here\n",
    "/work/skip/vendor.py": "needle there\n",
}


def _tool(cls, files=None):
    tool = cls.__new__(cls)
    return bind_os_backend(tool, FakeBackend(files if files is not None else _FILES))


def _error_type(raw):
    data = json.loads(raw)
    err = data.get("error")
    return err.get("type") if isinstance(err, dict) else err


def test_list_files_rejects_a_host_absolute_path_the_backend_calls_relative():
    """``C:/work`` абсолютен для Windows и не абсолютен для контейнера."""
    raw = _tool(ListFiles)(path="C:/work")

    assert _error_type(raw) == "invalid_argument"


def test_find_in_files_rejects_a_host_absolute_path_the_backend_calls_relative():
    raw = _tool(FindInFiles)(path="C:/work", pattern="needle", query_mode="text")

    assert _error_type(raw) == "invalid_argument"


def test_find_files_by_name_rejects_a_host_absolute_path_the_backend_calls_relative():
    raw = _tool(FindFilesByName)(path="C:/work", query="app")

    assert _error_type(raw) == "invalid_argument"


def test_backend_absolute_path_is_accepted():
    """Регрессия: контейнерный путь принимается на любом хосте."""
    data = json.loads(_tool(ListFiles)(path="/work"))

    assert data["ok"] is True


def test_exclude_dirs_is_normalized_by_the_backend():
    """Хостовый normpath ломал контейнерный путь, и исключение молча терялось."""
    raw = _tool(FindInFiles)(
        path="/work",
        pattern="needle",
        query_mode="text",
        exclude_dirs=["/work/skip"],
    )
    data = json.loads(raw)

    assert data["ok"] is True
    assert "exclude_dir_errors" not in data
    assert [m["file"] for m in data["matches"]] == ["/work/app.py"]


def test_refuse_breakdown_reports_backend_paths():
    """Разбивка при отказе предлагает пути для повторного вызова — они контейнерные."""
    files = {f"/work/pkg{i}/f.py": "needle\n" for i in range(4)}
    tool = FindInFiles.__new__(FindInFiles)
    bind_os_backend(
        tool,
        FakeBackend(files),
        config_dict={"fs_tools": {"find_in_files": {"max_candidates_like": 1}}},
    )
    data = json.loads(tool(path="/work", pattern="needle", query_mode="text"))

    assert data["stop_reason"] == "too_many_candidate_files"
    for entry in data["top_level_dirs"]:
        assert entry["path"].startswith("/work/"), entry["path"]
