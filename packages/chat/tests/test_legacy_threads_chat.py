"""gc-chat видит диалоги формата develop (спека хранилищ v2, 9.4).

На этапе 3 responses-index уступает место потоку "chat.responses" и алиасам;
ожидания — из manifest.json фикстур.
"""

from gc_chat.dialogs import dialog_list
from gc_chat.responses.index_store import ResponseIndex
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads


def test_chat_lists_legacy_dialog(tmp_path):
    fixture = LEGACY_THREADS["chat_dialog"]
    threads = install_legacy_threads(tmp_path)
    dialogs = dialog_list(ResponseIndex(str(threads)))
    assert [d["thread_id"] for d in dialogs] == [fixture.thread_id]
