from pathlib import Path

import pytest

from office_mcp.reader import (
    SUPPORTED_EXTENSIONS,
    format_meta_block,
    truncate_output,
)


def test_supported_extensions_exact_set():
    assert SUPPORTED_EXTENSIONS == (
        ".pdf", ".docx", ".xls", ".xlsx", ".pptx", ".csv", ".html", ".msg",
    )


def test_format_meta_block_with_pages():
    assert format_meta_block(fmt="pdf", total_pages=180, returned_pages="1-180") == (
        "[document: format=pdf, total_pages=180, returned_pages=1-180]"
    )


def test_format_meta_block_with_chars():
    assert format_meta_block(fmt="docx", total_chars=345000, returned_chars="1-345000") == (
        "[document: format=docx, total_chars=345000, returned_chars=1-345000]"
    )


def test_no_truncation_when_under_limit():
    text, truncated = truncate_output("hello", max_chars=100)
    assert text == "hello"
    assert truncated is False


def test_truncates_and_appends_marker():
    text, truncated = truncate_output("x" * 200, max_chars=100)
    assert truncated is True
    assert "[truncated: returned 100 of 200 characters.]" in text
    assert text.startswith("x" * 100)


def test_convert_docx_returns_markdown(sample_docx):
    from office_mcp.reader import _convert_with_markitdown
    md = _convert_with_markitdown(sample_docx)
    assert "Sample Heading" in md
    assert len(md) > 0


def test_convert_xlsx_returns_markdown(sample_xlsx):
    from office_mcp.reader import _convert_with_markitdown
    md = _convert_with_markitdown(sample_xlsx)
    assert "alpha" in md and "beta" in md


def test_convert_pdf_returns_markdown(sample_pdf):
    from office_mcp.reader import _convert_with_markitdown
    md = _convert_with_markitdown(sample_pdf)
    assert "Sample PDF" in md


def test_convert_pptx_returns_markdown(sample_pptx):
    from office_mcp.reader import _convert_with_markitdown
    md = _convert_with_markitdown(sample_pptx)
    assert "Sample Slide" in md


def test_convert_csv_returns_markdown(sample_csv):
    from office_mcp.reader import _convert_with_markitdown
    md = _convert_with_markitdown(sample_csv)
    assert "alpha" in md and "beta" in md


def test_convert_html_returns_markdown(sample_html):
    from office_mcp.reader import _convert_with_markitdown
    md = _convert_with_markitdown(sample_html)
    assert "Sample Heading" in md


def test_convert_xls_returns_markdown(fixtures_dir):
    xls = fixtures_dir / "sample.xls"
    assert xls.exists(), "sample.xls fixture must be committed to tests/fixtures/"
    from office_mcp.reader import _convert_with_markitdown
    md = _convert_with_markitdown(xls)
    assert len(md) > 0


def test_read_file_from_disk_returns_md_with_meta(sample_docx):
    from office_mcp.reader import read_file_from_disk
    out = read_file_from_disk(
        sample_docx,
        max_file_size=50 * 1024 * 1024,
        max_output_chars=1_000_000,
        timeout_s=60,
    )
    assert out.startswith("[document: format=docx")
    assert "Sample Heading" in out


def test_read_file_from_disk_unsupported_format(tmp_path):
    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_file_from_disk
    f = tmp_path / "x.rtf"
    f.write_bytes(b"x")
    with pytest.raises(OfficeReaderError) as e:
        read_file_from_disk(f, 10**7, 10**6, 60)
    assert e.value.category == ErrorCategory.UNSUPPORTED_FORMAT


def test_read_file_from_disk_file_not_found(tmp_path):
    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_file_from_disk
    with pytest.raises(OfficeReaderError) as e:
        read_file_from_disk(tmp_path / "missing.docx", 10**7, 10**6, 60)
    assert e.value.category == ErrorCategory.FILE_NOT_FOUND


def test_read_file_from_disk_file_too_large(tmp_path):
    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_file_from_disk
    f = tmp_path / "big.docx"
    f.write_bytes(b"x" * 100)
    with pytest.raises(OfficeReaderError) as e:
        read_file_from_disk(f, max_file_size=50, max_output_chars=10**6, timeout_s=60)
    assert e.value.category == ErrorCategory.FILE_TOO_LARGE


