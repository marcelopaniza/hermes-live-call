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

import os
import re
import signal
import subprocess
import sys
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
URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
STARTUP_TIMEOUT_S = 60
CHECK_INTERVAL_S = 15

_proc: subprocess.Popen | None = None
_stop = False


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
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("cloudflared exited during startup")
        try:
            with LOG_FILE.open("rb") as fh:
                fh.seek(offset)
                hit = URL_RE.search(fh.read())
        except OSError:
            hit = None
        if hit:
            return proc, hit.group(0).decode()
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"no tunnel URL within {STARTUP_TIMEOUT_S}s")


def publish(url: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = URL_FILE.with_suffix(".tmp")
    tmp.write_text(url + "\n", encoding="utf-8")
    os.replace(tmp, URL_FILE)
    log(f"published {url}")


def edge_alive(url: str) -> bool:
    """The room may be down (that is fine); we only need the EDGE to answer.

    Any HTTP status proves Cloudflare can reach cloudflared. 502/1033-class
    failures raise, which is what we want to detect.
    """
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=8) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def _handle_signal(_signum, _frame) -> None:
    global _stop
    _stop = True


def main() -> int:
    global _proc
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    backoff = 5
    while not _stop:
        try:
            _proc, url = start_tunnel()
            publish(url)
            backoff = 5
            misses = 0
            while not _stop:
                time.sleep(CHECK_INTERVAL_S)
                if _proc.poll() is not None:
                    log("cloudflared exited — restarting")
                    break
                if not edge_alive(url):
                    misses += 1
                    # The room server is often down between calls, so only a
                    # sustained edge failure counts as a dead tunnel.
                    if misses >= 4:
                        log("edge unreachable for ~1min — recycling tunnel")
                        _proc.terminate()
                        break
                else:
                    misses = 0
        except Exception as e:  # noqa: BLE001 — supervisor must never die
            log(f"error: {type(e).__name__}: {e}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        finally:
            if _proc and _proc.poll() is None and _stop:
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
