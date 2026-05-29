"""EV vs diesel "price per kilometre" comparison.

Combines four already-built pieces into the numbers the CEO report shows:

- EV **cost + energy** per vehicle — reuses :func:`reports.compute_energy_totals`
  over that vehicle's ``charging_sessions`` (so the exact cost rule — real
  ``cost_total`` summed, ``under_cap_rate`` fallback for missing-cost rows — is
  shared, not re-derived).
- **Distance** per vehicle — :func:`distance.compute_distances_for_depot`
  (odometer deltas).
- **Diesel €/L** — :func:`queries.fetch_or_pull_diesel_price` for the depot's
  country (the ex-tax / wholesale figure).
- **Diesel L/100km** per vehicle type — :func:`queries.resolve_fuel_baselines`.

Design:

- The pure core :func:`build_fleet_comparison` takes already-fetched per-vehicle
  inputs and returns a :class:`FleetKmResult` — no DB, fully unit-testable.
- :func:`compute_cost_per_km` is the thin DB orchestrator the report handler
  calls.

Honesty rules (no zero-faking):
- A vehicle with unknown/zero distance, or no diesel price, is **excluded** from
  the fleet €/km and % figures (its energy/cost is still reported for
  transparency), and counted in ``unpriceable_vehicle_count``.
- Fleet €/km is ``Σcost / Σdistance`` over priceable vehicles — *not* a mean of
  per-vehicle ratios (which would mis-weight short vs long runners).

Currency caveat: diesel prices are EUR/L; EV ``cost_total`` is in the depot's
currency. For a non-EUR depot the comparison mixes currencies — surfaced via
``currency_mismatch`` so the report can flag it rather than silently mislead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)

DIESEL_CURRENCY = "EUR"


@dataclass(frozen=True)
class VehicleInput:
    """Pre-fetched inputs for one vehicle (feeds the pure core)."""

    vehicle_id: str
    vehicle_type: Optional[str]
    distance_km: Optional[float]
    ev_energy_kwh: float
    ev_cost_eur: Optional[float]
    diesel_l_per_100km: float
    baseline_is_default: bool = False


@dataclass(frozen=True)
class VehicleKmResult:
    """Per-vehicle comparison outcome."""

    vehicle_id: str
    vehicle_type: Optional[str]
    distance_km: Optional[float]
    ev_energy_kwh: float
    ev_cost_eur: Optional[float]
    ev_eur_per_km: Optional[float]
    diesel_l_per_100km: float
    diesel_litres: Optional[float]
    diesel_cost_eur: Optional[float]
    diesel_eur_per_km: Optional[float]
    pct_difference: Optional[float]  # (diesel - ev)/diesel * 100; +ve ⇒ EV cheaper
    priceable: bool
    baseline_is_default: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class VehicleTypeKmAgg:
    """Per-vehicle-type rollup (priceable vehicles only)."""

    vehicle_type: str
    vehicle_count: int
    distance_km: float
    ev_energy_kwh: float
    ev_cost_eur: float
    ev_eur_per_km: Optional[float]
    diesel_litres: float
    diesel_cost_eur: float
    diesel_eur_per_km: Optional[float]
    pct_difference: Optional[float]


@dataclass(frozen=True)
class FleetKmResult:
    """Full EV-vs-diesel comparison for a depot over a period."""

    period_start: date
    period_end: date
    currency: str  # the EV-cost currency (depot currency)
    diesel_currency: str  # always EUR
    per_vehicle: list[VehicleKmResult]
    by_vehicle_type: list[VehicleTypeKmAgg]
    total_distance_km: float
    total_ev_cost_eur: float
    total_diesel_cost_eur: float
    fleet_ev_eur_per_km: Optional[float]
    fleet_diesel_eur_per_km: Optional[float]
    fleet_pct_difference: Optional[float]
    diesel_price_eur_per_l: Optional[float]
    diesel_region: Optional[str]
    priceable_vehicle_count: int
    unpriceable_vehicle_count: int
    currency_mismatch: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


def _safe_div(numer: float, denom: float) -> Optional[float]:
    """Return ``numer/denom`` or ``None`` when ``denom`` is zero."""
    return (numer / denom) if denom else None


def _pct_savings(ev: Optional[float], diesel: Optional[float]) -> Optional[float]:
    """% the EV is cheaper than diesel per km; ``None`` if not comparable."""
    if ev is None or diesel is None or diesel == 0:
        return None
    return (diesel - ev) / diesel * 100.0


def _evaluate_vehicle(vi: VehicleInput, diesel_price_eur_per_l: Optional[float]) -> VehicleKmResult:
    """Compute one vehicle's row (pure)."""
    notes: list[str] = []
    distance = vi.distance_km

    # EV €/km.
    ev_eur_per_km: Optional[float] = None
    if distance is None:
        notes.append("no_distance")
    elif distance == 0:
        notes.append("zero_distance")
    elif vi.ev_cost_eur is not None:
        ev_eur_per_km = vi.ev_cost_eur / distance

    # Diesel side.
    diesel_litres: Optional[float] = None
    diesel_cost: Optional[float] = None
    diesel_eur_per_km: Optional[float] = None
    if diesel_price_eur_per_l is None:
        notes.append("no_diesel_price")
    elif distance is not None and distance > 0:
        diesel_litres = distance * vi.diesel_l_per_100km / 100.0
        diesel_cost = diesel_litres * diesel_price_eur_per_l
        diesel_eur_per_km = diesel_cost / distance

    if vi.baseline_is_default:
        notes.append("baseline_default")

    priceable = ev_eur_per_km is not None and diesel_eur_per_km is not None
    pct = _pct_savings(ev_eur_per_km, diesel_eur_per_km) if priceable else None

    return VehicleKmResult(
        vehicle_id=vi.vehicle_id,
        vehicle_type=vi.vehicle_type,
        distance_km=distance,
        ev_energy_kwh=vi.ev_energy_kwh,
        ev_cost_eur=vi.ev_cost_eur,
        ev_eur_per_km=ev_eur_per_km,
        diesel_l_per_100km=vi.diesel_l_per_100km,
        diesel_litres=diesel_litres,
        diesel_cost_eur=diesel_cost,
        diesel_eur_per_km=diesel_eur_per_km,
        pct_difference=pct,
        priceable=priceable,
        baseline_is_default=vi.baseline_is_default,
        notes=tuple(notes),
    )


