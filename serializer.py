"""RawPCMSerializer — the wire format between the browser page and Pipecat.

Binary WebSocket messages are raw little-endian PCM16 mono audio, in both
directions. Text messages are JSON control events (``{"t": "..."}"``) used for
camera frames and client signals; they never carry audio.

Why raw instead of pipecat's protobuf serializer: the browser side is a single
static HTML file with no build step and no SDK, so the page has to speak the
protocol with nothing but ``AudioContext`` and ``WebSocket``.

Sample rates are fixed by the pipeline's StartFrame (``setup``) so the browser
can be told exactly what to send and what it will receive.
"""

from __future__ import annotations

import json
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputImageRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class RawPCMSerializer(FrameSerializer):
    """PCM16 in binary frames; JSON in text frames."""

    def __init__(self) -> None:
        super().__init__()
        self._in_rate = 16000
        self._out_rate = 24000
        self.on_image = None  # set by room_server to capture camera frames

    @property
    def type(self) -> str:
        return "binary"

    @property
    def in_sample_rate(self) -> int:
        return self._in_rate

    @property
    def out_sample_rate(self) -> int:
        return self._out_rate

    async def setup(self, frame: StartFrame) -> None:
        self._in_rate = frame.audio_in_sample_rate
        self._out_rate = frame.audio_out_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            if not data:
                return None
            return InputAudioRawFrame(
                audio=bytes(data), sample_rate=self._in_rate, num_channels=1
            )

        # Text: JSON control channel.
        try:
            msg: dict[str, Any] = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None

        if msg.get("t") == "image" and self.on_image:
            # Camera frames arrive as base64 JPEG; hand off to the room
            # server, which owns model wiring and recording.
            self.on_image(msg)
        return None
