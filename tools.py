"""Agent-facing tools for the live_call plugin.

Tools:
  call_start  — mint a one-time room link (mode=voice|video) and start the
                room service. The agent should send the returned ``url`` to
                the user on the channel the request came from.
  call_status — liveness: active room, mode, age, whether the user joined.
  call_end    — tear the room down (also runs automatically on session end).

Handlers follow the google_meet convention: ``handler(args, **_kw) -> str``
returning a compact JSON string the model can read.
"""

from __future__ import annotations

import json
import platform
from typing import Any, Dict


def check_live_call_requirements() -> bool:
    """Cheap availability gate. Heavy deps (aiohttp/pipecat) are checked at
    call time so the plugin loads and lists tools on a bare box."""
    return platform.system().lower() in {"linux", "darwin"}


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _err(msg: str, **extra: Any) -> str:
    return _json({"success": False, "error": msg, **extra})


CALL_START_SCHEMA: Dict[str, Any] = {
    "name": "call_start",
    "description": (
        "Start a live call room and mint a ONE-TIME join link for the user. "
        "Send the returned url back to the user in chat — it is single-use, "
        "expires, and only one room can exist at a time. You will speak on the "
        "call yourself, as yourself, with your memory and this conversation "
        "already loaded. A transcript is written to the workspace."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["voice", "video"],
                "description": (
                    "voice = mic only (default). video also turns the camera on, "
                    "but camera vision is not wired to the model yet — do not "
                    "promise the user you can see them in video mode."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional context for the call agent (what the user wants "
                    "to talk about / look at), injected into the call prompt."
                ),
            },
            "ttl_minutes": {
                "type": "integer",
                "description": "Link validity in minutes before first join (default 20, max 60).",
            },
        },
        "required": [],
    },
}

CALL_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "call_status",
    "description": "Report the live call room state: is a room active, its mode, link age, whether the caller joined, and the transcript path.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

CALL_END_SCHEMA: Dict[str, Any] = {
    "name": "call_end",
    "description": "End the active call room: invalidates the link, stops media, and finalizes the transcript.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def handle_call_start(args: Dict[str, Any], **_kw) -> str:
    mode = (args.get("mode") or "voice").strip().lower()
    if mode not in {"voice", "video"}:
        return _err("mode must be 'voice' or 'video'")
    ttl = args.get("ttl_minutes") or 20
    try:
        ttl = max(1, min(int(ttl), 60))
    except (TypeError, ValueError):
        return _err("ttl_minutes must be an integer")

    from . import service as svc  # lazy: keeps tool listing dependency-free

    try:
        res = svc.start_room(mode=mode, note=(args.get("note") or "").strip(), ttl_minutes=ttl)
    except svc.RoomAlreadyActive as e:
        return _err(str(e), hint="call_end first, or call_status to inspect")
    except svc.MissingDependency as e:
        return _err(str(e), stage=e.stage, hint=e.hint)
    except Exception as e:  # surface readable failures to the agent
        return _err(f"room start failed: {e}")

    return _json({"success": True, **res})


def handle_call_status(args: Dict[str, Any], **_kw) -> str:
    from . import service as svc

    return _json({"success": True, **svc.status()})


def handle_call_end(args: Dict[str, Any], **_kw) -> str:
    from . import service as svc

    res = svc.stop_room(reason="agent call_end")
    return _json({"success": True, **res})
