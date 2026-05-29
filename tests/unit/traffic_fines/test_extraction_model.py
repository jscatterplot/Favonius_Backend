"""Unit tests for the TrafficFineExtraction structured-output model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.traffic_fines.models import TrafficFineExtraction


def test_all_fields_optional_defaults():
    fine = TrafficFineExtraction()
    assert fine.is_traffic_fine is True
    assert fine.fine_reference is None
    assert fine.full_amount is None
    assert fine.iban is None


def test_forbids_extra_fields():
    with pytest.raises(ValidationError):
        TrafficFineExtraction(unexpected="x")  # type: ignore[call-arg]


def test_json_schema_usable_as_tool_input_schema():
    schema = TrafficFineExtraction.model_json_schema()
    assert schema["type"] == "object"
    for field in ("early_payment_deadline", "iban", "issuing_authority", "full_amount"):
        assert field in schema["properties"]


def test_roundtrip_from_dict():
    payload = {
        "is_traffic_fine": True,
        "issuing_authority": "Bussgeldstelle Berlin",
        "issuing_country": "Germany",
        "fine_reference": "B-123",
        "currency": "EUR",
        "full_amount": 100.0,
        "early_payment_amount": 80.0,
        "early_payment_deadline": "2026-06-01",
        "iban": "DE89370400440532013000",
    }
    fine = TrafficFineExtraction.model_validate(payload)
    assert fine.issuing_country == "Germany"
    assert fine.full_amount == 100.0
