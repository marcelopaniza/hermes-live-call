"""Context injection: right conversation, and no cross-user leakage.

These are the privacy-critical paths — an assistant shared by a household must
never hand the owner's memory to another caller.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import context  # noqa: E402

CONFIG = """
whatsapp:
  home_channel:
    platform: whatsapp
    chat_id: '000@lid'
    name: Alex
"""


def _make_home(tmp: Path) -> Path:
    (tmp / "config.yaml").write_text(CONFIG, encoding="utf-8")
    mem = tmp / "memories"
    mem.mkdir()
    (mem / "USER.md").write_text("Alex prefers espresso and hates meetings.", encoding="utf-8")
    (mem / "MEMORY.md").write_text("- The boat is called Nimbus.", encoding="utf-8")
    return tmp


def _make_db(tmp: Path, rows) -> None:
    """rows: (session_id, source, display_name, archived, [(role, text, ts)])"""
    con = sqlite3.connect(tmp / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT, source TEXT, display_name TEXT, archived INT, started_at REAL)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    for sid, source, name, archived, msgs in rows:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (sid, source, name, archived, msgs[0][2]))
        for role, text, ts in msgs:
            con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                        (sid, role, text, ts))
    con.commit()
    con.close()


def setup_function(_fn):
    os.environ.pop("HERMES_HOME", None)


def test_picks_session_with_newest_message_not_newest_session():
    """A long-running owner thread must outrank a chat whose session merely
    started later — ordering by session start once loaded the wrong context."""
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        tmp = _make_home(Path(d))
        _make_db(tmp, [
            # started long ago, but spoke 10 seconds ago
            ("old-but-active", "whatsapp", "Alex Doe", 0,
             [("user", "morning", now - 86400), ("user", "call me", now - 10)]),
            # started recently, but silent for 20 minutes
            ("new-but-quiet", "whatsapp", "Sam Roe", 0,
             [("user", "hello", now - 1200)]),
        ])
        os.environ["HERMES_HOME"] = str(tmp)
        name, convo = context.recent_conversation()
        assert name == "Alex Doe"
        assert any("call me" in line for line in convo)


def test_cli_and_cron_sessions_are_ignored():
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        tmp = _make_home(Path(d))
        _make_db(tmp, [
            ("cli-1", "cli", "terminal", 0, [("user", "run tests", now - 5)]),
            ("wa-1", "whatsapp", "Alex Doe", 0, [("user", "hi there", now - 60)]),
        ])
        os.environ["HERMES_HOME"] = str(tmp)
        name, convo = context.recent_conversation()
        assert name == "Alex Doe"


def test_stale_conversation_is_dropped():
    old = time.time() - (context.MAX_SESSION_AGE_S + 600)
    with tempfile.TemporaryDirectory() as d:
        tmp = _make_home(Path(d))
        _make_db(tmp, [("wa-1", "whatsapp", "Alex Doe", 0, [("user", "ancient", old)])])
        os.environ["HERMES_HOME"] = str(tmp)
        _, convo = context.recent_conversation()
        assert convo == []


def test_owner_call_gets_memory():
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        tmp = _make_home(Path(d))
        _make_db(tmp, [("wa-1", "whatsapp", "Alex Doe", 0, [("user", "call me", now - 5)])])
        os.environ["HERMES_HOME"] = str(tmp)
        block = context.build(note="testing")
        assert "espresso" in block          # USER.md
        assert "Nimbus" in block            # MEMORY.md
        assert "call me" in block           # conversation
        assert "testing" in block           # note


def test_non_owner_call_gets_no_memory_and_a_warning():
    """The privacy gate: another household member must not receive the
    owner's profile or standing memory."""
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        tmp = _make_home(Path(d))
        _make_db(tmp, [("wa-2", "whatsapp", "Sam Roe", 0, [("user", "hey", now - 5)])])
        os.environ["HERMES_HOME"] = str(tmp)
        block = context.build()
        assert "espresso" not in block
        assert "Nimbus" not in block
        assert "not the owner" in block
        assert "hey" in block               # their own thread is fine


def test_missing_database_degrades_quietly():
    with tempfile.TemporaryDirectory() as d:
        tmp = _make_home(Path(d))          # no state.db written
        os.environ["HERMES_HOME"] = str(tmp)
        name, convo = context.recent_conversation()
        assert name is None and convo == []
        assert isinstance(context.build(), str)
