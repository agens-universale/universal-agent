from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    serialize_shared_block,
)
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.states import SharedStateData


def test_confirm_rules_round_trips_through_meta():
    shared = SharedStateData()
    shared.confirm_rules["write:/x"] = True
    block = serialize_shared_block(shared)
    assert block["confirm_rules"] == {"write:/x": True}
    restored = ThreadMetaMapper.shared_from_meta(
        {"shared": block, "state_schema_version": "v2"}
    )
    assert restored.confirm_rules == {"write:/x": True}


def test_shared_defaults_confirm_rules_empty():
    assert SharedStateData().confirm_rules == {}


def _restore(block):
    return ThreadMetaMapper.shared_from_meta(
        {"state_schema_version": STATE_SCHEMA_VERSION, "shared": block}
    )


def test_judge_pause_and_counters_survive_save_and_restore():
    """Пауза судьи действий — состояние сессии: её снимает только человек, и
    она переживает и следующий ход, и перезапуск, как запомненные ответы."""
    shared = SharedStateData(
        judge_denials_in_a_row=2, judge_denials_total=7, judge_paused=True
    )
    restored = _restore(serialize_shared_block(shared))
    assert restored.judge_denials_in_a_row == 2
    assert restored.judge_denials_total == 7
    assert restored.judge_paused is True


def test_session_without_judge_denials_is_saved_exactly_as_before():
    block = serialize_shared_block(
        SharedStateData(working_memory={"goal": "g"})
    )
    assert not any(key.startswith("judge_") for key in block)
    restored = _restore(block)
    assert (restored.judge_denials_in_a_row, restored.judge_denials_total) == (
        0,
        0,
    )
    assert restored.judge_paused is False


def test_garbage_judge_counters_in_meta_load_as_zero():
    restored = _restore(
        {
            "judge_denials_in_a_row": "x",
            "judge_denials_total": -5,
            "judge_paused": "no",
        }
    )
    assert (restored.judge_denials_in_a_row, restored.judge_denials_total) == (
        0,
        0,
    )
    assert restored.judge_paused is False
