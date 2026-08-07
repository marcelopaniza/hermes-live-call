"""live_call plugin — voice/video call room for Hermes.

Flow: user asks for a call in any chat -> agent calls ``call_start`` -> plugin
mints a single-use tokenized link served by a local room service -> agent
texts the link back on the same channel -> user taps it, phone microphone
streams over a secure WebSocket to a realtime model (Gemini Live by default)
speaking as the assistant's own persona -> the session is transcribed.

Shipped: full-duplex voice, the assistant's own persona, memory + the chat
that requested the call, and a per-call transcript. Camera vision and MP4
recording are planned; sites are marked ``TODO``.

Registration mirrors the bundled google_meet plugin — the established
"agent joins a live call" pattern in Hermes.
"""

from __future__ import annotations

import logging
import platform

from . import service as svc
from .tools import (
    CALL_END_SCHEMA,
    CALL_START_SCHEMA,
    CALL_STATUS_SCHEMA,
    check_live_call_requirements,
    handle_call_end,
    handle_call_start,
    handle_call_status,
)

logger = logging.getLogger(__name__)


_TOOLS = (
    ("call_start",  CALL_START_SCHEMA,  handle_call_start,  "📞"),
    ("call_status", CALL_STATUS_SCHEMA, handle_call_status, "🟢"),
    ("call_end",    CALL_END_SCHEMA,    handle_call_end,    "👋"),
)


def _on_session_end(**kwargs) -> None:
    """Reap only rooms that are already finished.

    A chat session ending must NOT kill a call: the user may not have tapped
    the link yet (sessions rotate far faster than a person picks up a phone),
    and a live call obviously outlives the turn that started it. Unused links
    are reaped by the room's own TTL watchdog, and a finished call closes its
    room automatically — so this hook is a safety net, not the main path.
    """
    try:
        st = svc.status()
        if not st.get("active"):
            return
        if st.get("joined"):
            logger.info("live_call: session ended but a call is live — leaving it running")
            return
        if st.get("awaiting_caller"):
            logger.info("live_call: session ended, link still valid — leaving it for the caller")
            return
        svc.stop_room(reason="session ended (call already over)")
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("live_call on_session_end cleanup failed: %s", e)


def register(ctx) -> None:
    """Register tools + lifecycle hook. Called once by the plugin loader."""
    system = platform.system().lower()
    if system not in {"linux", "darwin"}:
        logger.info("live_call plugin: platform=%s not supported (linux/macos only)", system)
        return

    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="live_call",
            schema=schema,
            handler=handler,
            check_fn=check_live_call_requirements,
            emoji=emoji,
        )

    ctx.register_hook("on_session_end", _on_session_end)
