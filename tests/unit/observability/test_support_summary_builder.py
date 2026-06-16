"""Unit tests for the pure support-summary helpers (no I/O).

Covers the exact summary sentence, missing-error handling, status
pass-through, error-line truncation, screenshot decode/validation, and the
redacted/bounded log-excerpt renderer.
"""

import base64
import logging

import pytest

from src.observability.log_buffer import BufferedRecord
from src.observability.support_summary import (
    MAX_ERROR_LINE_CHARS,
    ScreenshotRejected,
    build_summary_text,
    decode_screenshot,
    extract_last_error_line,
    render_logs_excerpt,
)

# A small, validly-base64'd byte blob (not a real PNG, but bytes are bytes).
IMG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"payload-bytes" * 3).decode()


def test_summary_exact_sentence():
    out = build_summary_text(
        user_id="U123",
        page="/depots/x/state",
        tiger_cloud_status="healthy",
        websocket_status="unknown",
        last_error_line="Boom happened",
    )
    assert out == (
        "User U123 reported an error while viewing /depots/x/state. "
        "System status at time of report: Tiger Cloud healthy, WebSocket unknown. "
        "Last error log: Boom happened."
    )


def test_summary_no_error_renders_none():
    for value in (None, "", "   "):
        out = build_summary_text(
            user_id="U1",
            page="P",
            tiger_cloud_status="healthy",
            websocket_status="healthy",
            last_error_line=value,
        )
        assert out.endswith("Last error log: none.")


def test_summary_unknown_statuses_pass_through():
    out = build_summary_text(
        user_id="U1",
        page="P",
        tiger_cloud_status="unavailable",
        websocket_status="degraded",
        last_error_line="x",
    )
    assert "Tiger Cloud unavailable, WebSocket degraded" in out


def test_summary_truncates_long_error():
    long_error = "E" * (MAX_ERROR_LINE_CHARS + 50)
    out = build_summary_text(
        user_id="U1",
        page="P",
        tiger_cloud_status="healthy",
        websocket_status="healthy",
        last_error_line=long_error,
    )
    assert ("E" * MAX_ERROR_LINE_CHARS + "….") in out
    assert ("E" * (MAX_ERROR_LINE_CHARS + 1)) not in out


def test_decode_screenshot_none_when_absent():
    assert decode_screenshot(None, None, max_bytes=1000) is None
    assert decode_screenshot("", "image/png", max_bytes=1000) is None


def test_decode_screenshot_bare_base64():
    data, content_type = decode_screenshot(IMG_B64, "image/png", max_bytes=10_000)
    assert content_type == "image/png"
    assert isinstance(data, bytes) and len(data) > 0


def test_decode_screenshot_data_url_extracts_content_type():
    data_url = f"data:image/webp;base64,{IMG_B64}"
    data, content_type = decode_screenshot(data_url, None, max_bytes=10_000)
    assert content_type == "image/webp"
    assert isinstance(data, bytes)


def test_decode_screenshot_rejects_bad_content_type():
    with pytest.raises(ScreenshotRejected) as exc:
        decode_screenshot(IMG_B64, "application/pdf", max_bytes=10_000)
    assert exc.value.status_code == 400


def test_decode_screenshot_requires_content_type():
    with pytest.raises(ScreenshotRejected) as exc:
        decode_screenshot(IMG_B64, None, max_bytes=10_000)
    assert exc.value.status_code == 400


def test_decode_screenshot_rejects_oversize():
    big = base64.b64encode(b"x" * 2000).decode()
    with pytest.raises(ScreenshotRejected) as exc:
        decode_screenshot(big, "image/png", max_bytes=1000)
    assert exc.value.status_code == 413


def test_decode_screenshot_rejects_bad_base64():
    with pytest.raises(ScreenshotRejected) as exc:
        decode_screenshot("!!!not-base64!!!", "image/png", max_bytes=10_000)
    assert exc.value.status_code == 400


def test_render_logs_excerpt_redacts_and_formats():
    records = [
        BufferedRecord(
            ts=1000.0,
            level=logging.INFO,
            levelname="INFO",
            logger_name="svc",
            message="login user@example.com",
        )
    ]
    out = render_logs_excerpt(records, max_bytes=10_000)
    assert "[REDACTED_EMAIL]" in out
    assert "user@example.com" not in out
    assert "INFO svc:" in out


def test_render_logs_excerpt_truncates_to_tail():
    records = [
        BufferedRecord(
            ts=1000.0 + i,
            level=logging.INFO,
            levelname="INFO",
            logger_name="svc",
            message=f"line{i}-" + "y" * 50,
        )
        for i in range(100)
    ]
    out = render_logs_excerpt(records, max_bytes=200)
    assert out.startswith("…(truncated)…")
    assert len(out) < 300


def test_extract_last_error_line_redacts():
    record = BufferedRecord(
        ts=1.0,
        level=logging.ERROR,
        levelname="ERROR",
        logger_name="svc",
        message="boom for user@example.com",
    )
    line = extract_last_error_line(record)
    assert line is not None and "user@example.com" not in line


def test_extract_last_error_line_none():
    assert extract_last_error_line(None) is None
