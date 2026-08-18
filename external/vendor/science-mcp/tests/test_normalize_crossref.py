import json

from science_mcp.normalize import _crossref_author, crossref_to_work


def test_author_given_family():
    a = _crossref_author({"given": "Jane", "family": "Doe"})
    assert a.name == "Jane Doe"


def test_author_organization():
    a = _crossref_author({"name": "World Health Organization"})
    assert a.name == "World Health Organization"


def test_author_family_only():
    assert _crossref_author({"family": "Smith"}).name == "Smith"


def test_author_empty():
    assert _crossref_author({}).name == "Unknown"


def test_crossref_sample(crossref_sample):
    item = json.loads(crossref_sample)["message"]["items"][0]
    rec = crossref_to_work(item)
    assert rec.source == "crossref"
    assert rec.doi
    assert rec.title
