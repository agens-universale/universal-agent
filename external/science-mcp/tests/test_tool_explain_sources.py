from science_mcp.config import load_config
from science_mcp.tools.explain_sources import run


def test_no_secret_in_response(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "SECRET_NCBI_VALUE_DO_NOT_LEAK")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2_secret")
    monkeypatch.setenv("CORE_API_KEY", "core_secret")
    monkeypatch.setenv("SCIENCE_MCP_MAILTO", "real@team.io")

    out = run(load_config())

    blob = repr(out)
    assert "SECRET_NCBI_VALUE_DO_NOT_LEAK" not in blob
    assert "s2_secret" not in blob
    assert "core_secret" not in blob
    assert "real@team.io" not in blob
    ncbi = next(s for s in out["sources"] if s["id"] == "ncbi")
    assert ncbi["key_configured"] is True
    s2 = next(s for s in out["sources"] if s["id"] == "semantic_scholar")
    assert s2["key_configured"] is True
    assert out["mailto_configured"] is True


def test_no_key_configured_returns_false(monkeypatch):
    for k in ["NCBI_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "CORE_API_KEY", "SCIENCE_MCP_MAILTO"]:
        monkeypatch.delenv(k, raising=False)
    out = run(load_config())
    ncbi = next(s for s in out["sources"] if s["id"] == "ncbi")
    assert ncbi["key_configured"] is False
    assert out["mailto_configured"] is False
