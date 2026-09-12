"""Standalone RapidOCR worker executed inside the isolated component venv."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_IMAGE_PIXELS = 25_000_000
MAX_TEXT_CHARACTERS_PER_PAGE = 100_000


def self_test() -> None:
    import onnxruntime  # noqa: F401
    import pypdfium2  # noqa: F401
    from rapidocr import RapidOCR

    RapidOCR(params={"Global.log_level": "critical"})


def extract(pdf_path: Path, pages: list[int], dpi: int, min_confidence: float) -> dict:
    import numpy as np
    import pypdfium2 as pdfium
    from rapidocr import RapidOCR

    document = pdfium.PdfDocument(str(pdf_path))
    engine = RapidOCR(params={"Global.log_level": "critical"})
    output: dict[str, dict[str, object]] = {}
    try:
        for page_number in pages:
            if page_number < 1 or page_number > len(document):
                raise ValueError(f"page {page_number} is outside the PDF")
            page = document[page_number - 1]
            bitmap = None
            image = None
            try:
                width, height = page.get_size()
                requested_scale = dpi / 72.0
                safe_scale = (MAX_IMAGE_PIXELS / max(width * height, 1)) ** 0.5
                scale = min(requested_scale, safe_scale)
                bitmap = page.render(scale=scale, grayscale=True, optimize_mode="print")
                image = bitmap.to_pil()
                result = engine(np.asarray(image))
                texts = list(result.txts or ())
                scores = [float(value) for value in (result.scores or ())]
                accepted = [
                    (text.strip(), score)
                    for text, score in zip(texts, scores, strict=False)
                    if text.strip() and score >= min_confidence
                ]
                text = "\n".join(item[0] for item in accepted)[:MAX_TEXT_CHARACTERS_PER_PAGE]
                confidence = sum(item[1] for item in accepted) / len(accepted) if accepted else None
                output[str(page_number)] = {"text": text, "confidence": confidence}
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return {"pages": output}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--pages")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            print(json.dumps({"status": "ready"}))
            return 0
        if args.pdf is None or not args.pdf.is_file() or not args.pages:
            raise ValueError("an existing PDF and at least one page are required")
        pages = [int(value) for value in args.pages.split(",")]
        if len(pages) != len(set(pages)) or any(page < 1 for page in pages):
            raise ValueError("OCR page numbers must be unique positive integers")
        if not 100 <= args.dpi <= 300 or not 0 <= args.min_confidence <= 1:
            raise ValueError("OCR limits are invalid")
        print(json.dumps(extract(args.pdf.resolve(), pages, args.dpi, args.min_confidence)))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
