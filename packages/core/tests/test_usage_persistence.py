from types import SimpleNamespace

from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.usage.account import CATEGORY_MAIN, CATEGORY_SUBAGENT, UsageAccount


def _fake_state(usage: UsageAccount) -> SimpleNamespace:
    return SimpleNamespace(
        shared=None,
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        dialog_steps_offset=0,
        usage=usage,
    )


def test_merge_state_into_meta_writes_token_usage():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    acc.add(CATEGORY_SUBAGENT, 40, 8)
    meta = ThreadMetaMapper.merge_state_into_meta({}, _fake_state(acc))
    assert "token_usage" in meta
    restored = UsageAccount.from_meta(meta["token_usage"])
    assert restored.totals() == acc.totals()


def test_token_usage_meta_roundtrip_accumulates():
    # Прогон 1
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    meta = ThreadMetaMapper.merge_state_into_meta({}, _fake_state(acc))

    # Прогон 2 (resume): гидрируем кумулятив и докидываем
    acc2 = UsageAccount.from_meta(meta.get("token_usage"))
    acc2.add(CATEGORY_MAIN, 50, 10)
    meta2 = ThreadMetaMapper.merge_state_into_meta(meta, _fake_state(acc2))

    final = UsageAccount.from_meta(meta2["token_usage"])
    t = final.totals()
    assert t["input"] == 150 and t["output"] == 30


def test_merge_state_into_meta_missing_usage_field_ok():
    state = SimpleNamespace(
        shared=None,
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        dialog_steps_offset=0,
    )
    meta = ThreadMetaMapper.merge_state_into_meta({}, state)
    # Нет state.usage → не падаем, ключ просто не добавляется
    assert "token_usage" not in meta
