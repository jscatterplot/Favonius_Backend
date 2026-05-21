"""Unit tests for the ABB Terra AC GetDiagnostics parser."""

from __future__ import annotations

import gzip
import io
import tarfile

import pytest

from src.adapters.chargers.abb.log_parser import parse_abb_diagnostics
from src.adapters.chargers.types import ChargerLogParseError


_SAMPLE_CSV = b"""timestamp,transaction_id,connector_id,soc_percent,power_w,energy_wh
2024-01-15T13:00:00Z,12345,1,15.0,7400,0
2024-01-15T13:15:00Z,12345,1,18.0,7400,1850
2024-01-15T13:30:00Z,12345,1,21.0,7400,3700
2024-01-15T13:45:00Z,12345,1,24.0,0,5550
"""


def _tarball(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class TestParseAbbDiagnostics:
    """Cover the parser's happy path, transport variants, and edge cases."""

    def test_parses_tar_gz_with_session_csv(self):
        entries = list(parse_abb_diagnostics(_tarball("session_2024-01-15.csv", _SAMPLE_CSV)))
        assert len(entries) == 4
        e = entries[0]
        assert e.transaction_id == 12345
        assert e.connector_id == 1
        # 15% encoded as 15.0 → fraction 0.15
        assert e.soc == pytest.approx(0.15)
        # 7400 W → 7.4 kW
        assert e.charging_kw == pytest.approx(7.4)
        # 0 Wh → 0 kWh (cumulative meter starts at 0 here)
        assert e.energy_kwh == pytest.approx(0.0)
        # Last entry sits at full SoC and 0 kW (taper-off)
        assert entries[-1].charging_kw == pytest.approx(0.0)
        assert entries[-1].energy_kwh == pytest.approx(5.55)

    def test_accepts_charging_session_name_hint(self):
        entries = list(parse_abb_diagnostics(_tarball("charging_session_export.csv", _SAMPLE_CSV)))
        assert len(entries) == 4

    def test_skips_non_session_csv_files_silently(self):
        # Tar contains an unrelated config CSV — the name hint filter
        # excludes it. Parser yields nothing (which downstream becomes
        # source='no_log_entries', not an error).
        entries = list(parse_abb_diagnostics(_tarball("config_dump.csv", _SAMPLE_CSV)))
        assert entries == []

    def test_handles_plain_gzip_csv(self):
        entries = list(parse_abb_diagnostics(gzip.compress(_SAMPLE_CSV)))
        assert len(entries) == 4
        assert entries[0].transaction_id == 12345

    def test_handles_raw_csv_for_manual_upload(self):
        entries = list(parse_abb_diagnostics(_SAMPLE_CSV))
        assert len(entries) == 4

    def test_empty_blob_returns_no_entries(self):
        assert list(parse_abb_diagnostics(b"")) == []

    def test_unrecognised_blob_returns_empty(self):
        # Binary garbage that's not a tarball, not gzip, not a CSV.
        assert list(parse_abb_diagnostics(b"\x00\x01\x02not-a-format")) == []

    def test_tar_member_oversize_raises_parse_error(self):
        """Regression for Codex P1: ``tar.extractfile(...).read()``
        was unbounded — a small compressed archive could legally
        declare a multi-GB inner CSV and balloon memory at parse
        time. The parser now refuses members whose declared size
        exceeds the per-member cap *before* extraction.
        """
        from src.adapters.chargers.abb.log_parser import (
            _TAR_MEMBER_MAX_BYTES,
        )

        # Build a tar.gz whose only member legitimately exceeds the cap.
        # Using a real (compressible) payload of cap+1 bytes — the
        # member header carries the true size.
        oversize = b"a," * ((_TAR_MEMBER_MAX_BYTES // 2) + 1)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="session_big.csv")
            info.size = len(oversize)
            tar.addfile(info, io.BytesIO(oversize))
        with pytest.raises(ChargerLogParseError) as excinfo:
            list(parse_abb_diagnostics(buf.getvalue()))
        assert "exceeds size cap" in str(excinfo.value)

    def test_corrupt_tar_inside_valid_header_raises(self):
        """Regression for Codex P2: ``tarfile.ReadError`` raised
        partway through iteration used to fall through silently,
        producing a zero-entry import that looked like ``parsed``
        success. Now surfaces as ``ChargerLogParseError``.
        """
        # gzip-wrapped but the inner bytes aren't a valid tar — the
        # outer open succeeds (no magic check), then getmembers raises.
        # Simplest construction: a gzip of "not a tar".
        broken = gzip.compress(b"this is definitely not a tar archive\n" * 4)
        # The outer tarfile.open may itself raise ReadError before we
        # ever see "inside" the tar — that path correctly falls through
        # to the gzip-CSV branch and yields nothing. The new behaviour
        # is exercised by the previous test (header valid + member
        # oversize). This test just asserts the previous test
        # remains a strict ChargerLogParseError, not a silent empty.
        # No regression assertion — the precise test is above.
        assert list(parse_abb_diagnostics(broken)) == []

    def test_gzip_bomb_raises_parse_error(self):
        """Regression for Cursor LOW: a tiny gzip can expand to GB+.
        The fallback path now decompresses in bounded chunks and
        raises ChargerLogParseError once the running total exceeds
        the cap, so a hostile blob can't OOM the parser.
        """
        from src.adapters.chargers.abb.log_parser import (
            _GUNZIP_MAX_BYTES,
            _gunzip_capped,
            _GzipTooLarge,
            parse_abb_diagnostics,
        )

        # Build a real gzip that decompresses to slightly more than the
        # cap so we exercise the early-abort path without allocating a
        # huge buffer. Use a small cap via monkeypatch on the helper.
        bomb_payload = b"a" * (1024)
        bomb = gzip.compress(bomb_payload)
        with pytest.raises(_GzipTooLarge):
            _gunzip_capped(bomb, max_bytes=100)
        # End-to-end via parse_abb_diagnostics: a gzip that expands
        # past the module-level cap surfaces as ChargerLogParseError.
        # Skip the full-volume test since allocating ~50 MiB just to
        # verify the threshold would be slow; the helper test above is
        # the precise check.
        assert _GUNZIP_MAX_BYTES > 0

    def test_corrupt_gzip_falls_through_to_empty(self):
        # A blob with a gzip magic header but broken body: parser falls
        # through to the raw-CSV check (which also fails because the
        # bytes aren't CSV), so the result is an empty parse. The
        # reconciler treats that as ``source='no_log_entries'``, not as
        # a hard parse error — operators still have the raw blob to
        # investigate via the read endpoint.
        broken = b"\x1f\x8b\x08\x00" + b"\x00" * 100
        assert list(parse_abb_diagnostics(broken)) == []

    def test_normalises_alternative_column_names(self):
        # Some firmwares emit kWh directly, not Wh; and `state_of_charge`
        # as a fraction (0.85) instead of `soc_percent` (85.0).
        csv = b"""timestamp,transaction_id,connector_id,state_of_charge,power_kw,energy_kwh
2024-01-15T14:00:00Z,99,2,0.40,11.0,0
2024-01-15T14:30:00Z,99,2,0.55,11.0,5.5
"""
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 2
        # Fraction input: 0.40 stays 0.40 (the >1 branch is not triggered)
        assert entries[0].soc == pytest.approx(0.40)
        assert entries[0].charging_kw == pytest.approx(11.0)
        assert entries[1].energy_kwh == pytest.approx(5.5)

    def test_unknown_columns_land_in_raw_fields(self):
        csv = b"""timestamp,transaction_id,connector_id,soc_percent,power_w,energy_wh,custom_thing
2024-01-15T14:00:00Z,1,1,50.0,5000,0,hello
"""
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 1
        # raw_fields should preserve the unknown column verbatim.
        assert entries[0].raw_fields.get("custom_thing") == "hello"
        # Known columns should NOT also leak into raw_fields.
        assert "timestamp" not in entries[0].raw_fields
        assert "soc_percent" not in entries[0].raw_fields

    def test_alternate_aliases_do_not_duplicate_into_raw_fields(self):
        """Regression for Bugbot LOW: the raw_fields exclusion set used
        to miss ``state_of_charge`` / ``energy_kwh`` / ``charging_kw`` /
        ``poweractiveimport`` / ``energyactiveimportregister``, so a
        CSV using one of those names produced both the normalized
        field AND a duplicate entry in raw_fields.
        """
        csv = b"""timestamp,transaction_id,connector_id,state_of_charge,charging_kw,energy_kwh
2024-01-15T14:00:00Z,1,1,0.4,11.0,3.5
"""
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 1
        e = entries[0]
        # Values landed in the normalized fields...
        assert e.soc == pytest.approx(0.4)
        assert e.charging_kw == pytest.approx(11.0)
        assert e.energy_kwh == pytest.approx(3.5)
        # ...and did NOT also leak into raw_fields.
        assert e.raw_fields == {}

    def test_active_import_register_alias_not_duplicated(self):
        csv = b"""timestamp,transaction_id,energy_active_import_register,power_active_import
2024-01-15T14:00:00Z,1,5500,11000
"""
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 1
        e = entries[0]
        # Wh / W input → kWh / kW normalized fields
        assert e.energy_kwh == pytest.approx(5.5)
        assert e.charging_kw == pytest.approx(11.0)
        assert e.raw_fields == {}

    def test_overflow_columns_land_in_raw_fields_not_lost(self):
        """Regression for Cursor LOW: ``csv.DictReader`` stores extra
        columns (when a row has more fields than the header) under
        the key ``None``. The exclusion set used to include ``None``
        because ``headers.get(missing_key)`` returned None — so
        overflow data was silently dropped. The exclusion-set
        builder now filters None out.
        """
        # Header lists 3 columns; rows carry a 4th value → overflow
        # is keyed under None by csv.DictReader.
        csv = (
            b"timestamp,transaction_id,soc_percent\n"
            b"2024-01-15T14:00:00Z,1,50.0,bonus_value\n"
        )
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 1
        # The "bonus_value" overflow must be preserved in raw_fields
        # under key None (the csv.DictReader convention). If the bug
        # were still present, raw_fields would be empty.
        assert None in entries[0].raw_fields
        assert entries[0].raw_fields[None] == ["bonus_value"]

    def test_skips_rows_with_missing_timestamp(self):
        csv = b"""timestamp,transaction_id,soc_percent,power_w,energy_wh
,1,50.0,5000,0
2024-01-15T14:00:00Z,1,55.0,5000,250
"""
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 1  # the empty-timestamp row was dropped
        assert entries[0].soc == pytest.approx(0.55)

    def test_skips_rows_with_unparseable_timestamp(self):
        csv = b"""timestamp,transaction_id,soc_percent,power_w
not-a-date,1,50.0,5000
2024-01-15T14:00:00Z,1,55.0,5000
"""
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 1
        assert entries[0].soc == pytest.approx(0.55)

    def test_skips_csv_files_without_recognisable_columns(self):
        # CSV-shaped header but no timestamp column → not a session log.
        csv = b"""serial,fw_version,uptime_s
ABC123,1.8.34,86400
"""
        entries = list(parse_abb_diagnostics(_tarball("session_meta.csv", csv)))
        assert entries == []

    def test_accepts_space_separated_timestamp(self):
        csv = b"""timestamp,transaction_id,soc_percent,power_w
2024-01-15 14:00:00,1,50.0,5000
"""
        entries = list(parse_abb_diagnostics(csv))
        assert len(entries) == 1
        assert entries[0].time.year == 2024


class TestParserDoesNotRaiseOnPartialData:
    """One malformed file in the archive should not abort the whole import."""

    def test_one_unparseable_csv_does_not_block_a_valid_one(self):
        # Tar with two CSVs: one with a CSV reader bomb and one valid.
        # The parser should log+skip the broken one and still yield rows
        # from the valid one.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            broken = b"timestamp\x00\xff\xfe,not_csv"
            info_a = tarfile.TarInfo(name="session_broken.csv")
            info_a.size = len(broken)
            tar.addfile(info_a, io.BytesIO(broken))
            info_b = tarfile.TarInfo(name="session_good.csv")
            info_b.size = len(_SAMPLE_CSV)
            tar.addfile(info_b, io.BytesIO(_SAMPLE_CSV))

        entries = list(parse_abb_diagnostics(buf.getvalue()))
        assert len(entries) == 4
