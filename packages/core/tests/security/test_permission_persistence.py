from gc_core.states import SharedStateData
from gc_core.persistence.schema import serialize_shared_block
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper


def test_confirm_rules_round_trips_through_meta():
    shared = SharedStateData()
    shared.confirm_rules["write:/x"] = True
    block = serialize_shared_block(shared)
    assert block["confirm_rules"] == {"write:/x": True}
    restored = ThreadMetaMapper.shared_from_meta({"shared": block, "state_schema_version": "v2"})
    assert restored.confirm_rules == {"write:/x": True}


def test_shared_defaults_confirm_rules_empty():
    assert SharedStateData().confirm_rules == {}
