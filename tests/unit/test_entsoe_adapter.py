"""Unit tests for ENTSO-E price adapter.

Tests the ENTSO-E Transparency Platform adapter for European electricity pricing.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.adapters.entsoe import ENTSOEAdapter, ENTSOEPrice, get_bidding_zone, is_european_timezone
from src.adapters.entsoe.mappings import (
    COUNTRY_TO_BIDDING_ZONE,
    get_bidding_zone_for_country,
)
from src.adapters.entsoe.prices import _format_entsoe_time, _parse_price_document, _parse_utc_time

# ============ Fixtures ============


@pytest.fixture
def mock_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire = MagicMock(return_value=conn)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    return pool


@pytest.fixture
def entsoe_adapter(mock_pool):
    """ENTSO-E adapter with mocked pool."""
    return ENTSOEAdapter(security_token="test-token", pool=mock_pool)


@pytest.fixture
def sample_xml_hourly():
    """Sample ENTSO-E XML response with hourly resolution."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <mRID>test-doc-1</mRID>
  <type>A44</type>
  <TimeSeries>
    <mRID>1</mRID>
    <currency_Unit.name>EUR</currency_Unit.name>
    <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <Period>
      <timeInterval>
        <start>2026-02-09T23:00Z</start>
        <end>2026-02-10T23:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>45.32</price.amount></Point>
      <Point><position>2</position><price.amount>42.18</price.amount></Point>
      <Point><position>3</position><price.amount>38.50</price.amount></Point>
      <Point><position>4</position><price.amount>35.00</price.amount></Point>
      <Point><position>5</position><price.amount>33.20</price.amount></Point>
      <Point><position>6</position><price.amount>34.80</price.amount></Point>
      <Point><position>7</position><price.amount>40.10</price.amount></Point>
      <Point><position>8</position><price.amount>55.00</price.amount></Point>
      <Point><position>9</position><price.amount>65.30</price.amount></Point>
      <Point><position>10</position><price.amount>70.20</price.amount></Point>
      <Point><position>11</position><price.amount>72.50</price.amount></Point>
      <Point><position>12</position><price.amount>71.00</price.amount></Point>
      <Point><position>13</position><price.amount>68.30</price.amount></Point>
      <Point><position>14</position><price.amount>65.10</price.amount></Point>
      <Point><position>15</position><price.amount>62.80</price.amount></Point>
      <Point><position>16</position><price.amount>60.00</price.amount></Point>
      <Point><position>17</position><price.amount>75.50</price.amount></Point>
      <Point><position>18</position><price.amount>85.20</price.amount></Point>
      <Point><position>19</position><price.amount>90.00</price.amount></Point>
      <Point><position>20</position><price.amount>82.30</price.amount></Point>
      <Point><position>21</position><price.amount>70.10</price.amount></Point>
      <Point><position>22</position><price.amount>58.40</price.amount></Point>
      <Point><position>23</position><price.amount>50.20</price.amount></Point>
      <Point><position>24</position><price.amount>45.00</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


@pytest.fixture
def sample_xml_15min():
    """Sample ENTSO-E XML with 15-minute resolution."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <currency_Unit.name>EUR</currency_Unit.name>
    <Period>
      <timeInterval>
        <start>2026-02-09T23:00Z</start>
        <end>2026-02-10T00:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>10.0</price.amount></Point>
      <Point><position>2</position><price.amount>20.0</price.amount></Point>
      <Point><position>3</position><price.amount>30.0</price.amount></Point>
      <Point><position>4</position><price.amount>40.0</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


@pytest.fixture
def sample_xml_step_curve():
    """Sample ENTSO-E XML with A03 step curve (skipped positions)."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <currency_Unit.name>EUR</currency_Unit.name>
    <Period>
      <timeInterval>
        <start>2026-02-09T23:00Z</start>
        <end>2026-02-10T23:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>50.00</price.amount></Point>
      <Point><position>4</position><price.amount>60.00</price.amount></Point>
      <Point><position>6</position><price.amount>70.00</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


# ============ Mapping Tests ============


def test_is_european_timezone():
    """Test European timezone detection."""
    assert is_european_timezone("Europe/Berlin")
    assert is_european_timezone("Europe/Paris")
    assert is_european_timezone("Europe/London")
    assert is_european_timezone("Europe/Amsterdam")
    assert not is_european_timezone("America/Los_Angeles")
    assert not is_european_timezone("America/New_York")
    assert not is_european_timezone("Asia/Tokyo")
    assert not is_european_timezone("US/Pacific")


def test_get_bidding_zone():
    """Test timezone to bidding zone mapping."""
    assert get_bidding_zone("Europe/Berlin") == "10Y1001A1001A82H"
    assert get_bidding_zone("Europe/Paris") == "10YFR-RTE------C"
    assert get_bidding_zone("Europe/Amsterdam") == "10YNL----------L"
    assert get_bidding_zone("Europe/Madrid") == "10YES-REE------0"
    assert get_bidding_zone("Europe/London") == "10YGB----------A"
    assert get_bidding_zone("America/New_York") is None
    assert get_bidding_zone("Asia/Tokyo") is None


def test_get_bidding_zone_for_country():
    """Test country code to bidding zone mapping."""
    assert get_bidding_zone_for_country("DE") == "10Y1001A1001A82H"
    assert get_bidding_zone_for_country("FR") == "10YFR-RTE------C"
    assert get_bidding_zone_for_country("NL") == "10YNL----------L"
    assert get_bidding_zone_for_country("de") == "10Y1001A1001A82H"  # Case insensitive
    assert get_bidding_zone_for_country("US") is None
    assert get_bidding_zone_for_country("JP") is None


def test_all_major_countries_mapped():
    """Test that all major European countries have mappings."""
    major_countries = [
        "DE",
        "FR",
        "NL",
        "BE",
        "AT",
        "ES",
        "PT",
        "IT",
        "GB",
        "PL",
        "CZ",
        "SK",
        "HU",
        "RO",
        "BG",
        "GR",
        "FI",
        "SE",
        "NO",
        "DK",
        "EE",
        "LV",
        "LT",
        "CH",
        "SI",
        "HR",
    ]
    for cc in major_countries:
        assert cc in COUNTRY_TO_BIDDING_ZONE, f"Missing country: {cc}"


# ============ ENTSOEPrice Tests ============


def test_entsoe_price_creation():
    """Test ENTSOEPrice dataclass creation."""
    price = ENTSOEPrice(
        timestamp=datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc),
        price_eur_mwh=75.50,
        bidding_zone="10Y1001A1001A82H",
    )
    assert price.price_eur_mwh == 75.50
    assert price.currency == "EUR"
    assert price.resolution == "PT60M"
    assert price.bidding_zone == "10Y1001A1001A82H"


def test_entsoe_price_per_kwh():
    """Test price conversion from EUR/MWh to EUR/kWh."""
    price = ENTSOEPrice(
        timestamp=datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc),
        price_eur_mwh=150.0,
        bidding_zone="10YFR-RTE------C",
    )
    assert price.price_per_kwh == 0.15


# ============ Time Formatting Tests ============


def test_format_entsoe_time_utc():
    """Test ENTSO-E time formatting with UTC datetime."""
    dt = datetime(2026, 2, 10, 0, 0, tzinfo=timezone.utc)
    assert _format_entsoe_time(dt) == "202602100000"


def test_format_entsoe_time_naive():
    """Test ENTSO-E time formatting with naive datetime."""
    dt = datetime(2026, 2, 10, 14, 30)
    assert _format_entsoe_time(dt) == "202602101430"


def test_parse_utc_time_z_suffix():
    """Test parsing time string with Z suffix."""
    result = _parse_utc_time("2026-02-09T23:00Z")
    assert result == datetime(2026, 2, 9, 23, 0, tzinfo=timezone.utc)


