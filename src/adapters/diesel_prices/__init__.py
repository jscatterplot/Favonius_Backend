"""Diesel wholesale-price adapter — source for the EV-vs-diesel comparison.

A background poller (:func:`run_diesel_poll_loop`) fetches per-country wholesale
diesel prices from a configurable free source (EU Weekly Oil Bulletin by
default; fuel-prices.eu / Tankerkönig via ``DIESEL_PRICE_SOURCE``) into the
``diesel_prices`` hypertable (migration 047), which
``src/db/queries.py::fetch_or_pull_diesel_price`` reads when computing the
EV-vs-diesel cost-per-km report (``ev_vs_diesel_tco``).

Public surface:

- :class:`DieselPriceClient` / :class:`DieselPriceClientError` — REST client.
- :class:`DieselPrice`, :func:`parse_diesel_record`, :func:`normalize_country` —
  pure mapping helpers.
- :class:`DieselPriceAdapter` — fetch + store + cache read.
- :func:`run_diesel_poll_loop`, :func:`poll_once` — the live feed.
"""

from .adapter import DieselPriceAdapter
from .client import DieselPriceClient, DieselPriceClientError, configured_source
from .mapping import DieselPrice, normalize_country, parse_diesel_record
from .poller import poll_once, run_diesel_poll_loop

__all__ = [
    "DieselPriceAdapter",
    "DieselPriceClient",
    "DieselPriceClientError",
    "configured_source",
    "DieselPrice",
    "normalize_country",
    "parse_diesel_record",
    "poll_once",
    "run_diesel_poll_loop",
]
