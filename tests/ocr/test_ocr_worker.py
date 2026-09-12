from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from helix_mcp_knowledge.ocr_worker import extract

pytestmark = pytest.mark.ocr


@pytest.mark.skipif(
    any(
        importlib.util.find_spec(package) is None
        for package in ("onnxruntime", "pypdfium2", "rapidocr")
    ),
    reason="optional OCR dependencies are not installed",
)
def test_rapidocr_reads_a_completely_scanned_pdf(tmp_path: Path) -> None:
    import pypdfium2 as pdfium

    source = tmp_path / "source.pdf"
    pdf = canvas.Canvas(str(source), pagesize=A4)
    pdf.setFont("Helvetica-Bold", 34)
    pdf.drawString(70, 700, "HELIX SCANNED DEVICE DESIGN")
    pdf.save()

    document = pdfium.PdfDocument(str(source))
    page = document[0]
    bitmap = page.render(scale=3, grayscale=True, optimize_mode="print")
    image = bitmap.to_pil()
    image_path = tmp_path / "scan.png"
    image.save(image_path)
    image.close()
    bitmap.close()
    page.close()
    document.close()

    scanned = tmp_path / "scanned.pdf"
    pdf = canvas.Canvas(str(scanned), pagesize=A4)
    pdf.drawImage(ImageReader(str(image_path)), 0, 0, width=A4[0], height=A4[1])
    pdf.save()

    result = extract(scanned, [1], dpi=200, min_confidence=0.3)

    recognized = result["pages"]["1"]["text"].upper()
    assert "HELIX" in recognized
    assert "DEVICE" in recognized