def test_read_file_from_disk_truncates_long_output(sample_docx, monkeypatch):
    from office_mcp.reader import read_file_from_disk
    monkeypatch.setattr(
        "office_mcp.reader._convert_with_markitdown",
        lambda p: "x" * 2000,
    )
    out = read_file_from_disk(sample_docx, 10**7, max_output_chars=500, timeout_s=60)
    assert "[truncated" in out


def test_read_file_from_disk_wraps_unexpected_errors_as_parse_failed(sample_docx, monkeypatch):
    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_file_from_disk

    def boom(p):
        raise RuntimeError("markitdown exploded")

    monkeypatch.setattr("office_mcp.reader._convert_with_markitdown", boom)
    with pytest.raises(OfficeReaderError) as e:
        read_file_from_disk(sample_docx, 10**7, 10**6, 60)
    assert e.value.category == ErrorCategory.PARSE_FAILED


def test_read_document_valid_base64_docx(sample_docx):
    import base64

    from office_mcp.reader import read_document_from_bytes
    b64 = base64.b64encode(sample_docx.read_bytes()).decode()
    out = read_document_from_bytes(
        filename="sample.docx",
        content_base64=b64,
        max_file_size=10**7,
        max_output_chars=10**6,
        timeout_s=60,
    )
    assert out.startswith("[document: format=docx")
    assert "Sample Heading" in out


def test_read_document_invalid_base64():
    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_document_from_bytes
    with pytest.raises(OfficeReaderError) as e:
        read_document_from_bytes("x.docx", "!!!not_base64!!!", 10**7, 10**6, 60)
    assert e.value.category == ErrorCategory.INVALID_BASE64


def test_read_document_too_large():
    import base64

    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_document_from_bytes
    big = base64.b64encode(b"x" * 10_000).decode()
    with pytest.raises(OfficeReaderError) as e:
        read_document_from_bytes(
            "x.docx", big, max_file_size=100, max_output_chars=10**6, timeout_s=60,
        )
    assert e.value.category == ErrorCategory.FILE_TOO_LARGE


def test_read_document_unsupported_extension():
    import base64

    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_document_from_bytes
    b64 = base64.b64encode(b"x").decode()
    with pytest.raises(OfficeReaderError) as e:
        read_document_from_bytes("x.rtf", b64, 10**7, 10**6, 60)
    assert e.value.category == ErrorCategory.UNSUPPORTED_FORMAT


def test_read_document_cleans_up_tempfile_on_error(sample_docx, monkeypatch):
    """When read_file_from_disk raises, the temp file must still be unlinked."""
    import base64

    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_document_from_bytes

    seen_paths: list[Path] = []

    def boom(path, **kwargs):
        seen_paths.append(path)
        raise OfficeReaderError(ErrorCategory.PARSE_FAILED, path=str(path), fmt=".docx")

    monkeypatch.setattr("office_mcp.reader.read_file_from_disk", boom)
    b64 = base64.b64encode(sample_docx.read_bytes()).decode()
    with pytest.raises(OfficeReaderError):
        read_document_from_bytes("sample.docx", b64, 10**7, 10**6, 60)
    assert len(seen_paths) == 1
    assert not seen_paths[0].exists(), "temp file should be unlinked after failure"


def test_read_file_doc_returns_unsupported_format(tmp_path):
    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_file_from_disk

    src = tmp_path / "x.doc"
    src.write_bytes(b"any bytes")

    with pytest.raises(OfficeReaderError) as exc:
        read_file_from_disk(
            src, max_file_size=10**7, max_output_chars=10**6, timeout_s=60
        )
    assert exc.value.category == ErrorCategory.UNSUPPORTED_FORMAT


def test_read_document_doc_returns_unsupported_format():
    import base64

    from office_mcp.errors import ErrorCategory, OfficeReaderError
    from office_mcp.reader import read_document_from_bytes

    payload = base64.b64encode(b"x").decode("ascii")
    with pytest.raises(OfficeReaderError) as exc:
        read_document_from_bytes(
            "x.doc", payload, max_file_size=10**7, max_output_chars=10**6, timeout_s=60
        )
    assert exc.value.category == ErrorCategory.UNSUPPORTED_FORMAT