def test_parse_utc_time_offset():
    """Test parsing time string with explicit offset."""
    result = _parse_utc_time("2026-02-09T23:00+00:00")
    assert result == datetime(2026, 2, 9, 23, 0, tzinfo=timezone.utc)


# ============ XML Parsing Tests ============


def test_parse_hourly_prices(sample_xml_hourly):
    """Test parsing standard 24-hour day-ahead prices."""
    prices = _parse_price_document(sample_xml_hourly, "10Y1001A1001A82H")
    assert len(prices) == 24
    assert prices[0].price_eur_mwh == 45.32
    assert prices[0].currency == "EUR"
    assert prices[0].resolution == "PT60M"
    assert prices[0].bidding_zone == "10Y1001A1001A82H"
    # Check last price
    assert prices[23].price_eur_mwh == 45.00
    # Check timestamps are 1 hour apart
    assert prices[1].timestamp - prices[0].timestamp == timedelta(hours=1)


def test_parse_15min_resolution(sample_xml_15min):
    """Test parsing 15-minute resolution prices."""
    prices = _parse_price_document(sample_xml_15min, "10YNL----------L")
    assert len(prices) == 4
    assert prices[0].resolution == "PT15M"
    assert prices[1].timestamp - prices[0].timestamp == timedelta(minutes=15)
    assert prices[0].price_eur_mwh == 10.0
    assert prices[3].price_eur_mwh == 40.0


def test_parse_step_curve_fills_gaps(sample_xml_step_curve):
    """Test that A03 step curves forward-fill missing positions."""
    prices = _parse_price_document(sample_xml_step_curve, "10YDE-RWENET---I")
    assert len(prices) == 6
    # Position 1: explicitly 50.00
    assert prices[0].price_eur_mwh == 50.00
    # Position 2-3: forward-filled from position 1
    assert prices[1].price_eur_mwh == 50.00
    assert prices[2].price_eur_mwh == 50.00
    # Position 4: explicitly 60.00
    assert prices[3].price_eur_mwh == 60.00
    # Position 5: forward-filled from position 4
    assert prices[4].price_eur_mwh == 60.00
    # Position 6: explicitly 70.00
    assert prices[5].price_eur_mwh == 70.00


def test_parse_empty_xml():
    """Test parsing XML with no price data."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
</Publication_MarketDocument>"""
    prices = _parse_price_document(xml, "10Y1001A1001A82H")
    assert len(prices) == 0


def test_parse_multiple_periods():
    """Test parsing XML with multiple Period elements (DST transition)."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <currency_Unit.name>EUR</currency_Unit.name>
    <Period>
      <timeInterval>
        <start>2026-03-28T23:00Z</start>
        <end>2026-03-29T22:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>50.0</price.amount></Point>
      <Point><position>2</position><price.amount>45.0</price.amount></Point>
    </Period>
    <Period>
      <timeInterval>
        <start>2026-03-29T22:00Z</start>
        <end>2026-03-29T23:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>60.0</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""
    prices = _parse_price_document(xml, "10Y1001A1001A82H")
    assert len(prices) == 3
    # First period
    assert prices[0].price_eur_mwh == 50.0
    assert prices[1].price_eur_mwh == 45.0
    # Second period
    assert prices[2].price_eur_mwh == 60.0


# ============ Adapter Tests ============


@pytest.mark.asyncio
async def test_get_day_ahead_prices_with_zone(entsoe_adapter, sample_xml_hourly):
    """Test fetching prices with explicit bidding zone."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = sample_xml_hourly

    with patch.object(
        entsoe_adapter.client, "get", new_callable=AsyncMock, return_value=mock_response
    ):
        prices = await entsoe_adapter.get_day_ahead_prices(
            start_date=datetime(2026, 2, 9, 23, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 2, 10, 23, 0, tzinfo=timezone.utc),
            bidding_zone="10Y1001A1001A82H",
        )

    assert len(prices) == 24
    assert prices[0].price_eur_mwh == 45.32


