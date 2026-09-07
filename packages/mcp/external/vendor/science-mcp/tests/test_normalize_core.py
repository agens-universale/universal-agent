import json

from science_mcp.normalize import core_to_work


def test_core_sample(core_sample):
    raw = json.loads(core_sample)["results"][0]
    rec = core_to_work(raw)
    assert rec.source == "core"
    assert rec.is_open_access is True
    assert rec.title
