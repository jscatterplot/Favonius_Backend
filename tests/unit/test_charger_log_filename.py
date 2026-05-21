"""Tests for the upload filename extractor.

Regression for Cursor LOW: ``split("filename=")[-1]`` returned the
whole header value when ``filename=`` was absent, producing garbage
file names. ``_extract_upload_filename`` now uses a regex and only
honours an explicit ``filename=`` parameter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.api.main import _extract_upload_filename


def _req(headers: dict[str, str]) -> MagicMock:
    """Stand-in for ``starlette.requests.Request.headers``."""
    req = MagicMock()
    # Starlette headers are case-insensitive; mimic .get(...) directly.
    req.headers.get.side_effect = lambda name, default=None: headers.get(name, default)
    return req


class TestExtractUploadFilename:
    def test_x_file_name_wins(self):
        req = _req({"X-File-Name": "abc.tar.gz", "Content-Disposition": "attachment"})
        assert _extract_upload_filename(req) == "abc.tar.gz"

    def test_returns_none_when_no_filename_in_disposition(self):
        # Before the fix this returned ``"attachment"`` because the
        # naive split fell through to ``[-1]``.
        req = _req({"Content-Disposition": "attachment"})
        assert _extract_upload_filename(req) is None

    def test_returns_none_when_no_headers_present(self):
        req = _req({})
        assert _extract_upload_filename(req) is None

    def test_parses_quoted_filename(self):
        req = _req({"Content-Disposition": 'attachment; filename="diag.tar.gz"'})
        assert _extract_upload_filename(req) == "diag.tar.gz"

    def test_parses_unquoted_filename(self):
        req = _req({"Content-Disposition": "attachment; filename=diag.csv"})
        assert _extract_upload_filename(req) == "diag.csv"

    def test_case_insensitive_parameter(self):
        req = _req({"Content-Disposition": "attachment; FileName=diag.csv"})
        assert _extract_upload_filename(req) == "diag.csv"

    def test_strips_directory_components(self):
        req = _req(
            {"Content-Disposition": 'attachment; filename="/etc/passwd"'}
        )
        assert _extract_upload_filename(req) == "passwd"

    def test_strips_windows_directory_components(self):
        req = _req(
            {"Content-Disposition": r'attachment; filename="C:\evil\diag.tar.gz"'}
        )
        assert _extract_upload_filename(req) == "diag.tar.gz"

    def test_drops_control_characters(self):
        req = _req({"X-File-Name": "diag\r\n.tar.gz"})
        assert _extract_upload_filename(req) == "diag.tar.gz"

    def test_clips_overlong_filename(self):
        req = _req({"X-File-Name": "a" * 600})
        assert len(_extract_upload_filename(req)) == 255
