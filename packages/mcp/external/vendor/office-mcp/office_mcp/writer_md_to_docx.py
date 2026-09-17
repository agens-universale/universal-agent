from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

from docx import Document
from docx.document import Document as DocxDocument
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from markdown_it import MarkdownIt


def md_to_docx_bytes(markdown: str) -> bytes:
    doc = Document()
    md = MarkdownIt("commonmark", {"html": False}).enable(["table"])
    tokens = md.parse(markdown or "")
    _render(doc, tokens)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


@dataclass
class _RenderState:
    # Stack of "bullet" / "ordered"; depth == nesting level.
    list_stack: list[str] = field(default_factory=list)
    # True between blockquote_open / blockquote_close.
    in_quote: bool = False


def _render(doc: DocxDocument, tokens) -> None:
    state = _RenderState()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            level = int(tok.tag[1:])  # h1 -> 1
            inline = tokens[i + 1]
            p = doc.add_heading(level=level)
            _emit_inline(p, inline.children or [])
            i += 3  # heading_open + inline + heading_close
            continue
        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            p = _new_paragraph(doc, state)
            _emit_inline(p, inline.children or [])
            i += 3
            continue
        if tok.type in ("bullet_list_open", "ordered_list_open"):
            state.list_stack.append(
                "bullet" if tok.type == "bullet_list_open" else "ordered"
            )
            i += 1
            continue
        if tok.type in ("bullet_list_close", "ordered_list_close"):
            if state.list_stack:
                state.list_stack.pop()
            i += 1
            continue
        if tok.type == "table_open":
            end, rows = _collect_table(tokens, i)
            _emit_table(doc, rows)
            i = end + 1
            continue
        if tok.type in ("fence", "code_block"):
            _emit_code_block(doc, tok.content)
            i += 1
            continue
        if tok.type == "blockquote_open":
            state.in_quote = True
            i += 1
            continue
        if tok.type == "blockquote_close":
            state.in_quote = False
            i += 1
            continue
        if tok.type == "hr":
            _emit_hr(doc)
            i += 1
            continue
        i += 1


def _collect_table(tokens, start: int) -> tuple[int, list[list[str]]]:
    rows: list[list[str]] = []
    current: list[str] = []
    i = start + 1
    while i < len(tokens) and tokens[i].type != "table_close":
        tok = tokens[i]
        if tok.type == "tr_open":
            current = []
        elif tok.type == "tr_close":
            rows.append(current)
        elif tok.type in ("th_open", "td_open"):
            inline = tokens[i + 1]
            current.append(_inline_as_text(inline.children or []))
            i += 2  # skip inline + cell_close
        i += 1
    return i, rows


def _inline_as_text(children) -> str:
    parts: list[str] = []
    for c in children:
        if c.type == "text":
            parts.append(c.content)
        elif c.type == "code_inline":
            parts.append(c.content)
    return "".join(parts)


def _emit_table(doc: DocxDocument, rows: list[list[str]]) -> None:
    if not rows:
        return
    n_cols = len(rows[0])
    t = doc.add_table(rows=len(rows), cols=n_cols)
    t.style = "Table Grid"
    for r_idx, row in enumerate(rows):
        for c_idx, text in enumerate(row):
            t.rows[r_idx].cells[c_idx].text = text
    # Bold the header row.
    for cell in t.rows[0].cells:
        for run in cell.paragraphs[0].runs:
            run.bold = True


def _new_paragraph(doc: DocxDocument, state: _RenderState):
    if state.list_stack:
        style = "List Bullet" if state.list_stack[-1] == "bullet" else "List Number"
        p = doc.add_paragraph(style=style)
        depth = len(state.list_stack) - 1
        if depth > 0:
            p.paragraph_format.left_indent = Pt(18 * depth)
        return p
    if state.in_quote:
        return doc.add_paragraph(style="Quote")
    return doc.add_paragraph()


def _emit_code_block(doc: DocxDocument, content: str) -> None:
    # One paragraph per line; monospaced font.
    for line in content.rstrip("\n").split("\n"):
        p = doc.add_paragraph()
        r = p.add_run(line)
        r.font.name = "Consolas"


def _emit_hr(doc: DocxDocument) -> None:
    # Render HR as an empty paragraph with a single bottom border.
    p = doc.add_paragraph()
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:color"), "auto")
    pBdr.append(bottom)
    pPr.append(pBdr)


def _emit_inline(paragraph, children) -> None:
    bold = False
    italic = False
    link_element = None
    for tok in children:
        if tok.type == "strong_open":
            bold = True
        elif tok.type == "strong_close":
            bold = False
        elif tok.type == "em_open":
            italic = True
        elif tok.type == "em_close":
            italic = False
        elif tok.type == "link_open":
            href = (tok.attrs or {}).get("href")
            if href:
                r_id = paragraph.part.relate_to(
                    href, RT.HYPERLINK, is_external=True
                )
                link_element = OxmlElement("w:hyperlink")
                link_element.set(qn("r:id"), r_id)
                paragraph._p.append(link_element)
        elif tok.type == "link_close":
            link_element = None
        elif tok.type == "code_inline":
            r = paragraph.add_run(tok.content)
            r.font.name = "Consolas"
            _set_run_style(r, bold=bold, italic=italic)
            if link_element is not None:
                link_element.append(r._r)
                _apply_hyperlink_style(r)
        elif tok.type == "text":
            r = paragraph.add_run(tok.content)
            _set_run_style(r, bold=bold, italic=italic)
            if link_element is not None:
                link_element.append(r._r)
                _apply_hyperlink_style(r)
        elif tok.type in ("softbreak", "hardbreak"):
            paragraph.add_run("\n")


def _set_run_style(run, bold: bool, italic: bool) -> None:
    if bold:
        run.bold = True
    if italic:
        run.italic = True


def _apply_hyperlink_style(run) -> None:
    """Make a run look like a hyperlink: try Hyperlink style, fall back to blue+underline."""
    try:
        run.style = "Hyperlink"
    except (KeyError, ValueError):
        pass
    rPr = run._r.get_or_add_rPr()
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    rPr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rPr.append(underline)
