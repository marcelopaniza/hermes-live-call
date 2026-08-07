"""room_server — the live_call media server. Runs as a subprocess in its own
venv (spawned by service.py). Owns: join page, the audio
WebSocket, healthz, control-stop, and the Pipecat pipeline.

Transport choice (learned the hard way, 2026-08-06 field tests): audio rides
the **WebSocket**, not peer-to-peer WebRTC. A phone on mobile/hotel networks
and a server behind home NAT cannot open a direct media path, and public TURN
relays are unreliable — but WSS through the existing HTTPS tunnel always
works. Same pattern as Twilio Media Streams. Trade-off: ~50-150 ms extra
latency vs P2P, invisible next to model response time.

Env contract (set by service.py):
  LIVE_CALL_TOKEN            single-use join token (required)
  LIVE_CALL_MODE             voice | video
  LIVE_CALL_BIND             host:port (default 127.0.0.1:8199)
  LIVE_CALL_CONTROL_SECRET   shared secret for /control/stop
  LIVE_CALL_PIPELINE         echo | gemini
  LIVE_CALL_MODEL            Gemini Live model id
  LIVE_CALL_NOTE             optional context from the requesting chat
  LIVE_CALL_SYSTEM_FILE      optional persona/system prompt file
  LIVE_CALL_TTL_S            link TTL for the unused-link watchdog
  GEMINI_API_KEY             required for the gemini pipeline
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from pipecat.frames.frames import (
    InputAudioRawFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from serializer import RawPCMSerializer

WEB_DIR = Path(__file__).parent / "web"

TOKEN = os.environ.get("LIVE_CALL_TOKEN", "")
MODE = os.environ.get("LIVE_CALL_MODE", "video")
BIND = os.environ.get("LIVE_CALL_BIND", "127.0.0.1:8199")
SECRET = os.environ.get("LIVE_CALL_CONTROL_SECRET", "")
PIPELINE = os.environ.get("LIVE_CALL_PIPELINE", "echo")
MODEL = os.environ.get("LIVE_CALL_MODEL", "gemini-3.1-flash-live-preview")
NOTE = os.environ.get("LIVE_CALL_NOTE", "")
TTL_S = int(os.environ.get("LIVE_CALL_TTL_S", "0") or 0)
# Identity so a caller-side health probe can tell THIS room from an orphan
# still holding the port (an orphan once made a fresh link 404).
INSTANCE = os.environ.get("LIVE_CALL_INSTANCE") or uuid.uuid4().hex
STATE_FILE = Path(os.environ.get("LIVE_CALL_RECORDINGS_DIR", ".")) / "room.json"

# Gemini Live speaks 16 kHz in / 24 kHz out; echo keeps one rate both ways.
IN_RATE = 16000
OUT_RATE = 16000 if PIPELINE == "echo" else 24000

STATE = {
    "started": time.time(),
    "joined": False,
    "used": False,
    "stopping": False,
    "recording_path": None,   # TODO: MP4 recording
    "transcript_path": None,
    "images": 0,
}

_stop_event: asyncio.Event | None = None
_call_task: asyncio.Task | None = None


def log(msg: str) -> None:
    print(f"[room_server {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _persona() -> str:
    """Same identity as the chat assistant (SOUL.md + active personality)."""
    import persona as persona_mod

    text = persona_mod.build(mode=MODE, note=NOTE)
    name, _ = persona_mod.active_personality()
    log(f"persona: {name or 'default'} ({len(text)} chars)")
    return text


class EchoProcessor(FrameProcessor):
    """Keyless verification pipeline: loop caller audio straight back."""

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=frame.audio,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                )
            )
        else:
            await self.push_frame(frame, direction)


class TranscriptWriter(FrameProcessor):
    """Append both sides of the conversation to a per-call transcript file.

    Assistant speech arrives as many small TTSTextFrames; they are buffered and
    flushed on the next user turn (or at teardown) so the file reads as turns,
    not word salad.
    """

    def __init__(self, path: Path, speaker: str = "Assistant"):
        super().__init__()
        self._path = path
        self._speaker = speaker
        self._pending: list[str] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# live_call transcript — started {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n",
                        encoding="utf-8")

    def _write(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def flush_assistant(self) -> None:
        if self._pending:
            self._write(f"**{self._speaker}:** {''.join(self._pending).strip()}\n")
            self._pending.clear()

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            self.flush_assistant()
            self._write(f"**You:** {frame.text.strip()}\n")
        elif isinstance(frame, TTSTextFrame) and frame.text:
            self._pending.append(frame.text)
        await self.push_frame(frame, direction)


def _build_processors(transport: FastAPIWebsocketTransport, ctx: dict) -> list:
    if PIPELINE == "echo":
        return [transport.input(), EchoProcessor(), transport.output()]

    if PIPELINE == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set but LIVE_CALL_PIPELINE=gemini")
        from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

        kwargs: dict = {"api_key": api_key, "system_instruction": _persona()}
        if hasattr(GeminiLiveLLMService, "Settings"):
            kwargs["settings"] = GeminiLiveLLMService.Settings(model=MODEL)
        else:
            kwargs["model"] = MODEL
        llm = GeminiLiveLLMService(**kwargs)

        greeting = (
            "Greet the user in one short sentence, in character and using your own "
            "name, then ask what they need. Keep it under four seconds of speech."
        )
        context = LLMContext(messages=[{"role": "user", "content": greeting}])
        agg = LLMContextAggregatorPair(context)

        transcript_path = Path(
            os.environ.get("LIVE_CALL_RECORDINGS_DIR", ".")
        ) / f"call-{time.strftime('%Y%m%d-%H%M%S')}.md"
        import persona as persona_mod

        pname, _ = persona_mod.active_personality()
        writer = TranscriptWriter(transcript_path, speaker=(pname or "assistant").capitalize())
        ctx["transcript"] = writer
        STATE["transcript_path"] = str(transcript_path)

        # TODO: forward camera frames (serializer.on_image) to the model.
        return [transport.input(), agg.user(), llm, writer, transport.output(), agg.assistant()]

    raise RuntimeError(f"unknown LIVE_CALL_PIPELINE={PIPELINE!r}")


async def _run_call(websocket: WebSocket) -> None:
    serializer = RawPCMSerializer()

    def _on_image(_msg) -> None:
        STATE["images"] += 1   # TODO: route to model / recorder

    serializer.on_image = _on_image
    ctx: dict = {}

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
            session_timeout=None,
        ),
    )

    task = PipelineTask(
        Pipeline(_build_processors(transport, ctx)),
        params=PipelineParams(
            allow_interruptions=True,
            audio_in_sample_rate=IN_RATE,
            audio_out_sample_rate=OUT_RATE,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def _on_conn(_t, _ws):
        STATE["joined"] = True
        log("client connected — audio flowing")
        if PIPELINE == "gemini":
            # Speak first: proves the model→caller audio path immediately and
            # is better UX than a silent line waiting to be talked at.
            await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disc(_t, _ws):
        # Hang-up must END the pipeline. Without this the room outlives the
        # call and its link stays live (caught by the end-to-end verify).
        STATE["joined"] = False
        log("client disconnected — ending pipeline")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    log(f"pipeline starting (kind={PIPELINE}, mode={MODE}, {IN_RATE}->{OUT_RATE} Hz)")
    try:
        await runner.run(task)
    finally:
        writer = ctx.get("transcript")
        if writer:
            writer.flush_assistant()
            log(f"transcript: {STATE.get('transcript_path')}")
        log("pipeline finished")


# ---------------------------------------------------------------------------
# HTTP / WS
# ---------------------------------------------------------------------------

app = FastAPI()


@app.get("/join/{token}")
async def join(token: str):
    if STATE["stopping"] or token != TOKEN:
        return PlainTextResponse("no such room (link expired or already used)", status_code=404)
    if STATE["used"] and not STATE["joined"]:
        return PlainTextResponse("this link was already used", status_code=410)
    return FileResponse(WEB_DIR / "index.html")


@app.get("/config")
async def config(token: str = ""):
    if token != TOKEN:
        return JSONResponse({"error": "bad token"}, status_code=403)
    return {"mode": MODE, "inRate": IN_RATE, "outRate": OUT_RATE, "pipeline": PIPELINE}


@app.get("/healthz")
async def healthz():
    # Publicly reachable through the tunnel — no filesystem paths here.
    return {
        "ok": True,
        "instance": INSTANCE,
        "mode": MODE,
        "pipeline": PIPELINE,
        "transport": "websocket",
        "joined": STATE["joined"],
        "used": STATE["used"],
        "images": STATE["images"],
        "age_s": int(time.time() - STATE["started"]),
    }


@app.post("/control/stop")
async def control_stop(request: Request):
    if not SECRET or request.headers.get("x-control-secret") != SECRET:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    STATE["stopping"] = True
    log("stop requested via control endpoint")
    if _stop_event:
        _stop_event.set()
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    global _call_task
    token = websocket.query_params.get("token", "")
    if token != TOKEN or STATE["stopping"]:
        await websocket.close(code=4403)
        return
    if _call_task is not None and not _call_task.done():
        await websocket.close(code=4409)   # a caller is already connected
        return

    await websocket.accept()
    STATE["used"] = True
    _call_task = asyncio.create_task(_run_call(websocket))
    try:
        await _call_task
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001 — never kill the server on a call error
        log(f"call error: {type(e).__name__}: {e}")
    finally:
        STATE["joined"] = False
        # A finished call ends the room: single-use by design.
        if _stop_event:
            log("call over — room closing")
            _stop_event.set()


async def _watchdog() -> None:
    """Backstop reaper: unused link past its TTL, or a used room whose call is
    over (the /ws handler normally closes it; this catches the edge cases)."""
    while _stop_event and not _stop_event.is_set():
        await asyncio.sleep(10)
        if TTL_S and not STATE["used"] and time.time() - STATE["started"] > TTL_S:
            log("TTL expired unused — self-terminating")
            _stop_event.set()
        elif STATE["used"] and not STATE["joined"] and (_call_task is None or _call_task.done()):
            log("call finished — self-terminating")
            _stop_event.set()


def _write_state() -> None:
    """Leave a local record so a later process can find/stop this room."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "instance": INSTANCE,
            "pid": os.getpid(),
            "bind": BIND,
            "secret": SECRET,
            "started": STATE["started"],
        }), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"could not write state file: {e}")


