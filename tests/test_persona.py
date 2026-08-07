"""Persona assembly: the call must sound like the user's own assistant."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import persona  # noqa: E402

CONFIG = """
display:
  personality: custom
agent:
  personalities:
    default: 'You are a generic assistant.'
    custom: 'You are Sparrow, the user''s sharp right hand.'
"""


def _home(tmp: Path, soul: str | None = "You are Stock Assistant, made by Vendor.") -> Path:
    if soul is not None:
        (tmp / "SOUL.md").write_text(soul, encoding="utf-8")
    (tmp / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp


def setup_function(_fn):
    for key in ("LIVE_CALL_SYSTEM_FILE", "LIVE_CALL_CONTEXT", "HERMES_HOME"):
        os.environ.pop(key, None)
    os.environ["LIVE_CALL_CONTEXT"] = "0"   # context tested separately


def test_active_personality_found_by_search():
    with tempfile.TemporaryDirectory() as d:
        os.environ["HERMES_HOME"] = str(_home(Path(d)))
        name, text = persona.active_personality()
        assert name == "custom"
        assert "Sparrow" in text


def test_personality_wins_over_soul_identity():
    """SOUL.md and the personality often claim different names; the personality
    is the identity the user actually talks to."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["HERMES_HOME"] = str(_home(Path(d)))
        p = persona.build(mode="voice")
        assert "Sparrow" in p
        assert "Stock Assistant" in p          # base identity still present
        assert "personality section is authoritative" in p


def test_speech_rules_always_video_rules_only_in_video():
    with tempfile.TemporaryDirectory() as d:
        os.environ["HERMES_HOME"] = str(_home(Path(d)))
        voice = persona.build(mode="voice")
        video = persona.build(mode="video")
        assert "live voice call" in voice
        assert "camera" not in voice.split("live voice call")[1][:400] or "see the caller" not in voice
        assert "see the caller's camera" in video


def test_system_file_override_replaces_soul_and_personality():
    with tempfile.TemporaryDirectory() as d:
        tmp = _home(Path(d))
        override = tmp / "override.md"
        override.write_text("You are Override Bot.", encoding="utf-8")
        os.environ["HERMES_HOME"] = str(tmp)
        os.environ["LIVE_CALL_SYSTEM_FILE"] = str(override)
        p = persona.build(mode="voice")
        assert "Override Bot" in p
        assert "Sparrow" not in p and "Stock Assistant" not in p


def test_note_is_included():
    with tempfile.TemporaryDirectory() as d:
        os.environ["HERMES_HOME"] = str(_home(Path(d)))
        p = persona.build(mode="voice", note="he wants to review the budget")
        assert "review the budget" in p


def test_missing_home_still_produces_a_prompt():
    os.environ["HERMES_HOME"] = "/nonexistent-hermes-home"
    p = persona.build(mode="voice")
    assert len(p) > 50
    assert "live voice call" in p
