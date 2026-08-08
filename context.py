"""Give the call the same context the chat assistant has.

A Gemini Live session starts blank, so without this the caller talks to an
assistant with amnesia — it knows its personality but not the user, not the
standing memory, and not the conversation that just asked for the call.

Two sources, both read-only:

1. **Built-in memory** — ``$HERMES_HOME/memories/{MEMORY.md,USER.md}``. These
   describe the OWNER, so they are only injected for an owner conversation,
   identified by the chat's stable platform id (never its display name — that
   is chosen by the person on the other end, so name matching would let anyone
   claim the owner's memory by renaming themselves). Fails closed: if the
   owner's chat id cannot be determined, no memory is injected.
2. **The conversation that requested the call** — the most recently active
   non-CLI session in ``state.db``. A call is always started from a chat, so
   that chat is by definition the newest session at mint time. Scoping this
   way means a caller only ever gets their OWN thread back, with no need to
   trust a caller-supplied identifier.

Everything is best-effort: any failure degrades to less context, never to a
failed call.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

MAX_MEMORY_CHARS = 14000
MAX_MSG_CHARS = 400
MAX_MSGS = 24
MAX_SESSION_AGE_S = 45 * 60      # older than this and the chat is not "current"
SKIP_SOURCES = ("cli", "cron", "subagent")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _read(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…(truncated)"
    return text


def load_memory() -> str:
    """MEMORY.md + USER.md, the assistant's built-in memory."""
    mem_dir = _hermes_home() / "memories"
    blocks = []
    user = _read(mem_dir / "USER.md", MAX_MEMORY_CHARS // 2)
    if user:
        blocks.append("## What you know about the user\n" + user)
    memory = _read(mem_dir / "MEMORY.md", MAX_MEMORY_CHARS // 2)
    if memory:
        blocks.append("## Your standing memory\n" + memory)
    return "\n\n".join(blocks)


def _owner_chat_id() -> str:
    """The owner's chat id, from env or the gateway's configured home channel.

    A chat id (e.g. a WhatsApp LID) is assigned by the platform; a display name
    is typed by the other party. Only the former is safe to authorize on.
    """
    override = os.environ.get("LIVE_CALL_OWNER_CHAT_ID", "").strip()
    if override:
        return override

    cfg_path = _hermes_home() / "config.yaml"
    try:
        import yaml

        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return ""

    def walk(node: Any) -> str:
        if isinstance(node, dict):
            hc = node.get("home_channel")
            if isinstance(hc, dict) and hc.get("chat_id"):
                return str(hc["chat_id"])
            for v in node.values():
                got = walk(v)
                if got:
                    return got
        return ""

    return walk(cfg).strip()


def recent_conversation() -> tuple[Optional[str], Optional[str], list[str]]:
    """Return ``(display_name, chat_id, transcript_lines)`` for the calling chat."""
    db = _hermes_home() / "state.db"
    if not db.exists():
        return None, None, []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return None, None, []
    try:
        con.execute("PRAGMA query_only = ON")
        placeholders = ",".join("?" for _ in SKIP_SOURCES)
        # Order by the newest MESSAGE, not session start: a long-running thread
        # (the owner's) would otherwise lose to a chat whose session merely
        # started later — and the call would open with someone else's context.
        row = con.execute(
            f"""SELECT s.id, s.display_name, s.chat_id, MAX(m.timestamp) AS last_ts
                  FROM sessions s JOIN messages m ON m.session_id = s.id
                 WHERE s.source NOT IN ({placeholders}) AND s.archived = 0
                 GROUP BY s.id ORDER BY last_ts DESC LIMIT 1""",
            SKIP_SOURCES,
        ).fetchone()
        if not row:
            return None, None, []
        session_id, display_name, chat_id, last_ts = row
        if last_ts and (time.time() - float(last_ts)) > MAX_SESSION_AGE_S:
            return display_name, chat_id, []

        rows = con.execute(
            """SELECT role, content FROM messages
                WHERE session_id = ? AND role IN ('user','assistant')
                  AND content IS NOT NULL AND content != ''
                ORDER BY id DESC LIMIT ?""",
            (session_id, MAX_MSGS),
        ).fetchall()
    except sqlite3.Error:
        return None, None, []
    finally:
        con.close()

    lines = []
    for role, content in reversed(rows):
        text = " ".join(str(content).split())
        if not text:
            continue
        if len(text) > MAX_MSG_CHARS:
            text = text[:MAX_MSG_CHARS].rstrip() + "…"
        lines.append(f"{'Them' if role == 'user' else 'You'}: {text}")
    return display_name, chat_id, lines


def build(note: str = "") -> str:
    """Assemble the context block appended to the call's system prompt."""
    display_name, chat_id, convo = recent_conversation()
    owner_id = _owner_chat_id()
    # Fail closed: no configured owner id, or no id on this chat, means no memory.
    is_owner = bool(owner_id and chat_id and str(chat_id) == owner_id)

    parts: list[str] = []
    if is_owner:
        mem = load_memory()
        if mem:
            parts.append(mem)
    elif display_name:
        parts.append(
            f"You are speaking with {display_name}, who is not the owner of this "
            "assistant. Do not share the owner's personal information, memory, or "
            "private context."
        )

    if convo:
        who = display_name or "the caller"
        parts.append(
            f"## The chat you were just having with {who}\n"
            "This call continues that conversation — the caller expects you to "
            "remember it. Do not read it back verbatim; just know it.\n\n"
            + "\n".join(convo)
        )

    if note:
        parts.append(f"## Why they asked for this call\n{note}")

    return "\n\n".join(parts)
