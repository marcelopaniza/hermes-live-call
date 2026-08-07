"""End-to-end verification: spawn a real echo room via the service layer, connect a
headless WebSocket client, assert the tone comes back, and check that the room
is single-use and self-closing.

Run with the room-server venv python:
  ../.venv/bin/python tests/verify_live_call.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # hermes-calls/
sys.path.insert(0, str(Path(__file__).parent))                # tests/

os.environ["LIVE_CALL_PIPELINE"] = "echo"
os.environ.setdefault("LIVE_CALL_RECORDINGS_DIR",
                      str(Path(__file__).parent.parent / "recordings"))

from live_call import service, tools  # noqa: E402
from ws_client import run as ws_run   # noqa: E402


def main() -> int:
    out = json.loads(tools.handle_call_start({"mode": "voice", "ttl_minutes": 5}))
    if not out.get("success"):
        print("FAIL: call_start ->", out, file=sys.stderr)
        return 1
    print("room:", out["url"], "(pipeline:", out["pipeline"] + ")")
    token = out["url"].split("/join/")[1].split("?")[0]
    base = f"http://{os.environ.get('LIVE_CALL_BIND', '127.0.0.1:8199')}"

    rc = asyncio.run(ws_run(base, token, seconds=4.0))
    if rc != 0:
        tools.handle_call_end({})
        return rc

    # The room is single-use: it must tear itself down once the call ends.
    deadline = time.time() + 20
    st = {}
    while time.time() < deadline:
        time.sleep(1)
        st = json.loads(tools.handle_call_status({}))
        if not st.get("active"):
            break
    print("status after hangup:", st)
    if st.get("active"):
        print("FAIL: room still active 20s after the call ended", file=sys.stderr)
        tools.handle_call_end({})
        return 2
    print(f"room self-closed after the call ✅ ({int(20 - (deadline - time.time()))}s)")

    print("VERIFY: PASS — WebSocket round-trip audio confirmed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