def _aggregate_by_type(priceable: list[VehicleKmResult]) -> list[VehicleTypeKmAgg]:
    """Roll priceable vehicles up per vehicle_type (Σ/Σ weighting)."""
    buckets: dict[str, list[VehicleKmResult]] = {}
    for r in priceable:
        key = r.vehicle_type or "unknown"
        buckets.setdefault(key, []).append(r)

    out: list[VehicleTypeKmAgg] = []
    for vtype, rows in sorted(buckets.items()):
        dist = sum(r.distance_km or 0.0 for r in rows)
        ev_energy = sum(r.ev_energy_kwh for r in rows)
        ev_cost = sum(r.ev_cost_eur or 0.0 for r in rows)
        d_litres = sum(r.diesel_litres or 0.0 for r in rows)
        d_cost = sum(r.diesel_cost_eur or 0.0 for r in rows)
        ev_per_km = _safe_div(ev_cost, dist)
        d_per_km = _safe_div(d_cost, dist)
        out.append(
            VehicleTypeKmAgg(
                vehicle_type=vtype,
                vehicle_count=len(rows),
                distance_km=dist,
                ev_energy_kwh=ev_energy,
                ev_cost_eur=ev_cost,
                ev_eur_per_km=ev_per_km,
                diesel_litres=d_litres,
                diesel_cost_eur=d_cost,
                diesel_eur_per_km=d_per_km,
                pct_difference=_pct_savings(ev_per_km, d_per_km),
            )
        )
    return out


def build_fleet_comparison(
    vehicles: list[VehicleInput],
    *,
    diesel_price_eur_per_l: Optional[float],
    diesel_region: Optional[str],
    currency: str,
    period_start: date,
    period_end: date,
) -> FleetKmResult:
    """Pure core: per-vehicle inputs → :class:`FleetKmResult`.

    Fleet €/km is computed as Σcost / Σdistance over **priceable** vehicles
    (those with both a known positive distance and a diesel price), so a vehicle
    we can't measure neither inflates nor deflates the headline figure.
    """
    per_vehicle = [_evaluate_vehicle(vi, diesel_price_eur_per_l) for vi in vehicles]
    priceable = [r for r in per_vehicle if r.priceable]

    total_distance = sum(r.distance_km or 0.0 for r in priceable)
    total_ev_cost = sum(r.ev_cost_eur or 0.0 for r in priceable)
    total_diesel_cost = sum(r.diesel_cost_eur or 0.0 for r in priceable)

    fleet_ev = _safe_div(total_ev_cost, total_distance)
    fleet_diesel = _safe_div(total_diesel_cost, total_distance)
    fleet_pct = _pct_savings(fleet_ev, fleet_diesel)

    currency_mismatch = currency.upper() != DIESEL_CURRENCY
    notes: list[str] = []
    if currency_mismatch:
        notes.append(
            f"ev_cost_currency={currency} differs from diesel_currency={DIESEL_CURRENCY}; "
            "the per-km comparison mixes currencies"
        )
    if diesel_price_eur_per_l is None:
        notes.append("no_diesel_price_for_region")

    return FleetKmResult(
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        diesel_currency=DIESEL_CURRENCY,
        per_vehicle=per_vehicle,
        by_vehicle_type=_aggregate_by_type(priceable),
        total_distance_km=total_distance,
        total_ev_cost_eur=total_ev_cost,
        total_diesel_cost_eur=total_diesel_cost,
        fleet_ev_eur_per_km=fleet_ev,
        fleet_diesel_eur_per_km=fleet_diesel,
        fleet_pct_difference=fleet_pct,
        diesel_price_eur_per_l=diesel_price_eur_per_l,
        diesel_region=diesel_region,
        priceable_vehicle_count=len(priceable),
        unpriceable_vehicle_count=len(per_vehicle) - len(priceable),
        currency_mismatch=currency_mismatch,
        notes=tuple(notes),
    )


# ── DB orchestrator ───────────────────────────────────────────────────────────


