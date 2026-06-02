from science_mcp.normalize import pubmed_xml_to_works


def test_pubmed_efetch_xml_basic(pubmed_efetch_xml):
    records = pubmed_xml_to_works(pubmed_efetch_xml)
    assert len(records) >= 1
    r = records[0]
    assert r.source == "pubmed"
    assert r.pmid
    assert r.title
