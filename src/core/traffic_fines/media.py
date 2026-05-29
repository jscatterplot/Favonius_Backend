"""Media-type detection and Anthropic content-block construction for fines.

The traffic-fine upload accepts a PDF or a common image format. Detection is by
magic bytes (authoritative — never trust a client-declared content type), and
the matching Anthropic content block is a ``document`` block for PDFs or an
``image`` block for images. Unsupported payloads return ``None`` so the caller
can fail closed without ever calling the LLM.
"""

from __future__ import annotations

import base64
from typing import Optional

PDF_MEDIA_TYPE = "application/pdf"
IMAGE_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
SUPPORTED_MEDIA_TYPES: frozenset[str] = frozenset({PDF_MEDIA_TYPE}) | IMAGE_MEDIA_TYPES


def detect_media_type(raw: bytes) -> Optional[str]:
    """Return the media type of ``raw`` from its magic bytes, or None.

    None means the payload is not a supported fine document (PDF or
    PNG/JPEG/GIF/WEBP image) and must not be sent to the LLM.
    """
    if len(raw) < 12:
        return None
    if raw[:4] == b"%PDF":
        return PDF_MEDIA_TYPE
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_supported(media_type: Optional[str]) -> bool:
    """True when ``media_type`` is a PDF or supported image type."""
    return media_type in SUPPORTED_MEDIA_TYPES


def build_content_block(raw: bytes, media_type: str) -> dict:
    """Build the Anthropic content block for a fine document.

    PDFs become a ``document`` block; images become an ``image`` block. The
    bytes are base64-encoded inline (documents are small — capped by the
    upload size limit).

    Raises:
        ValueError: If ``media_type`` is not a supported type.
    """
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError(f"unsupported media type: {media_type!r}")
    data = base64.standard_b64encode(raw).decode("ascii")
    block_type = "document" if media_type == PDF_MEDIA_TYPE else "image"
    return {
        "type": block_type,
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }
