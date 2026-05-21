"""Parser for ABB Terra AC ``GetDiagnostics`` archives.

ABB Terra AC firmware ≥1.8.32 responds to ``GetDiagnostics`` by
uploading a ``.tar.gz`` archive. Inside the archive there is at least
one CSV-shaped log file whose name matches ``session*.csv`` or
``charging_sessions*.csv``. The column set varies slightly across
firmwares but the following columns are reliable across the pilot
fleet (FW 1.8.32-1.8.34):

    timestamp,transaction_id,connector_id,soc_percent,power_w,energy_wh

The parser is permissive: any unknown column lands in
``ChargerLogEntry.raw_fields``. Files that don't look like a session
log are skipped silently — the reconciler only needs *some* entries to
emit a meaningful row.

If the archive is not a tar.gz, we fall back to treating the whole
blob as a single CSV — operators sometimes drop a manual CSV export
from ChargerSync through the same upload endpoint.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import tarfile
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

from ..types import ChargerLogEntry, ChargerLogParseError

logger = logging.getLogger(__name__)


_SESSION_LOG_NAME_HINTS = ("session", "charging_session", "transaction")


def parse_abb_diagnostics(blob: bytes) -> Iterable[ChargerLogEntry]:
    """Extract normalized session-log entries from an ABB diagnostics blob.

    Args:
        blob: Raw bytes uploaded by the charger.

    Yields:
        :class:`ChargerLogEntry` per parsed row. A blob with zero
        recognisable rows yields nothing — the reconciler treats that
        as ``source='no_log_entries'``, not an error.

    Raises:
        ChargerLogParseError: When the archive is structurally broken
            (corrupt tar, unreadable gzip). The reconciler maps this
            to ``status='failed'`` + ``source='parse_failed'``.
    """
    if not blob:
        return iter(())

    csv_streams = list(_iter_csv_streams(blob))
    if not csv_streams:
        # Tar+gz with no candidate file is not a hard error — the
        # archive may contain other diagnostic dumps. Return empty.
        return iter(())

    entries: list[ChargerLogEntry] = []
    for name, text in csv_streams:
        try:
            for entry in _parse_csv_text(text):
                entries.append(entry)
        except ChargerLogParseError:
            raise
        except Exception as exc:
            # Skip a single bad file rather than failing the import.
            # The next CSV in the archive may still parse cleanly.
            logger.warning("ABB log parser: skipping %s: %s", name, exc)
            continue
    return iter(entries)


def _iter_csv_streams(blob: bytes) -> Iterator[tuple[str, str]]:
    """Yield (name, text) for every plausible session-log file in the blob."""
    # 1. tar.gz: the dominant ABB GetDiagnostics shape.
    try:
        buf = io.BytesIO(blob)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name_lower = member.name.lower()
                if not name_lower.endswith(".csv"):
                    continue
                if not any(hint in name_lower for hint in _SESSION_LOG_NAME_HINTS):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                data = f.read()
                yield member.name, _decode(data)
        return
    except tarfile.ReadError:
        pass
    except (EOFError, OSError) as exc:
        raise ChargerLogParseError(f"Corrupt ABB tar.gz: {exc}") from exc

    # 2. Plain gzip-wrapped CSV (some firmwares).
    # ``zlib.error`` is a subclass of OSError on 3.11+ but listed
    # explicitly so future Python versions don't quietly change the
    # behaviour. Corrupt gzip falls through to the raw-CSV check, which
    # also fails for a non-CSV blob — net result is an empty parse,
    # which the reconciler treats as ``source='no_log_entries'``.
    import zlib

    try:
        text = gzip.decompress(blob).decode("utf-8", errors="replace")
        yield "diagnostics.csv.gz", text
        return
    except (OSError, EOFError, zlib.error):
        pass

    # 3. Raw CSV. ChargerSync exports look like this when fed through
    #    the manual_upload path.
    head = blob[: min(len(blob), 256)]
    if _looks_like_csv(head):
        yield "diagnostics.csv", _decode(blob)


def _looks_like_csv(head: bytes) -> bool:
    """Cheap heuristic: line with several commas and printable text."""
    try:
        sample = head.decode("utf-8", errors="ignore")
    except Exception:
        return False
    first_line = sample.split("\n", 1)[0]
    return first_line.count(",") >= 2 and all(
        ch.isprintable() or ch in "\r\n\t" for ch in first_line
    )


def _decode(data: bytes) -> str:
    """UTF-8 with BOM tolerance + replacement so a stray byte never aborts."""
    return data.decode("utf-8-sig", errors="replace")


def _parse_csv_text(text: str) -> Iterator[ChargerLogEntry]:
    """Yield ChargerLogEntry rows from one CSV stream."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return
    headers = {_canon(h): h for h in reader.fieldnames}
    if "timestamp" not in headers and "time" not in headers:
        # Not a session-log shape — nothing to extract.
        return

    for row in reader:
        ts = _pick(row, headers, "timestamp", "time", "datetime")
        if ts is None:
            continue
        parsed_ts = _parse_timestamp(ts)
        if parsed_ts is None:
            continue
        # Energy can be reported in Wh or kWh; SoC in percent or fraction.
        # Normalize to kWh and fraction respectively so downstream
        # comparison code doesn't need to branch on vendor.
        energy_kwh = _read_energy_kwh(row, headers)
        power_kw = _read_power_kw(row, headers)
        soc = _read_soc(row, headers)
        # ``headers`` is keyed by ``_canon(header)`` (alphanumerics
        # only), so the exclusion set must list canonical forms too —
        # any underscored variant here would silently be dead code.
        # Every alias the reader helpers below accept must appear in
        # this set, or that column ends up both in the normalized
        # field AND duplicated into raw_fields.
        _KNOWN_CANON_KEYS = {
            "timestamp", "time", "datetime",
            "transactionid",
            "connectorid",
            "soc", "socpercent", "stateofcharge",
            "energykwh", "energydeliveredkwh",
            "energywh", "energyactiveimportregister",
            "powerkw", "chargingkw",
            "powerw", "poweractiveimport",
        }
        raw = {
            row_key: row[row_key]
            for row_key in row
            if row_key not in {headers.get(k) for k in _KNOWN_CANON_KEYS}
        }
        yield ChargerLogEntry(
            time=parsed_ts,
            transaction_id=_read_int(row, headers, "transactionid", "transaction_id"),
            connector_id=_read_int(row, headers, "connectorid", "connector_id"),
            soc=soc,
            charging_kw=power_kw,
            energy_kwh=energy_kwh,
            raw_fields=raw,
        )


def _canon(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())


def _pick(row: dict, headers: dict, *keys: str) -> Optional[str]:
    for key in keys:
        h = headers.get(_canon(key))
        if h is None:
            continue
        value = row.get(h)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def _read_int(row: dict, headers: dict, *keys: str) -> Optional[int]:
    value = _pick(row, headers, *keys)
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _read_float(row: dict, headers: dict, *keys: str) -> Optional[float]:
    value = _pick(row, headers, *keys)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_energy_kwh(row: dict, headers: dict) -> Optional[float]:
    """Return cumulative energy in kWh regardless of source unit."""
    kwh = _read_float(row, headers, "energykwh", "energy_kwh", "energydeliveredkwh")
    if kwh is not None:
        return kwh
    wh = _read_float(row, headers, "energywh", "energy_wh", "energyactiveimportregister")
    if wh is not None:
        return wh / 1000.0
    return None


def _read_power_kw(row: dict, headers: dict) -> Optional[float]:
    kw = _read_float(row, headers, "powerkw", "power_kw", "chargingkw")
    if kw is not None:
        return kw
    w = _read_float(row, headers, "powerw", "power_w", "poweractiveimport")
    if w is not None:
        return w / 1000.0
    return None


def _read_soc(row: dict, headers: dict) -> Optional[float]:
    """Return SoC as a fraction in [0, 1] regardless of source unit."""
    soc = _read_float(row, headers, "soc", "socpercent", "soc_percent", "stateofcharge")
    if soc is None:
        return None
    # Both 0.85 and 85.0 are plausible inputs; the >1 branch covers
    # percentage encodings, which is the dominant ABB convention.
    return soc / 100.0 if soc > 1.0 else soc


def _parse_timestamp(value: str) -> Optional[datetime]:
    """ISO 8601 with or without a ``Z`` suffix → tz-aware UTC datetime."""
    s = value.strip()
    if not s:
        return None
    # Common variants we accept: '2024-01-15T13:45:00Z',
    # '2024-01-15T13:45:00+00:00', '2024-01-15 13:45:00'.
    s = s.replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
