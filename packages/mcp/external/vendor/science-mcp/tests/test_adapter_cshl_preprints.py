import httpx
import pytest

from science_mcp.adapters import cshl_preprints
from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.throttle import Throttler


@pytest.mark.asyncio
async def test_filters_out_non_preprint_dois(tmp_path):
    crossref_payload = {
        "message": {
            "items": [
                {"DOI": "10.1101/2024.01.01.000001", "title": ["preprint A"], "issued": {"date-parts": [[2024]]}},
                {"DOI": "10.1101/cshperspect.a041384", "title": ["journal piece"], "issued": {"date-parts": [[2024]]}},
                {"DOI": "10.1101/2023.05.05.555555", "title": ["preprint B"], "issued": {"date-parts": [[2023]]}},
            ]
        }
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=crossref_payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await cshl_preprints.search(
        client, load_config(), Throttler({"crossref": 0.0, "biorxiv": 0.0}),
        DiskCache(tmp_path, enabled=False),
        query="x", limit=10, offset=0, server="biorxiv", fetch_details=False,
    )
    await client.aclose()
    dois = [r.doi for r in out]
    assert "10.1101/cshperspect.a041384" not in dois
    assert "10.1101/2024.01.01.000001" in dois
    assert "10.1101/2023.05.05.555555" in dois
    assert all(r.source == "biorxiv" for r in out)
