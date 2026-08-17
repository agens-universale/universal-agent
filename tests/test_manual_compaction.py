from types import SimpleNamespace

from gc_core import manual_compaction


def test_compact_thread_noop_in_responses_mode():
    ctx = SimpleNamespace(config_dict={"backend_api_mode": "responses"},
                          tools_spec=[], tools_description="", logger=None)
    assert manual_compaction.compact_thread(ctx, "t-1") is False


def test_compact_thread_noop_on_empty_thread_id():
    ctx = SimpleNamespace(config_dict={"backend_api_mode": "chat_completions"},
                          tools_spec=[], tools_description="", logger=None)
    assert manual_compaction.compact_thread(ctx, "") is False
