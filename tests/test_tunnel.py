"""Tunnel supervisor log parsing.

This path only executes when cloudflared actually starts, so it never ran
locally while the source IP was rate-limited — and shipped with a bytes-pattern
applied to a decoded string, which took the tunnel down until the next retry.
Hence these tests: the parser must be exercised without a live tunnel.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "live_call_tunnel_supervisor", Path(__file__).parent.parent / "tunnel_supervisor.py")
ts = importlib.util.module_from_spec(_spec)
sys.modules["live_call_tunnel_supervisor"] = ts
_spec.loader.exec_module(ts)

REAL_LOG = """2026-08-09T08:05:31Z INF Thank you for trying Cloudflare Tunnel.
2026-08-09T08:05:31Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-08-09T08:05:33Z INF +--------------------------------------------------------+
2026-08-09T08:05:33Z INF |  https://dental-flows-query-amplifier.trycloudflare.com |
2026-08-09T08:05:33Z INF +--------------------------------------------------------+
2026-08-09T08:05:33Z INF Generated Connector ID: aef8e443-1b83-4059-9a7e-db0aba7a32a7
2026-08-09T08:05:33Z INF Starting metrics server on 127.0.0.1:20243/metrics
2026-08-09T08:05:34Z INF Registered tunnel connection connIndex=0
"""


def test_parses_metrics_address_and_connector_id():
    addr, cid = ts.parse_tunnel_details(REAL_LOG)
    assert addr == "127.0.0.1:20243"
    assert cid == "aef8e443-1b83-4059-9a7e-db0aba7a32a7"


def test_parser_takes_a_string_not_bytes():
    """The log helper returns str; a bytes pattern here raised TypeError and
    cost a live tunnel."""
    assert isinstance(ts.parse_tunnel_details(REAL_LOG)[0], str)
    assert ts.METRICS_RE.pattern.__class__ is str
    assert ts.CONNECTOR_RE.pattern.__class__ is str


def test_missing_details_degrade_to_empty_not_an_exception():
    assert ts.parse_tunnel_details("") == ("", "")
    assert ts.parse_tunnel_details("no metrics line here") == ("", "")


def test_rate_limit_markers_recognised():
    """Cloudflare answers 429/1015 as HTML, not JSON, so cloudflared reports a
    JSON parse error — the markers must match what it actually prints."""
    sample = ('failed to unmarshal quick Tunnel: invalid character \'e\' '
              'looking for beginning of value status_code="429 Too Many Requests"')
    assert any(m in sample for m in ts.RATE_LIMIT_MARKERS)
    assert any(m in "ERR Error unmarshaling QuickTunnel response: error code: 1015"
               for m in ts.RATE_LIMIT_MARKERS)


def test_url_pattern_still_matches_bytes():
    """URL_RE is applied to raw bytes from the log file — that one must stay
    bytes."""
    assert ts.URL_RE.pattern.__class__ is bytes
    assert ts.URL_RE.search(REAL_LOG.encode()) is not None
