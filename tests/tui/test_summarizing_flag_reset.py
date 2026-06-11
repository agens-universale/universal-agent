"""reset_stale_summarizing_flag: протухший tui_summarizing снимается на старте run'а.

Регрессия: если прошлый run прерван посреди компакции (закрытие TUI / зависание
summary-вызова на нездоровом бэкенде), флаг tui_summarizing остаётся True на диске
навсегда → TUI вечно показывает «сжимаю контекст» вместо «думаю» на каждом сообщении.
"""
from thread_store import save_thread_meta, load_thread_meta
from run_summary_coordinator import reset_stale_summarizing_flag


class _StubSession:
    def __init__(self, threads_dir, meta):
        self.threads_dir = threads_dir
        self.system_id = "cli"
        self.thread_id = "t1"
        self.meta = meta


def test_reset_clears_stale_flag_on_disk_and_in_memory(tmp_path):
    save_thread_meta(str(tmp_path), "cli", "t1",
                     {"tui_summarizing": True, "messages_count": 3})
    ts = _StubSession(str(tmp_path), {"tui_summarizing": True, "messages_count": 3})

    reset_stale_summarizing_flag(ts)

    assert "tui_summarizing" not in ts.meta            # in-memory снят
    disk = load_thread_meta(str(tmp_path), "cli", "t1")
    assert not disk.get("tui_summarizing")             # на диске снят
    assert disk.get("messages_count") == 3             # прочее сохранено


def test_reset_noop_when_flag_absent(tmp_path):
    save_thread_meta(str(tmp_path), "cli", "t1", {"messages_count": 1})
    ts = _StubSession(str(tmp_path), {"messages_count": 1})
    reset_stale_summarizing_flag(ts)                   # не должно падать/портить
    assert load_thread_meta(str(tmp_path), "cli", "t1").get("messages_count") == 1


def test_reset_none_session_is_noop():
    reset_stale_summarizing_flag(None)                 # CLI без треда — no-op
