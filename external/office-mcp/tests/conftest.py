from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    d = Path(__file__).parent / "fixtures"
    d.mkdir(exist_ok=True)
    return d


@pytest.fixture(scope="session")
def sample_docx(fixtures_dir: Path) -> Path:
    path = fixtures_dir / "sample.docx"
    if not path.exists():
        from docx import Document
        doc = Document()
        doc.add_heading("Sample Heading", level=1)
        doc.add_paragraph("Hello from a sample document.")
        doc.save(path)
    return path


@pytest.fixture(scope="session")
def sample_xlsx(fixtures_dir: Path) -> Path:
    path = fixtures_dir / "sample.xlsx"
    if not path.exists():
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["name", "amount"])
        ws.append(["alpha", 100])
        ws.append(["beta", 200])
        wb.save(path)
    return path


@pytest.fixture(scope="session")
def sample_pptx(fixtures_dir: Path) -> Path:
    path = fixtures_dir / "sample.pptx"
    if not path.exists():
        from pptx import Presentation
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "Sample Slide"
        slide.placeholders[1].text = "Hello from a sample presentation."
        prs.save(path)
    return path


@pytest.fixture(scope="session")
def sample_csv(fixtures_dir: Path) -> Path:
    path = fixtures_dir / "sample.csv"
    if not path.exists():
        path.write_text("name,amount\nalpha,100\nbeta,200\n", encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def sample_html(fixtures_dir: Path) -> Path:
    path = fixtures_dir / "sample.html"
    if not path.exists():
        path.write_text(
            "<html><body><h1>Sample Heading</h1><p>Hello from HTML.</p></body></html>",
            encoding="utf-8",
        )
    return path


@pytest.fixture(scope="session")
def sample_pdf(fixtures_dir: Path) -> Path:
    path = fixtures_dir / "sample.pdf"
    if not path.exists():
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(path))
        c.drawString(100, 750, "Sample PDF for tests")
        c.showPage()
        c.save()
    return path
