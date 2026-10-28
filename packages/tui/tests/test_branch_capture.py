"""При создании новой сессии в git-проекте ветка пишется в tui_meta.branch."""

from gc_testkit.develop_files import meta_of

import pytest
from gc_tui.services.stores.file_store import FileTaskStore


@pytest.mark.asyncio
async def test_branch_captured_on_new_session(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.screens import main as main_mod

    monkeypatch.setattr(
        main_mod, "current_branch", lambda cwd: "feat/captured"
    )
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
        meta = meta_of(
            FileTaskStore(threads_dir=str(tmp_path), system="cli").get_record(
                tid
            )
        )
        assert meta.get("tui", {}).get("branch") == "feat/captured"


class _NoopExecutor:
    async def arun(self, *a, **k):
        return None

    def stop_run(self):
        pass

    def add_message(self, m):
        pass
