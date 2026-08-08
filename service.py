"""live_call service — process manager for the room server.

The gateway-side plugin stays dependency-free: this module mints tokens and
SPAWNS ``room_server.py`` as a subprocess in its own interpreter/venv
(``LIVE_CALL_PYTHON``), following the google_meet precedent so pipecat never
enters the live gateway environment. The room server owns HTTP (join page,
the audio WebSocket, /healthz, /control/stop) and the media pipeline.

Pipeline selection: ``LIVE_CALL_PIPELINE`` env, defaulting to ``gemini`` when
``GEMINI_API_KEY`` is present and ``echo`` otherwise — so the same build is
verifiable before a model key exists, and upgrades itself once one appears.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
DEFAULT_BIND = "127.0.0.1:8199"
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
_STARTUP_TIMEOUT_S = 25


class RoomAlreadyActive(RuntimeError):
    pass


class MissingDependency(RuntimeError):
    def __init__(self, msg: str, stage: str, hint: str):
        super().__init__(msg)
        self.stage = stage
        self.hint = hint


@dataclass
class Room:
    token: str
    mode: str                       # "voice" | "video"
    note: str
    created: float
    ttl_minutes: int
    joined: bool = False
    recording_path: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def expired(self) -> bool:
        return (not self.joined) and (time.time() - self.created > self.ttl_minutes * 60)

    def age_s(self) -> int:
        return int(time.time() - self.created)


_lock = threading.Lock()
_room: Optional[Room] = None
_proc: Optional[subprocess.Popen] = None
_tunnel_proc: Optional[subprocess.Popen] = None
_control_secret: Optional[str] = None
_probe_cache: Dict[str, Optional[str]] = {}

_CONTEXT_MODULE = "live_call._context"
_MAX_LOG_BYTES = 5 * 1024 * 1024
_TUNNEL_TIMEOUT_S = 30
_TRYCLOUDFLARE_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")


# ---------------------------------------------------------------------------
# Environment resolution
# ---------------------------------------------------------------------------

def _open_log(path: Path):
    """Append to a log, rolling it over once it gets large.

    These files are opened for the lifetime of a subprocess and never rotated
    by anything else, so without this they grow forever on a long-lived host.
    """
    try:
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass
    return open(path, "ab")


def _bind() -> str:
    return os.environ.get("LIVE_CALL_BIND", DEFAULT_BIND)


def _public_base() -> str:
    return os.environ.get("LIVE_CALL_PUBLIC_URL", f"http://{_bind()}").rstrip("/")


def _published_tunnel() -> Optional[str]:
    """URL published by the always-on tunnel supervisor, if it is healthy.

    Preferred over minting a per-call tunnel: a supervised tunnel outlives the
    agent turn that started the call (a per-turn tunnel dies with it, leaving
    the user holding a dead link) and its DNS is long since propagated.
    """
    state_dir = Path(os.environ.get("LIVE_CALL_STATE_DIR", str(_recordings_dir())))
    url_file = state_dir / "tunnel_url.txt"
    try:
        url = url_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not url.startswith("https://"):
        return None
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=6) as r:
            if r.status < 500:
                return url
    except urllib.error.HTTPError as e:
        if e.code < 500:      # edge is up; room may simply be idle
            return url
    except (urllib.error.URLError, OSError, ValueError):
        pass
    logger.warning("live_call: published tunnel %s is not answering", url)
    return None


def _supervisor_present() -> bool:
    """Is an always-on tunnel supervisor managing this host's public URL?"""
    state_dir = Path(os.environ.get("LIVE_CALL_STATE_DIR", str(_recordings_dir())))
    if (state_dir / "tunnel_url.txt").exists():
        return True
    try:
        res = subprocess.run(
            ["systemctl", "--user", "is-enabled", "hermes-live-call-tunnel.service"],
            capture_output=True, text=True, timeout=5,
        )
        return res.stdout.strip() in {"enabled", "static", "enabled-runtime"}
    except (OSError, subprocess.SubprocessError):
        return False


def _cloudflared() -> Optional[str]:
    cand = os.environ.get("LIVE_CALL_CLOUDFLARED")
    if cand and Path(cand).exists():
        return cand
    for p in (ROOT / "bin" / "cloudflared", Path.home() / "bin" / "cloudflared"):
        if p.exists():
            return str(p)
    return shutil.which("cloudflared")


