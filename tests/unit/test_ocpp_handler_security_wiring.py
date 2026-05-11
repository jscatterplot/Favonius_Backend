"""Regression tests for the wiring between ``EnhancedOCPPChargePoint`` and
``SecurityManager``.

Background: ``SecurityManager`` reaches static identity tables
(``charging_stations``, ``station_credentials``) via ``static_auth_client``.
``charging_stations`` lives in Supabase only, so ``static_auth_client`` must
be the ``SupabaseClient`` instance — falling back to ``timescale_client``
(SecurityManager's default) silently breaks Basic Auth because the table
doesn't exist on the Timescale pool.

These tests pin that ``EnhancedOCPPChargePoint.__init__`` accepts
``static_auth_client`` as a keyword argument and forwards it into
``SecurityManager``. Constructing an actual ``EnhancedOCPPChargePoint`` in
unit tests is impractical (its base class drives an OCPP/WebSocket loop), so
we use signature + source introspection.
"""

from __future__ import annotations

import inspect

from src.websocket_handler.ocpp_handler import EnhancedOCPPChargePoint


def test_constructor_accepts_static_auth_client_kwarg() -> None:
    """The constructor must expose ``static_auth_client`` so callers can
    forward the SupabaseClient. Removing this parameter is a regression."""
    sig = inspect.signature(EnhancedOCPPChargePoint.__init__)
    assert "static_auth_client" in sig.parameters
    param = sig.parameters["static_auth_client"]
    # Optional with a default so existing test instantiations keep working.
    assert param.default is None


def test_constructor_forwards_static_auth_client_to_security_manager() -> None:
    """Pin that ``__init__`` actually passes the kwarg into SecurityManager.

    A pure signature check would not catch the case where the parameter is
    accepted but silently dropped on the way to SecurityManager — which is
    exactly the regression that breaks Basic Auth in production.
    """
    src = inspect.getsource(EnhancedOCPPChargePoint.__init__)
    # Tolerant match: any whitespace between '=' and identifier is fine,
    # and we only require the binding to exist somewhere in __init__.
    assert "static_auth_client=static_auth_client" in src.replace(" ", ""), (
        "EnhancedOCPPChargePoint.__init__ must forward static_auth_client "
        "into SecurityManager; otherwise SecurityManager falls back to "
        "timescale_client, which doesn't host charging_stations."
    )
    # Belt-and-braces: SecurityManager must actually appear in the body.
    assert "SecurityManager(" in src
