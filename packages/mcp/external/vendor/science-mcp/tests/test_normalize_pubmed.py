from science_mcp.normalize import pubmed_xml_to_works


def test_pubmed_efetch_xml_basic(pubmed_efetch_xml):
    records = pubmed_xml_to_works(pubmed_efetch_xml)
    assert len(records) >= 1
    r = records[0]
    assert r.source == "pubmed"
    assert r.pmid
    assert r.title


def test_pubmed_efetch_extracts_pmcid(pubmed_efetch_xml):
    records = pubmed_xml_to_works(pubmed_efetch_xml)
    # the fixture's single article carries <ArticleId IdType="pmc">PMC13175772
    assert records[0].pmcid == "PMC13175772"
