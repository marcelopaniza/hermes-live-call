#!/usr/bin/env python3
"""Keep one Cloudflare quick tunnel pointed at the live_call room port.

Why a supervised, always-on tunnel instead of one per call:
  * a tunnel spawned by an agent turn dies with that turn — the link the user
    was just handed goes dead (observed on the live box, HTTP 1033);
  * quick-tunnel DNS takes seconds to propagate, so minting per call made
    ``call_start`` slow AND racy;
  * one long-lived tunnel means links work the instant they are minted.

The public hostname changes whenever this process restarts, so it is written
to a state file that the plugin re-reads on every call. Modeled on this box's
existing ``line_tunnel_supervisor.py``.

State file: ``$LIVE_CALL_STATE_DIR/tunnel_url.txt`` (default
``$HERMES_HOME/workspace/live_calls``).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
STATE_DIR = Path(os.environ.get("LIVE_CALL_STATE_DIR", HERMES_HOME / "workspace" / "live_calls"))
URL_FILE = STATE_DIR / "tunnel_url.txt"
LOG_FILE = STATE_DIR / "tunnel.log"
BIND = os.environ.get("LIVE_CALL_BIND", "127.0.0.1:8199")
# Health MUST be judged at cloudflared's own metrics endpoint, not by fetching
# through the tunnel: between calls there is no room listening, so the edge
# legitimately errors while the tunnel is perfectly healthy. Recycling on that
# minted a new tunnel every idle minute — which is what gets the source IP
# rate-limited.
#
# The port is DISCOVERED from cloudflared's log rather than fixed: other
# cloudflared instances on the same host (e.g. another tunnel service) claim
# ports in the same range, and pinning one would both collide with them and let
# us read THEIR readiness and call our own dead tunnel healthy.
METRICS_RE = re.compile(r"metrics server on (127\.0\.0\.1:\d+)")
CONNECTOR_RE = re.compile(r"Generated Connector ID: ([0-9a-fA-F-]{36})")
URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
STARTUP_TIMEOUT_S = 60
CHECK_INTERVAL_S = 15
# Cloudflare rate-limits quick tunnels per source IP (HTTP 429 / error 1015).
# Retrying every couple of minutes prolongs the block, so back off hard.
RATE_LIMIT_BACKOFF_S = 20 * 60
MAX_BACKOFF_S = 30 * 60
RATE_LIMIT_MARKERS = ("429 Too Many Requests", "error code: 1015", "Too Many Requests")

_proc: subprocess.Popen | None = None
_metrics_addr = ""
_connector_id = ""
# An Event, not a bare flag: every wait below is interruptible, so SIGTERM
# stops the service promptly instead of systemd having to SIGKILL it after a
# stop timeout (which risks orphaning the cloudflared child).
_stop_evt = threading.Event()


class RateLimited(RuntimeError):
    """Cloudflare refused to mint a quick tunnel (429 / error 1015)."""


def log(msg: str) -> None:
    print(f"[live_call-tunnel] {msg}", flush=True)


def _cloudflared() -> str:
    for cand in (
        os.environ.get("LIVE_CALL_CLOUDFLARED", ""),
        str(Path(__file__).parent / "bin" / "cloudflared"),
        str(HOME / ".local" / "bin" / "cloudflared"),
        "/usr/local/bin/cloudflared",
    ):
        if cand and Path(cand).exists():
            return cand
    log("FATAL: cloudflared not found")
    sys.exit(2)


def _recent_log(offset: int) -> str:
    try:
        with LOG_FILE.open("rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def start_tunnel() -> tuple[subprocess.Popen, str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(LOG_FILE, "ab")
    f.write(f"\n===== tunnel start {time.strftime('%Y-%m-%dT%H:%M:%SZ')} =====\n".encode())
    f.flush()
    offset = LOG_FILE.stat().st_size   # only parse this run's output

    proc = subprocess.Popen(
        [_cloudflared(), "tunnel", "--no-autoupdate", "--url", f"http://{BIND}"],
        stdout=f, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + STARTUP_TIMEOUT_S
    while time.time() < deadline and not _stop_evt.is_set():
        if proc.poll() is not None:
            tail = _recent_log(offset)
            if any(m in tail for m in RATE_LIMIT_MARKERS):
                raise RateLimited(
                    "Cloudflare is rate-limiting quick tunnels from this IP "
                    "(429/1015). A named tunnel or your own reverse proxy "
                    "(LIVE_CALL_PUBLIC_URL) avoids this entirely."
                )
            raise RuntimeError("cloudflared exited during startup")
        try:
            with LOG_FILE.open("rb") as fh:
                fh.seek(offset)
                hit = URL_RE.search(fh.read())
        except OSError:
            hit = None
        if hit:
            global _metrics_addr, _connector_id
            _metrics_addr, _connector_id = "", ""
            try:
                _metrics_addr, _connector_id = parse_tunnel_details(_recent_log(offset))
            except Exception as e:  # noqa: BLE001
                # A working tunnel must never be lost to a parsing slip; health
                # simply falls back to process liveness.
                log(f"could not read metrics details ({type(e).__name__}: {e})")
            log(f"metrics at {_metrics_addr or 'unknown'}, connector {_connector_id[:8] or '?'}")
            return proc, hit.group(0).decode()
        if _sleep(0.5):
            break
    proc.terminate()
    if _stop_evt.is_set():
        raise RuntimeError("stopping")
    raise RuntimeError(f"no tunnel URL within {STARTUP_TIMEOUT_S}s")


def parse_tunnel_details(blob: str) -> tuple[str, str]:
    """Pull cloudflared's metrics address and connector id out of its log."""
    m = METRICS_RE.search(blob)
    c = CONNECTOR_RE.search(blob)
    return (m.group(1) if m else ""), (c.group(1) if c else "")