@pytest.mark.asyncio
async def test_get_day_ahead_prices_with_timezone(entsoe_adapter, sample_xml_hourly):
    """Test fetching prices with depot timezone (auto bidding zone detection)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = sample_xml_hourly

    with patch.object(
        entsoe_adapter.client, "get", new_callable=AsyncMock, return_value=mock_response
    ):
        prices = await entsoe_adapter.get_day_ahead_prices(
            start_date=datetime(2026, 2, 9, 23, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 2, 10, 23, 0, tzinfo=timezone.utc),
            depot_timezone="Europe/Berlin",
        )

    assert len(prices) == 24


@pytest.mark.asyncio
async def test_get_day_ahead_prices_no_zone_raises(entsoe_adapter):
    """Test that missing bidding zone raises ValueError."""
    with pytest.raises(ValueError, match="Cannot determine bidding zone"):
        await entsoe_adapter.get_day_ahead_prices(
            start_date=datetime(2026, 2, 9, 23, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 2, 10, 23, 0, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_get_day_ahead_prices_no_token():
    """Test that missing security token raises ValueError."""
    adapter = ENTSOEAdapter(security_token="")
    with pytest.raises(ValueError, match="security token not configured"):
        await adapter.get_day_ahead_prices(
            start_date=datetime(2026, 2, 9, 23, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 2, 10, 23, 0, tzinfo=timezone.utc),
            bidding_zone="10Y1001A1001A82H",
        )


@pytest.mark.asyncio
async def test_get_day_ahead_prices_rate_limit(entsoe_adapter):
    """Test handling of 429 rate limit response."""
    mock_response = MagicMock()
    mock_response.status_code = 429

    with patch.object(
        entsoe_adapter.client, "get", new_callable=AsyncMock, return_value=mock_response
    ):
        with pytest.raises(RuntimeError, match="rate limit"):
            await entsoe_adapter.get_day_ahead_prices(
                start_date=datetime(2026, 2, 9, 23, 0, tzinfo=timezone.utc),
                end_date=datetime(2026, 2, 10, 23, 0, tzinfo=timezone.utc),
                bidding_zone="10Y1001A1001A82H",
            )


@pytest.mark.asyncio
async def test_store_prices_to_db(entsoe_adapter, mock_pool):
    """Test storing ENTSO-E prices to database."""
    depot_id = uuid4()
    prices = [
        ENTSOEPrice(
            timestamp=datetime(2026, 2, 10, h, 0, tzinfo=timezone.utc),
            price_eur_mwh=50.0 + h,
            bidding_zone="10Y1001A1001A82H",
        )
        for h in range(5)
    ]

    stored = await entsoe_adapter.store_prices_to_db(prices, depot_id)

    assert stored == 5
    assert mock_pool.acquire.return_value.execute.call_count == 5


@pytest.mark.asyncio
async def test_store_prices_to_db_no_pool():
    """Test storing without pool raises error."""
    adapter = ENTSOEAdapter(security_token="test-token")
    depot_id = uuid4()
    prices = [
        ENTSOEPrice(
            timestamp=datetime(2026, 2, 10, 0, 0, tzinfo=timezone.utc),
            price_eur_mwh=50.0,
            bidding_zone="10Y1001A1001A82H",
        )
    ]

    with pytest.raises(RuntimeError, match="Database pool not configured"):
        await adapter.store_prices_to_db(prices, depot_id)


@pytest.mark.asyncio
async def test_store_prices_empty_list(entsoe_adapter):
    """Test storing empty price list returns 0."""
    stored = await entsoe_adapter.store_prices_to_db([], uuid4())
    assert stored == 0


def test_price_conversion_eur_mwh_to_kwh():
    """Test EUR/MWh to EUR/kWh conversion."""
    price = ENTSOEPrice(
        timestamp=datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc),
        price_eur_mwh=250.0,
        bidding_zone="10YFR-RTE------C",
    )
    assert price.price_per_kwh == 0.25
