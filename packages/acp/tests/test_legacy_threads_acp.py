"""ACP видит сессии формата develop (спека хранилищ v2, 9.4).

Состояние сессии v2 — ``frontend["acp"]``; у сессии develop его нет, и cwd
читается из ``acp-sessions/<id>.json``. Ожидания — из manifest.json фикстур.
"""

from gc_acp.session_index import SessionIndex
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads


async def test_acp_lists_legacy_session_with_cwd(tmp_path):
    fixture = LEGACY_THREADS["acp_session"]
    threads = install_legacy_threads(tmp_path)
    index = SessionIndex(
        Threads(FileThreadStore(threads)), legacy_dir=str(threads)
    )
    sessions = await index.list_sessions(None)
    match = [s for s in sessions if s["sessionId"] == fixture.thread_id]
    assert match, "старая сессия ACP не видна"
    assert match[0]["cwd"] == str(tmp_path / "workspace")
    assert match[0]["title"] == fixture.first_user_text
