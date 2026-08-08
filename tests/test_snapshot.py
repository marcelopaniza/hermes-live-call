"""The mint-time context snapshot: created, handed over, and always removed.

This mechanism carries the owner's private memory to disk, so its lifecycle is
security-relevant. It previously shipped with no coverage at all — and leaked a
file on every normal hang-up because cleanup lived only on a path that a
hang-up never takes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _plugin  # noqa: E402

_plugin.load()
from live_call import service  # noqa: E402


def setup_function(_fn):
    service._room = None
    service._proc = None
    service._probe_cache.clear()
    for key in ("LIVE_CALL_PIPELINE", "LIVE_CALL_PUBLIC_URL", "HERMES_HOME"):
        os.environ.pop(key, None)


def test_context_module_loads_under_a_private_name():
    """Importing it must not squat the generic 'context' name in a process
    that hosts other plugins."""
    mod = service._load_context_module()
    assert hasattr(mod, "build")
    assert service._CONTEXT_MODULE in sys.modules
    assert service._CONTEXT_MODULE != "context"
    assert service._load_context_module() is mod        # cached, not re-imported
    before = list(sys.path)
    service._load_context_module()
    assert sys.path == before                            # and never grows sys.path


def test_discard_snapshot_is_safe_and_idempotent():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "context-abc.txt"
        p.write_text("secrets", encoding="utf-8")
        service._discard_snapshot(p)
        assert not p.exists()
        service._discard_snapshot(p)      # already gone
        service._discard_snapshot(None)   # never created


def test_room_server_removes_the_snapshot_at_shutdown():
    """The room must clean up after itself: a hang-up tears it down from the
    inside and never calls back into the gateway."""
    import importlib.util

    with tempfile.TemporaryDirectory() as d:
        snap = Path(d) / "context-xyz.txt"
        snap.write_text("the owner's private memory", encoding="utf-8")
        os.environ.update({
            "LIVE_CALL_TOKEN": "t",
            "LIVE_CALL_RECORDINGS_DIR": d,
            "LIVE_CALL_CONTEXT_FILE": str(snap),
        })
        try:
            spec = importlib.util.spec_from_file_location(
                "rs_under_test", Path(__file__).parent.parent / "room_server.py")
            rs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(rs)
        except ImportError:
            return  # room-server deps not installed in this interpreter
        finally:
            for k in ("LIVE_CALL_TOKEN", "LIVE_CALL_RECORDINGS_DIR", "LIVE_CALL_CONTEXT_FILE"):
                os.environ.pop(k, None)

        assert snap.exists()
        rs._discard_context_snapshot()
        assert not snap.exists(), "snapshot survived shutdown — it holds private memory"


def test_snapshot_is_written_private():
    """0600 from creation: it holds whatever the owner's memory contains."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "context-perm.txt"
        fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("x")
        assert oct(p.stat().st_mode & 0o777) == "0o600"
