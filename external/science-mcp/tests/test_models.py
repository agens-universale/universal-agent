from science_mcp.models import Author, ToolError, ToolResponse, WorkRecord


def test_workrecord_minimal_required_fields():
    rec = WorkRecord(source="openalex", id="W1", title="x")
    assert rec.authors == []
    assert rec.year is None
    assert rec.doi is None
    assert rec.is_open_access is None
    assert rec.extra == {}


def test_workrecord_with_authors():
    rec = WorkRecord(
        source="crossref",
        id="10.1/x",
        title="t",
        authors=[Author(name="Jane Doe"), Author(name="John Smith")],
        year=2024,
        doi="10.1/x",
    )
    assert rec.authors[0].name == "Jane Doe"


def test_toolresponse_ok_shape():
    resp = ToolResponse(
        query="crispr",
        limit=5,
        offset=0,
        has_more=True,
        sources_queried=["openalex"],
        results=[],
        warnings=[],
    )
    assert resp.ok is True
    assert resp.warnings == []


def test_toolerror_shape():
    err = ToolError(error="rate_limited", source="arxiv", message="429")
    assert err.ok is False
    assert err.error == "rate_limited"
