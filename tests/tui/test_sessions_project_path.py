"""SessionsScreen: отображение пути проекта сессии в конце метастроки.

Спека: docs_md/actual/2026-07-29-sessions-show-project-path.md
Кейсы: путь есть → показан; пути нет (старая сессия) → не показан, не падает;
длинный путь → обрезан слева; запись project_path при создании новой сессии.
"""
import json
from pathlib import Path

import pytest

from gc_core.thread_store import append_new_thread_messages
from gc_tui.services.stores.file_store import FileTaskStore


def _seed(threads_dir, tid, prompt, *, project_path=None,
          updated_at="2026-05-30T10:00:00+00:00", msgs=1):
    """Сессия = data-файл + meta. project_path пишется в meta.tui."""
    append_new_thread_messages(str(threads_dir), "cli", tid,
                               [{"role": "user", "content": prompt,
                                 "id": f"m_{tid}"}], set())
    meta = {"thread_id": tid, "system": "cli", "status": "stopped",
            "updated_at": updated_at, "messages_count": msgs}
    if project_path is not None:
        meta["tui"] = {"project_path": project_path}
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _rows_text(pilot):
    return "\n".join(
        str(s.render()) for s in pilot.app.screen.query("#sessions-list Static"))


@pytest.mark.asyncio
async def test_card_shows_project_path(tmp_path):
    """В meta.tui.project_path есть путь → он в конце метастроки карточки."""
    pytest.importorskip("textual")
    ppath = str(tmp_path)
    _seed(tmp_path, "pp000001", "вопрос", project_path=ppath)
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "pp000001" in text  # short id
        # Путь может быть обрезан слева (truncate_path_left при > _PATH_MAXLEN),
        # поэтому проверяем хвост, а не полный путь.
        from gc_tui.screens.sessions import _PATH_MAXLEN
        if len(ppath) > _PATH_MAXLEN:
            assert ppath[-(_PATH_MAXLEN - 1):] in text
            assert "…" in text
        else:
            assert ppath in text


@pytest.mark.asyncio
async def test_card_without_project_path_no_crash(tmp_path):
    """Старая сессия без project_path в meta → пути нет, карточка не падает."""
    pytest.importorskip("textual")
    _seed(tmp_path, "old00001", "старый вопрос", project_path=None)
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "old00001" in text
        # Карточка не упала, путь не вставлен (нет мусора)


@pytest.mark.asyncio
async def test_long_project_path_truncated_left(tmp_path):
    """Длинный путь (> _PATH_MAXLEN) обрезан слева через truncate_path_left."""
    pytest.importorskip("textual")
    from gc_tui.screens.sessions import _PATH_MAXLEN
    long_path = "/home/alex/sber_gigacode/mini-cli-pub/very/deep/nested/dir/structure"
    assert len(long_path) > _PATH_MAXLEN
    _seed(tmp_path, "long0001", "длинный путь", project_path=long_path)
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        # Хвост пути сохранён (truncate_path_left оставляет конец)
        assert long_path[-(_PATH_MAXLEN - 1):] in text
        # Полный длинный путь не должен поместиться целиком
        assert long_path not in text


@pytest.mark.asyncio
async def test_project_path_captured_on_new_session(tmp_path, monkeypatch):
    """При создании новой сессии project_path пишется в meta.tui (как branch)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.screens import main as main_mod
    monkeypatch.setattr(main_mod, "current_branch", lambda cwd: "")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    app._executor_factory = lambda: _NoopExecutor()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "привет"
        await pilot.press("enter")
        await pilot.pause()
        tid = pilot.app.tui_state.active_task_id
        assert tid is not None
        meta = FileTaskStore(
            threads_dir=str(tmp_path), system="cli").get_task_meta(tid)
        ppath = meta.get("tui", {}).get("project_path")
        assert ppath is not None
        assert str(tmp_path) in ppath or ppath.endswith(str(tmp_path).lstrip("/"))


class _NoopExecutor:
    def run(self, *a, **k):
        return None

    def stop_run(self):
        pass

    def add_message(self, m):
        pass
