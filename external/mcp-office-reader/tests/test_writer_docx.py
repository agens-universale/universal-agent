from __future__ import annotations

from io import BytesIO

from docx import Document

from mcp_office_reader.writer import generate_docx


def _open(b: bytes) -> Document:
    return Document(BytesIO(b))


def test_headings():
    md = "# Title\n\n## Subtitle\n\n### Sub-sub\n"
    doc = _open(generate_docx(md))
    styles = [p.style.name for p in doc.paragraphs]
    assert "Heading 1" in styles
    assert "Heading 2" in styles
    assert "Heading 3" in styles


def test_plain_paragraphs():
    md = "Hello.\n\nWorld.\n"
    doc = _open(generate_docx(md))
    texts = [p.text for p in doc.paragraphs if p.text]
    assert "Hello." in texts
    assert "World." in texts


def test_heading_text_preserved():
    md = "# The Title\n"
    doc = _open(generate_docx(md))
    h1 = [p for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert len(h1) == 1
    assert h1[0].text == "The Title"


def test_empty_markdown_returns_valid_docx():
    doc = _open(generate_docx(""))
    # An empty docx has at least one empty body paragraph; the call must succeed.
    assert doc is not None


def test_bold_italic_code_inline():
    md = "This is **bold**, *italic*, and `code`.\n"
    doc = _open(generate_docx(md))
    p = [p for p in doc.paragraphs if p.text][0]
    runs = {r.text: (r.bold, r.italic) for r in p.runs if r.text.strip()}
    assert "bold" in runs and runs["bold"][0] is True
    assert "italic" in runs and runs["italic"][1] is True
    # The `code` run is neither bold nor italic, but uses the Consolas font.
    code_runs = [r for r in p.runs if r.text == "code"]
    assert code_runs and code_runs[0].font.name == "Consolas"


def test_link_emits_text():
    md = "Visit [Google](https://google.com).\n"
    doc = _open(generate_docx(md))
    text = "".join(p.text for p in doc.paragraphs)
    assert "Google" in text
    assert "Visit" in text


def test_nested_bold_italic():
    md = "***both***\n"
    doc = _open(generate_docx(md))
    p = [p for p in doc.paragraphs if p.text][0]
    # At least one run should have both bold and italic set.
    assert any(r.bold and r.italic for r in p.runs)


def test_bulleted_list():
    md = "- apple\n- banana\n- cherry\n"
    doc = _open(generate_docx(md))
    bullets = [p for p in doc.paragraphs if p.style.name.startswith("List Bullet")]
    assert len(bullets) == 3
    assert [p.text for p in bullets] == ["apple", "banana", "cherry"]


def test_numbered_list():
    md = "1. first\n2. second\n"
    doc = _open(generate_docx(md))
    numbered = [p for p in doc.paragraphs if p.style.name.startswith("List Number")]
    assert len(numbered) == 2
    assert [p.text for p in numbered] == ["first", "second"]


def test_nested_list():
    md = "- outer\n  - inner\n"
    doc = _open(generate_docx(md))
    items = [p for p in doc.paragraphs if "List" in p.style.name]
    assert len(items) == 2
    texts = [p.text for p in items]
    assert "outer" in texts
    assert "inner" in texts
    # Nested item should have a larger left indent than the outer item.
    outer = next(p for p in items if p.text == "outer")
    inner = next(p for p in items if p.text == "inner")
    inner_indent = inner.paragraph_format.left_indent or 0
    outer_indent = outer.paragraph_format.left_indent or 0
    assert inner_indent > outer_indent


def test_gfm_table_rendered():
    md = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob   | 25 |\n"
    doc = _open(generate_docx(md))
    assert len(doc.tables) == 1
    t = doc.tables[0]
    assert t.rows[0].cells[0].text == "Name"
    assert t.rows[0].cells[1].text == "Age"
    assert t.rows[1].cells[0].text == "Alice"
    assert t.rows[2].cells[1].text == "25"


def test_gfm_table_header_is_bold():
    md = "| H1 | H2 |\n|----|----|\n| a | b |\n"
    doc = _open(generate_docx(md))
    t = doc.tables[0]
    header_runs = [
        run for cell in t.rows[0].cells for run in cell.paragraphs[0].runs
    ]
    assert header_runs
    assert all(r.bold for r in header_runs)
    # Body row should not be bold.
    body_runs = [
        run for cell in t.rows[1].cells for run in cell.paragraphs[0].runs
    ]
    assert body_runs
    assert not any(r.bold for r in body_runs)


def test_fenced_code_block():
    md = "```\nprint('hello')\nprint('world')\n```\n"
    doc = _open(generate_docx(md))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "print('hello')" in text
    assert "print('world')" in text
    # Code block lines should render in a monospace font.
    code_paragraphs = [
        p for p in doc.paragraphs
        if p.text and p.runs and p.runs[0].font.name == "Consolas"
    ]
    assert code_paragraphs


def test_indented_code_block():
    md = "    indented_code_line\n"
    doc = _open(generate_docx(md))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "indented_code_line" in text


def test_blockquote():
    md = "> quoted line\n"
    doc = _open(generate_docx(md))
    quoted = [p for p in doc.paragraphs if "quoted line" in p.text]
    assert quoted
    p = quoted[0]
    left_indent = p.paragraph_format.left_indent
    assert p.style.name.lower().endswith("quote") or (
        left_indent is not None and left_indent > 0
    )


def test_horizontal_rule_produces_paragraph():
    md = "before\n\n---\n\nafter\n"
    doc = _open(generate_docx(md))
    texts = [p.text for p in doc.paragraphs]
    assert "before" in texts
    assert "after" in texts


def test_hyperlink_emits_w_hyperlink_element():
    md = "Visit [Google](https://google.com) today.\n"
    doc = _open(generate_docx(md))
    p = next(p for p in doc.paragraphs if "Google" in p.text)
    from docx.oxml.ns import qn
    hyperlinks = p._p.findall(qn("w:hyperlink"))
    assert hyperlinks, "expected at least one w:hyperlink element"
    text_inside = "".join(t.text or "" for t in hyperlinks[0].iter(qn("w:t")))
    assert text_inside == "Google"


def test_hyperlink_target_is_recorded_in_relationships():
    md = "See [docs](https://example.com/docs).\n"
    doc = _open(generate_docx(md))
    targets = [
        rel.target_ref
        for rel in doc.part.rels.values()
        if rel.reltype.endswith("/hyperlink")
    ]
    assert "https://example.com/docs" in targets


def test_hyperlink_is_styled_as_hyperlink():
    md = "Click [here](https://example.com).\n"
    doc = _open(generate_docx(md))
    p = next(p for p in doc.paragraphs if "here" in p.text)
    from docx.oxml.ns import qn
    hyperlink = p._p.find(qn("w:hyperlink"))
    assert hyperlink is not None
    rstyle_vals = [
        rs.get(qn("w:val"))
        for rs in hyperlink.iter(qn("w:rStyle"))
    ]
    color_vals = [
        c.get(qn("w:val"))
        for c in hyperlink.iter(qn("w:color"))
    ]
    has_underline = any(True for _ in hyperlink.iter(qn("w:u")))
    is_styled = (
        "Hyperlink" in rstyle_vals
        or (any((v or "").lower() in {"0000ff", "0563c1"} for v in color_vals) and has_underline)
    )
    assert is_styled, "hyperlink runs should be visually distinct"
