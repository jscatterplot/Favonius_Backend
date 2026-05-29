"""Unit tests for traffic-fine media detection + content-block construction."""

from __future__ import annotations

import base64

import pytest

from src.core.traffic_fines.media import (
    PDF_MEDIA_TYPE,
    build_content_block,
    detect_media_type,
    is_supported,
)

_PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj"
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x03"
_GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00"
_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "


@pytest.mark.parametrize(
    "raw,expected",
    [
        (_PDF, PDF_MEDIA_TYPE),
        (_PNG, "image/png"),
        (_JPEG, "image/jpeg"),
        (_GIF, "image/gif"),
        (_WEBP, "image/webp"),
    ],
)
def test_detect_supported(raw: bytes, expected: str):
    assert detect_media_type(raw) == expected
    assert is_supported(expected)


def test_detect_unsupported_returns_none():
    assert detect_media_type(b"PK\x03\x04 a zip or docx payload here") is None
    assert detect_media_type(b"tooshort") is None  # < 12 bytes
    assert is_supported(None) is False
    assert is_supported("application/zip") is False


def test_build_document_block_for_pdf():
    block = build_content_block(_PDF, PDF_MEDIA_TYPE)
    assert block["type"] == "document"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == PDF_MEDIA_TYPE
    assert base64.standard_b64decode(block["source"]["data"]) == _PDF


def test_build_image_block_for_png():
    block = build_content_block(_PNG, "image/png")
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"


def test_build_content_block_rejects_unsupported():
    with pytest.raises(ValueError):
        build_content_block(b"whatever", "application/zip")
