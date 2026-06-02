import json

from science_mcp.normalize import dblp_hit_to_work


def test_dblp_sample(dblp_sample):
    hit = json.loads(dblp_sample)["result"]["hits"]["hit"][0]
    rec = dblp_hit_to_work(hit)
    assert rec.source == "dblp"
    assert rec.title
