"""Test fixtures for Favonius tests.

This module provides realistic test fixtures for Phase 5 and 6 testing.
"""

from tests.fixtures.realistic_depot import (
    DEPOT_ID,
    VEHICLE_IDS,
    CHARGER_IDS,
    realistic_depot_config,
    realistic_depot_state,
    state_with_incoming_vehicle,
)

__all__ = [
    'DEPOT_ID',
    'VEHICLE_IDS',
    'CHARGER_IDS',
    'realistic_depot_config',
    'realistic_depot_state',
    'state_with_incoming_vehicle',
]
