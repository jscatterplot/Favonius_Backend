"""Tests for trusted-proxy-aware parsing in :mod:`src.security.forwarded_ip`.

These tests cover the trust-aware right-to-left walk that prevents an
attacker from spoofing their origin country by prepending an
``X-Forwarded-For`` value when the edge proxy appends rather than
replaces. Pure-format helpers (``normalize_forwarded_ip``,
``parse_ip_networks``) are exercised in ``tests/security/test_geo_blocking.py``.
"""

from __future__ import annotations

import ipaddress
import logging

from src.security.forwarded_ip import extract_forwarded_ip, parse_ip_networks


def _net(cidr: str):
    return ipaddress.ip_network(cidr, strict=False)


class _Headers:
    """Minimal case-insensitive header dict mirroring Starlette's interface."""

    def __init__(self, raw: dict) -> None:
        self._raw = {k.lower(): v for k, v in raw.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._raw.get(key.lower(), default)


class TestLegacyBehaviour:
    """No trust config — preserve leftmost-wins for callers that didn't opt in."""

    def test_no_trust_args_returns_leftmost(self):
        headers = _Headers({"X-Forwarded-For": "8.8.8.8, 1.1.1.1"})
        assert extract_forwarded_ip(headers) == "8.8.8.8"

    def test_no_trust_args_returns_xff_when_forwarded_absent(self):
        headers = _Headers({"X-Forwarded-For": "1.1.1.1"})
        assert extract_forwarded_ip(headers) == "1.1.1.1"

    def test_falls_back_to_x_real_ip(self):
        headers = _Headers({"X-Real-IP": "1.1.1.1"})
        assert extract_forwarded_ip(headers) == "1.1.1.1"

    def test_returns_none_when_no_headers(self):
        assert extract_forwarded_ip(_Headers({})) is None

    def test_returns_none_when_object_lacks_get(self):
        assert extract_forwarded_ip(object()) is None


class TestImplicitPrivateTrust:
    """``trust_implicit_private=True`` implicitly trusts only RFC 6598 CGNAT.

    Private / loopback / link-local IPs may be real client addresses on
    on-prem or container networks and are NOT auto-skipped — those
    deployments must declare their proxy CIDR via ``trusted_networks``.
    """

    def test_cgnat_is_implicitly_skipped(self):
        headers = _Headers({"X-Forwarded-For": "18.196.90.141, 100.64.0.2"})
        assert extract_forwarded_ip(headers, trust_implicit_private=True) == "18.196.90.141"

    def test_does_not_implicitly_skip_private(self):
        """Private clients (on-prem / VPN) are returned, not skipped as proxies."""
        headers = _Headers({"X-Forwarded-For": "18.196.90.141, 10.0.0.5"})
        # Right-to-left: 10.0.0.5 is private but NOT CGNAT — it is the client.
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "10.0.0.5"
        )

    def test_does_not_implicitly_skip_loopback(self):
        headers = _Headers({"X-Forwarded-For": "18.196.90.141, 127.0.0.1"})
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "127.0.0.1"
        )

    def test_does_not_implicitly_skip_link_local(self):
        headers = _Headers({"X-Forwarded-For": "18.196.90.141, fe80::1"})
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "fe80::1"
        )

    def test_skips_explicitly_trusted_private(self):
        """Private LB declared in ``trusted_networks`` IS skipped."""
        headers = _Headers({"X-Forwarded-For": "18.196.90.141, 10.0.0.5"})
        assert (
            extract_forwarded_ip(
                headers,
                trusted_networks=(_net("10.0.0.0/8"),),
                trust_implicit_private=True,
            )
            == "18.196.90.141"
        )

    def test_falls_back_to_leftmost_when_all_trusted(self):
        """Internal-only chain: nothing non-trusted to return — preserve the claim."""
        headers = _Headers({"X-Forwarded-For": "100.64.0.1, 100.64.0.2"})
        # Both CGNAT → both implicitly trusted → fall back to leftmost.
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "100.64.0.1"
        )


