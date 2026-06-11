from pathlib import Path

from office_mcp.reader import read_file_from_disk

FIXTURE = Path(__file__).parent / "fixtures" / "sample.pdf"


def test_read_file_from_disk_pdf_returns_fulltext():
    out = read_file_from_disk(
        FIXTURE,
        max_file_size=50 * 1024 * 1024,
        max_output_chars=1_000_000,
        timeout_s=60,
    )
    # meta header identifies the format
    assert out.startswith("[document: format=pdf")
    # the fixture's known body text was actually extracted (not just the meta line)
    assert "Sample PDF for tests" in out
