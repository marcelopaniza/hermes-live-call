"""Headless WebSocket call client — verifies the audio path without a phone.

Streams a 440 Hz PCM16 tone to /ws like the browser page does and measures the
audio energy coming back. With PIPELINE=echo the room must return the tone.

Usage:
  ../.venv/bin/python tests/ws_client.py --base http://127.0.0.1:8199 --token <tok>

Exit 0 = audio returned; 2 = connected but silence; 1 = error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import sys
import urllib.request

import websockets

CHUNK = 1024  # samples per message


def tone_chunk(start: int, rate: int) -> bytes:
    vals = []
    for i in range(CHUNK):
        t = (start + i) / rate
        vals.append(int(0.3 * 32767 * math.sin(2 * math.pi * 440 * t)))
    return struct.pack(f"<{CHUNK}h", *vals)


def rms(buf: bytes) -> float:
    if not buf:
        return 0.0
    n = len(buf) // 2
    vals = struct.unpack(f"<{n}h", buf[: n * 2])
    return math.sqrt(sum(v * v for v in vals) / n)


async def run(base: str, token: str, seconds: float) -> int:
    with urllib.request.urlopen(f"{base}/config?token={token}", timeout=10) as r:
        cfg = json.loads(r.read())
    in_rate = cfg["inRate"]
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?token={token}"

    got = {"msgs": 0, "rms_sum": 0.0}
    async with websockets.connect(ws_url, max_size=None) as ws:
        async def sender():
            sent = 0
            while sent / in_rate < seconds:
                await ws.send(tone_chunk(sent, in_rate))
                sent += CHUNK
                await asyncio.sleep(CHUNK / in_rate)   # pace like a real mic

        async def receiver():
            while True:
                msg = await ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    got["msgs"] += 1
                    got["rms_sum"] += rms(bytes(msg))

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        await send_task
        await asyncio.sleep(1.0)   # drain tail
        recv_task.cancel()

    avg = got["rms_sum"] / got["msgs"] if got["msgs"] else 0.0
    print(json.dumps({"msgs_back": got["msgs"], "avg_rms": round(avg, 1), "cfg": cfg}))
    if got["msgs"] == 0:
        print("FAIL: no audio came back", file=sys.stderr)
        return 2
    if avg < 50:
        print(f"FAIL: audio came back silent (avg_rms={avg:.1f})", file=sys.stderr)
        return 2
    print("PASS: round-trip audio over WebSocket confirmed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8199")
    ap.add_argument("--token", required=True)
    ap.add_argument("--seconds", type=float, default=4.0)
    a = ap.parse_args()
    try:
        return asyncio.run(run(a.base.rstrip("/"), a.token, a.seconds))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