def _start_tunnel(log_path: Path) -> str:
    """Spin up a Cloudflare quick tunnel to the room and return its public URL.

    A fresh tunnel per call keeps links unguessable and means no domain, no
    account, and no inbound firewall hole. Raises MissingDependency when
    cloudflared is absent so the agent gets an actionable error instead of a
    localhost URL the phone cannot reach.
    """
    global _tunnel_proc
    exe = _cloudflared()
    if not exe:
        raise MissingDependency(
            "cloudflared is not installed, so no public link can be created",
            stage="tunnel",
            hint="install cloudflared, or set LIVE_CALL_PUBLIC_URL to an already-public base URL",
        )
    _stop_tunnel()   # never run two tunnels against one port

    # Only ever parse what THIS process writes: the log is append-only across
    # runs, and scanning the whole file hands back a previous (dead) tunnel's
    # URL — which is exactly how a 530 reached the user once.
    f = _open_log(log_path)
    f.write(f"\n===== tunnel {time.strftime('%Y-%m-%dT%H:%M:%S')} =====\n".encode())
    f.flush()
    start_offset = log_path.stat().st_size

    _tunnel_proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://{_bind()}", "--no-autoupdate"],
        stdout=f, stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.time() + _TUNNEL_TIMEOUT_S
    while time.time() < deadline:
        if _tunnel_proc.poll() is not None:
            raise RuntimeError(f"cloudflared exited early (see {log_path})")
        try:
            with log_path.open("rb") as fh:
                fh.seek(start_offset)
                hit = _TRYCLOUDFLARE_RE.search(fh.read())
        except OSError:
            hit = None
        if hit:
            url = hit.group(0).decode()
            _await_tunnel_dns(url)
            logger.info("live_call tunnel up: %s", url)
            return url
        time.sleep(0.5)
    _stop_tunnel()
    raise RuntimeError(f"cloudflared did not produce a URL within {_TUNNEL_TIMEOUT_S}s (see {log_path})")


def _await_tunnel_dns(url: str, timeout_s: int = 25) -> None:
    """Block until the fresh hostname actually answers.

    A quick tunnel's DNS takes a few seconds to propagate; handing the agent a
    URL before then means the user taps and gets a resolver error. Better a
    slower tool call than a dead link. Non-fatal on timeout — the room is up
    and the name usually lands moments later.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=4) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1.5)
    logger.warning("live_call: tunnel %s not answering yet after %ss", url, timeout_s)


def _stop_tunnel() -> None:
    global _tunnel_proc
    p, _tunnel_proc = _tunnel_proc, None
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(timeout=4)
    except subprocess.TimeoutExpired:
        p.kill()


def _model() -> str:
    return os.environ.get("LIVE_CALL_MODEL", DEFAULT_MODEL)


def _pipeline_kind() -> str:
    kind = os.environ.get("LIVE_CALL_PIPELINE", "").strip().lower()
    if kind in {"echo", "gemini"}:
        return kind
    return "gemini" if os.environ.get("GEMINI_API_KEY") else "echo"


def _recordings_dir() -> Path:
    default = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "workspace" / "live_calls"
    d = Path(os.environ.get("LIVE_CALL_RECORDINGS_DIR", str(default)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_python() -> str:
    """Interpreter for the room server: env override, dev venv (repo root),
    deployed venv (inside the plugin dir), then this interpreter."""
    cand = os.environ.get("LIVE_CALL_PYTHON")
    if cand:
        return cand
    for p in (ROOT.parent / ".venv" / "bin" / "python", ROOT / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    return sys.executable


def _probe_deps(py: str) -> None:
    """One subprocess probe per interpreter per process: readable error early
    beats an opaque spawn failure later."""
    if py in _probe_cache:
        err = _probe_cache[py]
    else:
        try:
            res = subprocess.run(
                [py, "-c", "import aiohttp, pipecat"],
                capture_output=True, text=True, timeout=30,
            )
            err = None if res.returncode == 0 else (res.stderr.strip().splitlines() or ["import failed"])[-1]
        except FileNotFoundError:
            err = f"interpreter not found: {py}"
        except subprocess.TimeoutExpired:
            err = "dependency probe timed out"
        _probe_cache[py] = err
    if err:
        raise MissingDependency(
            f"room server deps unavailable in {py}: {err}",
            stage="deps",
            hint="python -m venv .venv && .venv/bin/pip install aiohttp 'pipecat-ai[webrtc,google]' (or set LIVE_CALL_PYTHON)",
        )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def mint_token() -> str:
    return secrets.token_urlsafe(16)


def _load_context_module():
    """Import the plugin's context module without polluting sys.path/sys.modules.

    This runs inside the long-lived gateway, which loads many plugins into one
    interpreter; a bare ``import context`` would put a very generic name into
    the shared module cache (first import wins, forever) and grow sys.path on
    every call.
    """
    import importlib.util  # noqa: PLC0415

    cached = sys.modules.get(_CONTEXT_MODULE)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_CONTEXT_MODULE, ROOT / "context.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load context module from {ROOT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CONTEXT_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _discard_snapshot(path: Optional[Path]) -> None:
    """Drop a context snapshot whose room never started."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _room_state_file() -> Path:
    return _recordings_dir() / "room.json"