async def compute_cost_per_km(
    pools: Any,
    *,
    depot_id: str,
    organization_id: str,
    period_start: date,
    period_end: date,
    timezone_name: str,
    currency: str,
    under_cap_rate: Optional[float],
    session_rows_by_vehicle: dict[str, list[Any]],
    diesel_source: Optional[str] = None,
) -> FleetKmResult:
    """Fetch distance + diesel price + baselines and build the comparison.

    ``session_rows_by_vehicle`` is ``{vehicle_id: [SessionRow, ...]}`` already
    fetched by the caller (the report handler owns the session query + charger
    mapping); this keeps that DB-shape concern in one place and lets the math be
    tested with plain inputs. EV cost/energy per vehicle is computed here via
    :func:`reports.compute_energy_totals` so the cost rule stays shared.
    """
    import asyncio
    from datetime import datetime, timezone

    from ...api.reports import compute_energy_totals
    from ...db.queries import (
        fetch_or_pull_diesel_price,
        resolve_country_code,
        resolve_fuel_baselines,
    )
    from ...monitoring.metrics import (
        COST_PER_KM_COMPUTED,
        COST_PER_KM_DURATION,
        COST_PER_KM_UNPRICEABLE_VEHICLES,
    )
    from ..billing.distance import compute_distances_for_depot

    loop = asyncio.get_event_loop()
    started = loop.time()

    # 1. Vehicle identities (id + type) for the depot, scoped to the org.
    async with pools.static.acquire() as conn:
        veh_rows = await conn.fetch(
            """
            SELECT id::text AS vehicle_id, vehicle_type
            FROM vehicles
            WHERE site_id = $1::uuid
            """,
            depot_id,
        )
        vehicle_types = {r["vehicle_id"]: r["vehicle_type"] for r in veh_rows}
        country = await resolve_country_code(conn, depot_id)
        baselines = await resolve_fuel_baselines(
            conn, depot_id=depot_id, organization_id=organization_id
        )

    vehicle_ids = list(vehicle_types.keys())

    # 2. Distance per vehicle (odometer deltas over the period, in UTC bounds).
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    start_utc = (
        datetime.combine(period_start, datetime.min.time())
        .replace(tzinfo=tz)
        .astimezone(timezone.utc)
    )
    from datetime import timedelta

    end_utc = (
        datetime.combine(period_end + timedelta(days=1), datetime.min.time())
        .replace(tzinfo=tz)
        .astimezone(timezone.utc)
    )
    distances = await compute_distances_for_depot(
        pools.ts, vehicle_ids=vehicle_ids, start=start_utc, end=end_utc
    )

    # 3. Diesel €/L for the depot's country (ex-tax wholesale).
    diesel_price: Optional[float] = None
    if country:
        async with pools.ts.acquire() as conn:
            diesel_price = await fetch_or_pull_diesel_price(
                conn, country, end_utc, source=diesel_source
            )

    # 4. EV cost/energy per vehicle (shared cost rule) → VehicleInput list.
    default_baseline = baselines.get("__default__")
    inputs: list[VehicleInput] = []
    for vid in vehicle_ids:
        rows = session_rows_by_vehicle.get(vid, [])
        totals = compute_energy_totals(
            rows,
            timezone=timezone_name,
            under_cap_rate=under_cap_rate,
            currency=currency,
            from_date=period_start,
            to_date=period_end,
        )
        ev_energy = float(totals["energy_kwh"])
        # cost.amount is 0.0 when there are no sessions; treat "no sessions" as
        # no EV cost (None) so a vehicle with zero charging isn't shown as €0/km.
        ev_cost = float(totals["cost"]["amount"]) if totals["session_count"] > 0 else None
        vtype = vehicle_types.get(vid)
        baseline = baselines.get(vtype) if vtype else None
        baseline_is_default = baseline is None
        l_per_100 = baseline if baseline is not None else default_baseline
        inputs.append(
            VehicleInput(
                vehicle_id=vid,
                vehicle_type=vtype,
                distance_km=distances[vid].distance_km if vid in distances else None,
                ev_energy_kwh=ev_energy,
                ev_cost_eur=ev_cost,
                diesel_l_per_100km=float(l_per_100),
                baseline_is_default=baseline_is_default,
            )
        )

    result = build_fleet_comparison(
        inputs,
        diesel_price_eur_per_l=diesel_price,
        diesel_region=country,
        currency=currency,
        period_start=period_start,
        period_end=period_end,
    )

    # Metrics.
    COST_PER_KM_DURATION.observe(loop.time() - started)
    if diesel_price is None:
        COST_PER_KM_COMPUTED.labels(outcome="no_diesel_price").inc()
    elif result.priceable_vehicle_count == 0:
        COST_PER_KM_COMPUTED.labels(outcome="no_distance").inc()
    else:
        COST_PER_KM_COMPUTED.labels(outcome="ok").inc()
    for r in result.per_vehicle:
        if "no_distance" in r.notes:
            COST_PER_KM_UNPRICEABLE_VEHICLES.labels(reason="no_distance").inc()
        elif "zero_distance" in r.notes:
            COST_PER_KM_UNPRICEABLE_VEHICLES.labels(reason="zero_distance").inc()
        elif "no_diesel_price" in r.notes:
            COST_PER_KM_UNPRICEABLE_VEHICLES.labels(reason="no_diesel_price").inc()

    return result
