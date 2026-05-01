"""HTTP Basic Auth verification for OCPP-J 1.6 WebSocket connections.

Per OCPP-J 1.6 Security Profile 1, chargers authenticate the WebSocket
upgrade with HTTP Basic Auth. This module verifies the supplied credentials
against the bcrypt-hashed password stored in ``station_credentials``.

Two invariants must hold for the credential to be accepted:

1. The Basic Auth ``username`` must equal the ``charge_point_id`` advertised in
   the upgrade path. Without this binding, a leaked credential could be used
   to impersonate a different charger by reusing the password.
2. The bcrypt comparison must run against a **stored hash** for that
   ``station_id``. We never accept a row whose ``active`` flag is FALSE.

All failure paths burn a constant amount of bcrypt time so that absence of
a row, an inactive row, and a wrong password are indistinguishable from
the network. Defeats username- and tenant-enumeration timing attacks.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
from typing import Optional

import asyncpg
import bcrypt

logger = logging.getLogger(__name__)


# Pre-computed bcrypt hash so failure paths spend the same CPU as success.
# The plaintext is unguessable; this hash never matches a real password.
_DUMMY_BCRYPT_HASH: bytes = bcrypt.hashpw(b"\x00" * 32, bcrypt.gensalt(rounds=12))


def _decode_basic_auth(header: Optional[str]) -> Optional[tuple[str, str]]:
    """Parse ``Basic <base64(user:pass)>``; return ``(user, pass)`` or None."""
    if not header or len(header) < 6:
        return None
    if header[:6].lower() != "basic ":
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True)
        text = decoded.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in text:
        return None
    user, _, pwd = text.partition(":")
    return user, pwd


async def verify_ocpp_basic_auth(
    auth_header: Optional[str],
    charge_point_id: str,
    pool: asyncpg.Pool,
) -> bool:
    """Verify Basic Auth credentials against ``station_credentials``.

    Returns True iff:
      * Header is well-formed Basic Auth.
      * Username equals the path-supplied ``charge_point_id``.
      * A row exists in ``station_credentials`` for that ``station_id`` with
        ``active = TRUE``.
      * ``bcrypt.checkpw`` succeeds against the stored ``password_hash``.

    On success, ``last_used`` is best-effort updated.
    On any failure, a dummy bcrypt comparison runs to equalize timing.
    """
    parsed = _decode_basic_auth(auth_header)
    if parsed is None:
        await asyncio.to_thread(bcrypt.checkpw, b"x", _DUMMY_BCRYPT_HASH)
        return False

    username, password = parsed
    username_ok = hmac.compare_digest(username, charge_point_id)

    stored_hash: Optional[bytes] = None
    if username_ok:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT password_hash "
                    "FROM station_credentials "
                    "WHERE station_id = $1 AND username = $1 AND active = TRUE",
                    charge_point_id,
                )
        except Exception:
            logger.warning(
                "OCPP auth: DB lookup failed for station_id=%s",
                charge_point_id,
                exc_info=True,
            )
            row = None
        if row and row["password_hash"]:
            raw = row["password_hash"]
            stored_hash = raw.encode("utf-8") if isinstance(raw, str) else raw

    target_hash = stored_hash if stored_hash is not None else _DUMMY_BCRYPT_HASH
    matched = await asyncio.to_thread(
        bcrypt.checkpw,
        password.encode("utf-8"),
        target_hash,
    )
    accepted = bool(username_ok and stored_hash is not None and matched)

    if accepted:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE station_credentials SET last_used = NOW() "
                    "WHERE station_id = $1 AND username = $1 AND active = TRUE",
                    charge_point_id,
                )
        except Exception:
            logger.debug(
                "OCPP auth: last_used update failed for station_id=%s",
                charge_point_id,
                exc_info=True,
            )

    return accepted


__all__ = ["verify_ocpp_basic_auth"]