def _is_room_pid(pid: int) -> bool:
    """Confirm the pid is still OUR room server — pids get recycled, and
    signalling a stranger because a state file went stale is unacceptable."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False
    return "room_server.py" in cmdline


def _port_free() -> bool:
    """A retired room stops answering health before it releases the socket."""
    host, _, port = _bind().partition(":")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host or "127.0.0.1", int(port or 8199))) != 0


def _clear_foreign_room() -> None:
    """Retire a room left behind by an earlier process before we bind the port.

    Agent turns are separate processes, so in-memory state cannot see a room
    started by a previous one. If that orphan still holds the port, our room
    fails to bind and the freshly minted link 404s (observed on the live box).
    A call in progress is never interrupted.
    """
    hz = _healthz(timeout=2)
    if not hz or not hz.get("ok"):
        return
    if hz.get("joined"):
        raise RoomAlreadyActive("a call is already in progress on this machine")

    # The room writes its state before serving, but tolerate a slow/absent file
    # rather than declaring an unstoppable room.
    state: Dict[str, Any] = {}
    for _ in range(6):
        try:
            state = json.loads(_room_state_file().read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            time.sleep(0.5)

    secret = state.get("secret")
    if secret:
        try:
            req = urllib.request.Request(
                f"http://{_bind()}/control/stop", data=b"{}", method="POST",
                headers={"X-Control-Secret": secret, "content-type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except (urllib.error.URLError, OSError):
            pass

    pid = state.get("pid")
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _healthz(timeout=1) and _port_free():
            logger.info("live_call: retired an orphaned room (pid=%s)", pid)
            return
        time.sleep(0.5)

    if isinstance(pid, int) and _is_room_pid(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(1.5)
            if not _healthz(timeout=1):
                logger.info("live_call: killed orphaned room pid=%s with %s", pid, sig)
                return
    raise RuntimeError(
        f"another room server is holding {_bind()} and would not stop; "
        "stop it manually before starting a new call"
    )


def _healthz(timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://{_bind()}/healthz", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def start_room(mode: str, note: str, ttl_minutes: int) -> Dict[str, Any]:
    global _room, _proc, _control_secret
    with _lock:
        if _room is not None and not _room.expired() and _proc is not None and _proc.poll() is None:
            raise RoomAlreadyActive(f"a {_room.mode} room is already active (age {_room.age_s()}s)")

        py = find_python()
        _probe_deps(py)
        _clear_foreign_room()

        token = mint_token()
        secret = secrets.token_urlsafe(24)
        instance = secrets.token_hex(16)
        rec_dir = _recordings_dir()
        log_path = rec_dir / "room_server.log"

        # Snapshot the caller's context NOW. Resolving it when the link is
        # tapped means whichever chat spoke most recently wins — so a
        # non-owner's link, tapped after the owner happens to message, would
        # open with the OWNER's memory and conversation. Minting time is the
        # only moment we can attribute the request correctly.
        ctx_path = None
        try:
            block = _load_context_module().build(note=note)
            if block:
                ctx_path = rec_dir / f"context-{token[:8]}.txt"
                fd = os.open(ctx_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(block)
        except Exception as e:  # noqa: BLE001 — context is a bonus, not a gate
            logger.warning("live_call: could not snapshot context: %s", e)


        env = dict(os.environ)
        env.update({
            "LIVE_CALL_TOKEN": token,
            "LIVE_CALL_MODE": mode,
            "LIVE_CALL_BIND": _bind(),
            "LIVE_CALL_CONTROL_SECRET": secret,
            "LIVE_CALL_INSTANCE": instance,
            "LIVE_CALL_PIPELINE": _pipeline_kind(),
            "LIVE_CALL_MODEL": _model(),
            "LIVE_CALL_NOTE": note,
            "LIVE_CALL_TTL_S": str(ttl_minutes * 60),
            "LIVE_CALL_RECORDINGS_DIR": str(rec_dir),
            "LIVE_CALL_CONTEXT_FILE": str(ctx_path) if ctx_path else "",
        })
        log_f = _open_log(log_path)
        log_f.write(f"\n===== spawn {time.strftime('%Y-%m-%dT%H:%M:%S')} mode={mode} pipeline={env['LIVE_CALL_PIPELINE']} =====\n".encode())
        proc = subprocess.Popen(
            [py, str(ROOT / "room_server.py")],
            stdout=log_f, stderr=subprocess.STDOUT,
            env=env, cwd=str(ROOT),
            start_new_session=True,
        )
        log_f.close()   # the child holds its own dup of the fd now

        deadline = time.time() + _STARTUP_TIMEOUT_S
        hz = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            hz = _healthz(timeout=0.8)
            # Must be OUR room: a stranger answering health is how a bad token
            # got handed out before.
            if hz and hz.get("ok") and hz.get("instance") == instance:
                break
            hz = None
            time.sleep(0.4)

        if not hz or proc.poll() is not None:
            try:
                proc.terminate()
            except OSError:
                pass
            tail = _tail(log_path)
            _discard_snapshot(ctx_path)
            raise RuntimeError(f"room server failed to start (see {log_path}). Last output: {tail}")

        # Public reachability, in order: explicit base > the always-on
        # supervisor's tunnel > a per-call tunnel (last resort; it dies with
        # the agent turn). When a supervisor is managing tunnels but has no
        # healthy URL, do NOT mint another one — quick tunnels are rate-limited
        # per IP, so piling on is what keeps the block alive.
        try:
            base = os.environ.get("LIVE_CALL_PUBLIC_URL", "").rstrip("/") or _published_tunnel()
            if not base:
                if _supervisor_present():
                    raise MissingDependency(
                        "no public link is available right now — the tunnel service has "
                        "no healthy URL (Cloudflare rate-limits quick tunnels per IP)",
                        stage="tunnel",
                        hint="check: systemctl --user status hermes-live-call-tunnel; "
                             "for a permanent fix set LIVE_CALL_PUBLIC_URL to a named "
                             "tunnel or your own reverse proxy",
                    )
                base = _start_tunnel(rec_dir / "tunnel.log")
        except Exception:
            try:
                proc.terminate()
            except OSError:
                pass
            _discard_snapshot(ctx_path)
            raise

        _room = Room(token=token, mode=mode, note=note, created=time.time(), ttl_minutes=ttl_minutes)
        _proc = proc
        _control_secret = secret
        return {
            "url": f"{base}/join/{token}?mode={mode}",
            "mode": mode,
            "ttl_minutes": ttl_minutes,
            "pipeline": env["LIVE_CALL_PIPELINE"],
            "model": _model() if env["LIVE_CALL_PIPELINE"] == "gemini" else None,
            "log": str(log_path),
        }


def status() -> Dict[str, Any]:
    with _lock:
        if _room is None:
            return {"active": False}
        if _proc is not None and _proc.poll() is not None:
            # Room self-closed after the call — reap its tunnel too.
            _stop_tunnel()
            return {"active": False, "exited": True, "returncode": _proc.returncode}
        if _room.expired():
            return {"active": False, "expired_room": True}
        out: Dict[str, Any] = {
            "active": True,
            "mode": _room.mode,
            "age_s": _room.age_s(),
            "joined": _room.joined,
            "recording_path": _room.recording_path,
        }
    hz = _healthz()
    if hz:
        out["joined"] = bool(hz.get("joined", out["joined"]))
        if hz.get("recording_path"):
            out["recording_path"] = hz["recording_path"]
        out["pipeline"] = hz.get("pipeline")
        # A minted link nobody has tapped yet — must survive session churn.
        out["awaiting_caller"] = not hz.get("used", False)
        out["transcript_path"] = hz.get("transcript_path")
        with _lock:
            if _room is not None:
                _room.joined = out["joined"]
                _room.recording_path = out.get("recording_path")
    return out


def stop_room(reason: str = "") -> Dict[str, Any]:
    global _room, _proc, _control_secret
    with _lock:
        room, proc, secret = _room, _proc, _control_secret
        _room, _proc, _control_secret = None, None, None

    _stop_tunnel()
    if room is not None:
        for leftover in _recordings_dir().glob(f"context-{room.token[:8]}.txt"):
            try:
                leftover.unlink()
            except OSError:
                pass

    if proc is None or proc.poll() is not None:
        if room is not None:
            return {"stopped": True, "reason": reason or "process already gone", "age_s": room.age_s()}
        return {"stopped": False, "reason": "no active room"}

    try:
        req = urllib.request.Request(
            f"http://{_bind()}/control/stop", data=b"{}", method="POST",
            headers={"X-Control-Secret": secret or "", "content-type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=2)
    except (urllib.error.URLError, OSError):
        pass

    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    age = room.age_s() if room else None
    logger.info("live_call room stopped (%s) after %ss", reason or "no reason", age)
    return {"stopped": True, "reason": reason, "age_s": age}


def current_room() -> Optional[Room]:
    with _lock:
        if _room is not None and _room.expired():
            return None
        return _room


def _tail(path: Path, n: int = 5) -> str:
    try:
        lines = path.read_bytes().decode(errors="replace").strip().splitlines()
        return " | ".join(lines[-n:])
    except OSError:
        return "(no log)"