def publish(url: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = URL_FILE.with_suffix(".tmp")
    tmp.write_text(url + "\n", encoding="utf-8")
    os.replace(tmp, URL_FILE)
    log(f"published {url}")


def tunnel_healthy(_url: str) -> bool:
    """Is OUR tunnel up — regardless of whether a room is currently listening?

    ``/ready`` reports cloudflared's registered edge connections. The
    connector id is checked too, so another cloudflared on this host answering
    the same port cannot make a dead tunnel of ours look alive.
    """
    if not _metrics_addr:
        return _proc is not None and _proc.poll() is None
    try:
        with urllib.request.urlopen(f"http://{_metrics_addr}/ready", timeout=5) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError:
        return False
    except Exception:
        # Metrics unreachable: fall back to process liveness rather than
        # tearing down a tunnel that may be fine.
        return _proc is not None and _proc.poll() is None

    if _connector_id and body.get("connectorId") not in (None, _connector_id):
        log("metrics port answered by a different cloudflared — ignoring it")
        return _proc is not None and _proc.poll() is None
    return int(body.get("readyConnections") or 0) > 0


def _handle_signal(_signum, _frame) -> None:
    _stop_evt.set()


def _sleep(seconds: float) -> bool:
    """Sleep unless/until we are asked to stop. Returns True if stopping."""
    return _stop_evt.wait(seconds)


def main() -> int:
    global _proc
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    backoff = 5
    while not _stop_evt.is_set():
        try:
            _proc, url = start_tunnel()
            publish(url)
            backoff = 5
            misses = 0
            while not _stop_evt.is_set():
                if _sleep(CHECK_INTERVAL_S):
                    break
                if _proc.poll() is not None:
                    log("cloudflared exited — restarting")
                    break
                if not tunnel_healthy(url):
                    misses += 1
                    # Only a sustained failure of cloudflared's own readiness
                    # counts; a single blip must not cost a new tunnel.
                    if misses >= 4:
                        log("cloudflared not ready for ~1min — recycling tunnel")
                        _proc.terminate()
                        break
                else:
                    misses = 0
        except RateLimited as e:
            # Publishing nothing is better than publishing a dead hostname:
            # the plugin then reports an actionable error instead of handing
            # the user a link that cannot resolve.
            try:
                URL_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            log(f"{e} Sleeping {RATE_LIMIT_BACKOFF_S // 60} min before retrying.")
            _sleep(RATE_LIMIT_BACKOFF_S)
            backoff = 5
        except Exception as e:  # noqa: BLE001 — supervisor must never die
            if _stop_evt.is_set():
                break
            log(f"error: {type(e).__name__}: {e}; retrying in {backoff}s")
            _sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
        finally:
            if _proc and _proc.poll() is None and _stop_evt.is_set():
                _proc.terminate()

    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
