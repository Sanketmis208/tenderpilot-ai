"""Amazon Textract OCR adapter for scanned pages.

Synchronous DetectDocumentText is used per rasterised page (keeps it region-agnostic and avoids
S3 staging). Large documents should use StartDocumentAnalysis via S3 in production; see docs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.core.errors import ServiceUnavailable
from app.extraction.parsers.types import ParsedBlock


@dataclass
class OcrPage:
    text: str
    blocks: list[ParsedBlock]
    confidence: float


class TextractOcr:
    def __init__(self) -> None:
        s = get_settings()
        if not s.textract_enabled:
            raise ServiceUnavailable("Amazon Textract is not enabled (TP_TEXTRACT_ENABLED=false).")
        import boto3

        self.client = boto3.client("textract", region_name=s.s3_region)

    def ocr_image(self, png_bytes: bytes, page_width: float, page_height: float) -> OcrPage:
        resp = self.client.detect_document_text(Document={"Bytes": png_bytes})
        lines, blocks, confs = [], [], []
        for b in resp.get("Blocks", []):
            if b["BlockType"] != "LINE":
                continue
            box = b["Geometry"]["BoundingBox"]
            x0, y0 = box["Left"] * page_width, box["Top"] * page_height
            bbox = [x0, y0, x0 + box["Width"] * page_width, y0 + box["Height"] * page_height]
            lines.append(b["Text"])
            blocks.append(ParsedBlock(b["Text"], bbox))
            confs.append(b.get("Confidence", 0.0))
        return OcrPage("\n".join(lines), blocks, (sum(confs) / len(confs) / 100) if confs else 0.0)


def get_ocr() -> TextractOcr | None:
    try:
        return TextractOcr()
    except ServiceUnavailable:
        return None
