"""ACP видит сессии формата develop (спека хранилищ v2, 9.4).

На этапе 3 индекс сессий уступает место ThreadInfo.workspace; ожидания —
из manifest.json фикстур.
"""

from gc_acp.session_index import SessionIndex
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads


def test_acp_lists_legacy_session_with_cwd(tmp_path):
    fixture = LEGACY_THREADS["acp_session"]
    threads = install_legacy_threads(tmp_path)
    sessions = SessionIndex(str(threads)).list_sessions(None)
    match = [s for s in sessions if s["sessionId"] == fixture.thread_id]
    assert match, "старая сессия ACP не видна"
    assert match[0]["cwd"] == str(tmp_path / "workspace")
    assert match[0]["title"] == fixture.first_user_text
