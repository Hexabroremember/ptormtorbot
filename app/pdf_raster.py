"""Rasterize PDF page 1 to JPEG for Telegram sendPhoto (shared by bot + API delivery)."""

from __future__ import annotations

import logging
from io import BytesIO

import fitz
from PIL import Image

logger = logging.getLogger(__name__)


def pdf_bytes_to_telegram_jpeg(
    pdf_bytes: bytes,
    *,
    zoom: float = 2.25,
    jpeg_quality: int = 78,
    max_long_edge: int = 2000,
) -> bytes | None:
    """Rasterize first PDF page to a JPEG, scaled for Telegram."""
    try:
        data = bytes(pdf_bytes)
        doc = fitz.open(stream=data, filetype="pdf")
        mat = fitz.Matrix(zoom, zoom)
        if len(doc) < 1:
            doc.close()
            return None
        pix = doc[0].get_pixmap(matrix=mat, alpha=False)
        doc.close()
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        lw, lh = image.size
        longest = max(lw, lh)
        if longest > max_long_edge:
            r = max_long_edge / longest
            image = image.resize(
                (max(1, int(lw * r)), max(1, int(lh * r))),
                Image.Resampling.LANCZOS,
            )

        buf = BytesIO()
        image.save(
            buf,
            format="JPEG",
            quality=jpeg_quality,
            optimize=True,
            progressive=True,
        )
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to rasterize PDF for Telegram JPEG")
        return None
