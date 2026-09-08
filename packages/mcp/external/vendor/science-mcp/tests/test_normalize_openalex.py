import json

from science_mcp.normalize import invert_to_text, openalex_to_work


def test_invert_to_text_basic():
    idx = {"The": [0], "study": [1], "ends.": [2]}
    assert invert_to_text(idx) == "The study ends."


def test_invert_to_text_none_and_empty():
    assert invert_to_text(None) is None
    assert invert_to_text({}) is None


def test_openalex_sample_one_record(openalex_sample):
    payload = json.loads(openalex_sample)
    raw = payload["results"][0]
    rec = openalex_to_work(raw)
    assert rec.source == "openalex"
    assert rec.title
    if rec.doi:
        assert rec.doi.startswith("10.")
    assert "abstract_inverted_index" not in rec.extra
