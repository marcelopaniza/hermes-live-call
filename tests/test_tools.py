"""Unit tests for the live_call plugin (hermetic — stdlib only, no spawn).

Run from live_call/:  python -m pytest tests/ -q   (or the plain runner)
The real media path is covered by tests/ws_client.py against a live room
(see tests/verify_live_call.py).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # hermes-calls/

from live_call import service, tools  # noqa: E402


def setup_function(_fn):
    service._room = None
    service._proc = None
    service._probe_cache.clear()
    os.environ.pop("LIVE_CALL_PIPELINE", None)


def test_schemas_shape():
    for schema in (tools.CALL_START_SCHEMA, tools.CALL_STATUS_SCHEMA, tools.CALL_END_SCHEMA):
        assert schema["name"]
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"


def test_status_idle():
    out = json.loads(tools.handle_call_status({}))
    assert out["success"] is True
    assert out["active"] is False


def test_call_start_rejects_bad_mode():
    out = json.loads(tools.handle_call_start({"mode": "hologram"}))
    assert out["success"] is False
    assert "mode" in out["error"]


def test_missing_deps_yields_hint():
    """Force the room-server interpreter to one without pipecat: readable
    MissingDependency with a pip hint, no spawn attempted."""
    os.environ["LIVE_CALL_PYTHON"] = sys.executable
    try:
        import pipecat  # noqa: F401
        return  # environment actually has pipecat in the base interpreter; covered elsewhere
    except ImportError:
        pass
    try:
        out = json.loads(tools.handle_call_start({"mode": "voice"}))
        assert out["success"] is False
        assert "pipecat" in out["error"] or "deps" in out["error"]
        assert out.get("hint")
    finally:
        os.environ.pop("LIVE_CALL_PYTHON", None)


def test_room_lifecycle_and_expiry():
    room = service.Room(token=service.mint_token(), mode="voice", note="", created=time.time() - 3600, ttl_minutes=10)
    assert room.expired() is True
    room2 = service.Room(token=service.mint_token(), mode="video", note="", created=time.time(), ttl_minutes=10)
    assert room2.expired() is False
    assert room2.token != room.token
    assert len(room.token) >= 16


def test_single_room_guard_without_spawn():
    """Guard fires before any dependency probe or spawn."""

    class FakeProc:
        def poll(self):
            return None

    service._room = service.Room(token="t", mode="voice", note="", created=time.time(), ttl_minutes=10)
    service._proc = FakeProc()
    out = json.loads(tools.handle_call_start({"mode": "video"}))
    assert out["success"] is False and "already active" in out["error"]
    service._room = None
    service._proc = None


def test_pipeline_kind_defaults():
    had = os.environ.pop("GEMINI_API_KEY", None)
    try:
        assert service._pipeline_kind() == "echo"
        os.environ["GEMINI_API_KEY"] = "x"
        assert service._pipeline_kind() == "gemini"
        os.environ["LIVE_CALL_PIPELINE"] = "echo"
        assert service._pipeline_kind() == "echo"
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("LIVE_CALL_PIPELINE", None)
        if had:
            os.environ["GEMINI_API_KEY"] = had