def _clear_state() -> None:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if data.get("instance") == INSTANCE:   # never clobber a newer room
                STATE_FILE.unlink()
    except (OSError, ValueError):
        pass


async def amain() -> int:
    global _stop_event
    if not TOKEN:
        log("FATAL: LIVE_CALL_TOKEN missing")
        return 2
    _stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop_event.set)

    host, _, port = BIND.partition(":")
    server = uvicorn.Server(uvicorn.Config(
        app, host=host or "127.0.0.1", port=int(port or 8199),
        log_level="warning", access_log=False,
    ))
    # State must exist BEFORE the first health response: callers use it to find
    # and retire this room, and healthz starts answering the instant uvicorn
    # serves — a later write loses that race and leaves the room unstoppable.
    _write_state()
    serve_task = asyncio.create_task(server.serve())
    # Fail loudly if the port is already owned: a silent bind failure once let
    # call_start hand out a token no running room recognised (404 for the user).
    await asyncio.sleep(1.0)
    if serve_task.done():
        exc = serve_task.exception()
        log(f"FATAL: could not bind {BIND}: {exc}")
        _clear_state()
        return 3
    watchdog = asyncio.create_task(_watchdog())
    log(f"listening on {BIND} (instance={INSTANCE[:8]}, pipeline={PIPELINE}, mode={MODE})")

    await _stop_event.wait()
    watchdog.cancel()
    if _call_task and not _call_task.done():
        _call_task.cancel()
        try:
            await asyncio.wait_for(_call_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    _clear_state()
    log("clean shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
