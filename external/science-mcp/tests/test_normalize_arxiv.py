from science_mcp.normalize import arxiv_atom_to_works


def test_arxiv_sample(arxiv_sample):
    recs = arxiv_atom_to_works(arxiv_sample)
    assert len(recs) >= 1
    r = recs[0]
    assert r.source == "arxiv"
    assert r.arxiv_id
    assert r.title
    assert r.abstract
    assert r.url and "arxiv.org" in r.url
