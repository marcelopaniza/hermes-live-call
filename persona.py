"""Persona assembly — the call must sound like the SAME assistant as the chat.

Sources, in order (all optional, all best-effort):
  1. ``$HERMES_HOME/SOUL.md``            — base identity
  2. ``$HERMES_HOME/config.yaml``        — the ACTIVE personality's text
     (``display.personality`` names a key inside a ``personalities`` map;
     that map lives under an agent/display section depending on version, so it
     is located by search rather than a hardcoded path)
  3. call-specific speech guidance (always appended — voice is not chat)
  4. the ``note`` passed by the agent when it started the call

An explicit ``LIVE_CALL_SYSTEM_FILE`` overrides 1+2 entirely.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

SPEECH_RULES = (
    "You are on a live voice call, not in a chat. Speak in short, natural "
    "sentences — a couple at a time, then let them reply. Never read out URLs, "
    "file paths, code, logs, or markdown; describe them instead. If asked for "
    "something long, summarize aloud and offer to send the details in chat. "
    "It is fine to be interrupted mid-sentence; just stop and listen."
)

VIDEO_RULES = (
    "Video is on: you can see the caller's camera. When they show you "
    "something, describe what you actually see and answer about it directly. "
    "Say so plainly if the image is unclear or too dark to read."
)


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _find_personalities(node: Any) -> Optional[dict]:
    """Depth-first search for a ``personalities`` mapping anywhere in the config."""
    if isinstance(node, dict):
        got = node.get("personalities")
        if isinstance(got, dict) and got:
            return got
        for v in node.values():
            found = _find_personalities(v)
            if found:
                return found
    return None


def _find_active_name(node: Any) -> Optional[str]:
    """Find ``display.personality`` (or a bare ``personality``) value."""
    if isinstance(node, dict):
        disp = node.get("display")
        if isinstance(disp, dict) and isinstance(disp.get("personality"), str):
            return disp["personality"]
        if isinstance(node.get("personality"), str):
            return node["personality"]
        for v in node.values():
            found = _find_active_name(v)
            if found:
                return found
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def active_personality() -> tuple[Optional[str], str]:
    """Return ``(name, text)`` of the assistant's currently selected personality."""
    cfg_path = _hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return None, ""
    try:
        import yaml
    except ImportError:
        return None, ""
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — a broken config must not break the call
        return None, ""
    name = _find_active_name(cfg)
    table = _find_personalities(cfg) or {}
    text = table.get(name, "") if name else ""
    return name, (text or "").strip()


def build(mode: str = "voice", note: str = "") -> str:
    """Assemble the system prompt for a call."""
    override = os.environ.get("LIVE_CALL_SYSTEM_FILE", "")
    parts: list[str] = []

    if override and Path(override).exists():
        parts.append(_read_text(Path(override)))
    else:
        soul = _read_text(_hermes_home() / "SOUL.md")
        if soul:
            parts.append(soul)
        _, personality = active_personality()
        if personality:
            parts.append(personality)
            if soul:
                # SOUL.md and the personality often assert different names; the
                # personality is the one the user actually talks to in chat, so
                # it wins (otherwise the call answers as generic Hermes).
                parts.append(
                    "Where the sections above disagree about who you are, the "
                    "personality section is authoritative: that is your name, "
                    "voice, and character on this call."
                )

    if not parts:
        parts.append(
            "You are the user's personal AI assistant, speaking on a live call."
        )

    parts.append(SPEECH_RULES)
    if mode == "video":
        parts.append(VIDEO_RULES)

    # Memory + the conversation that asked for the call. Without this the
    # caller reaches an assistant with amnesia (reported after the first real
    # call: "the AI had no access to current memories from WhatsApp").
    if os.environ.get("LIVE_CALL_CONTEXT", "1") not in ("0", "false", "no"):
        try:
            import context as context_mod

            ctx = context_mod.build(note=note)
        except Exception:  # noqa: BLE001 — context is a bonus, never a blocker
            ctx = ""
        if ctx:
            parts.append(ctx)
            note = ""   # already included by the context block

    if note:
        parts.append(f"Context for this call, from the chat request that started it: {note}")

    return "\n\n".join(p for p in parts if p)