class TestSpoofResistance:
    """The point of this audit fix."""

    def test_prepended_spoof_is_ignored(self):
        """Classic XFF spoof: attacker prepends a country IP, edge appends real IP.

        ``X-Forwarded-For: 8.8.8.8, <real-attacker-ip>`` with Railway's CGNAT
        appended → walking right-to-left, the real attacker IP (public) is
        the first non-trusted entry. The spoof never wins.
        """
        headers = _Headers(
            {"X-Forwarded-For": "8.8.8.8, 93.184.216.34, 100.64.0.2"}
        )
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "93.184.216.34"
        )

    def test_long_prepended_spoof_chain_ignored(self):
        headers = _Headers(
            {
                "X-Forwarded-For": (
                    "1.2.3.4, 5.6.7.8, 9.10.11.12, 13.14.15.16, "
                    "93.184.216.34, 100.64.0.2"
                )
            }
        )
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "93.184.216.34"
        )

    def test_explicit_trusted_network_skipped(self):
        """On-prem deployment with a public-IP load balancer in front."""
        lb = (_net("198.51.100.10/32"),)
        headers = _Headers(
            {"X-Forwarded-For": "8.8.8.8, 93.184.216.34, 198.51.100.10"}
        )
        assert (
            extract_forwarded_ip(
                headers,
                trusted_networks=lb,
                trust_implicit_private=True,
            )
            == "93.184.216.34"
        )

    def test_explicit_only_no_implicit(self):
        """Paranoid mode: implicit private trust disabled; only explicit CIDRs skipped."""
        proxies = (_net("203.0.113.10/32"),)
        headers = _Headers({"X-Forwarded-For": "8.8.8.8, 203.0.113.10"})
        assert (
            extract_forwarded_ip(
                headers,
                trusted_networks=proxies,
                trust_implicit_private=False,
            )
            == "8.8.8.8"
        )

    def test_spoofed_leading_entries_unreached(self):
        """Right-to-left walk stops at the first non-trusted entry — spoofs at left are ignored."""
        headers = _Headers(
            {"X-Forwarded-For": "10.0.0.99, 93.184.216.34, 100.64.0.2"}
        )
        assert (
            extract_forwarded_ip(
                headers,
                trusted_networks=proxies,
                trust_implicit_private=True,
            )
            == "93.184.216.34"
        )

    def test_private_client_behind_private_lb_is_not_spoofed(self):
        """Codex P1: real client on private network, attacker spoofs public IP.

        Scenario: on-prem deployment where clients live on 10.0.0.0/8 and
        the LB also has a private IP. Attacker sends
        ``X-Forwarded-For: 8.8.8.8`` and the LB appends ``10.1.2.3`` (real
        client). Earlier behaviour: walker skipped 10.1.2.3 as a "private
        proxy" and returned the spoofed 8.8.8.8. Fixed behaviour: 10.1.2.3
        is treated as the client (not an implicit proxy), spoof rejected.
        """
        headers = _Headers({"X-Forwarded-For": "8.8.8.8, 10.1.2.3"})
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "10.1.2.3"
        )


class TestForwardedHeaderRfc7239:
    """The same trust-walk rules apply to RFC 7239 ``Forwarded``."""

    def test_forwarded_takes_precedence_over_xff(self):
        headers = _Headers(
            {
                "Forwarded": 'for="1.1.1.1";proto=https',
                "X-Forwarded-For": "198.51.100.7",
            }
        )
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "1.1.1.1"
        )

    def test_forwarded_chain_walk_skips_trusted(self):
        headers = _Headers(
            {"Forwarded": 'for="8.8.8.8", for="93.184.216.34", for="100.64.0.2"'}
        )
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "93.184.216.34"
        )


class TestInvalidEntries:
    """Garbage entries in the chain don't help an attacker."""

    def test_invalid_entry_is_dropped_not_returned(self):
        headers = _Headers({"X-Forwarded-For": "junk, 93.184.216.34, 100.64.0.2"})
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "93.184.216.34"
        )

    def test_all_invalid_falls_through_to_xreal_ip(self):
        headers = _Headers(
            {"X-Forwarded-For": "junk, also-junk", "X-Real-IP": "1.1.1.1"}
        )
        assert (
            extract_forwarded_ip(headers, trust_implicit_private=True)
            == "1.1.1.1"
        )


class TestParseIpNetworks:
    def test_skips_invalid_entries(self):
        nets = parse_ip_networks(
            "10.0.0.0/8, junk, 192.168.0.0/16, ",
            logger=logging.getLogger("test"),
            env_var_name="UNIT_TEST",
        )
        assert len(nets) == 2
        assert str(nets[0]) == "10.0.0.0/8"
        assert str(nets[1]) == "192.168.0.0/16"

    def test_empty_string_returns_empty_list(self):
        nets = parse_ip_networks(
            "",
            logger=logging.getLogger("test"),
            env_var_name="UNIT_TEST",
        )
        assert nets == []
