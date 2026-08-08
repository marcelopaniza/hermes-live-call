# live_call — Advanced

Configuration reference, the agent's tools, how a call is assembled, the
component map, and the design decisions behind it. For the pitch and quick
start, see the [main README](../README.md). For the threat model, see
[SECURITY.md](../SECURITY.md).

## How a call actually happens

```
chat: "call me"
  │
  ├─ agent calls call_start ──► service.py (inside the Hermes gateway)
  │      • mints a 128-bit single-use token
  │      • snapshots the caller's context to a 0600 file
  │      • retires any leftover room, waits for the port
  │      • spawns room_server.py in its own venv
  │      • resolves a public base URL
  │
  ◄─ returns https://<public>/join/<token>
  │
  └─ agent sends that link to the chat it came from

tap ─► join page (self-contained HTML)
        │ mic capture, PCM16 @ 16 kHz
        ▼
   WebSocket /ws ──► room_server.py ──► Pipecat pipeline ──► realtime model
        ▲                                     │
        └──────── PCM16 @ 24 kHz ◄────────────┘
                                              └─ transcript → workspace

hang up ─► pipeline cancelled ─► room self-closes ─► link is dead
```

## Configuration

Everything is environment-driven, read from the Hermes environment (put values
in `$HERMES_HOME/.env`). See [.env.example](../.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Realtime model key. Absent ⇒ echo pipeline. |
| `LIVE_CALL_PUBLIC_URL` | quick tunnel | Stable public base URL. Recommended for regular use. |
| `LIVE_CALL_BIND` | `127.0.0.1:8199` | Local listen address. Keep it on loopback. |
| `LIVE_CALL_MODEL` | `gemini-3.1-flash-live-preview` | Realtime model id. |
| `LIVE_CALL_PIPELINE` | auto | Force `echo` or `gemini`. Auto picks `gemini` when a key exists. |
| `LIVE_CALL_MAX_CALL_S` | `1800` | Hard cap on a single call. |
| `LIVE_CALL_SYSTEM_FILE` | — | Replace the assembled persona with a file. |
| `LIVE_CALL_CONTEXT` | `1` | `0` disables memory + conversation injection. |
| `LIVE_CALL_OWNER_CHAT_ID` | from config | Override which chat counts as the owner. |
| `LIVE_CALL_RECORDINGS_DIR` | `$HERMES_HOME/workspace/live_calls` | Transcripts, logs, room state. |
| `LIVE_CALL_PYTHON` | plugin `.venv` | Interpreter for the room server. |
| `LIVE_CALL_CLOUDFLARED` | `./bin`, then `PATH` | Path to the cloudflared binary. |

## Tools the agent gets

| Tool | Purpose |
|---|---|
| `call_start(mode, note?, ttl_minutes?)` | Start a room; returns a one-time join URL to send to the user. `mode` is `voice` (default) or `video`. `note` is context for the call. `ttl_minutes` defaults to 20, max 60. |
| `call_status()` | Whether a call is live, whether the link was used, and where the transcript is. |
| `call_end()` | Tear down the room and invalidate the link. |

Ending a chat session does **not** end a call or invalidate an unused link —
sessions rotate far faster than a person picks up a phone.

## What the assistant knows on a call

The system prompt is assembled from four sources:

1. **Identity** — `SOUL.md` plus the *active* personality from `config.yaml`
   (`display.personality` names a key in a `personalities` map). Where the two
   disagree about who the assistant is, the personality wins — otherwise a
   generic vendor prompt overrides the character the user actually talks to.
2. **Memory** — `memories/USER.md` and `memories/MEMORY.md`.
3. **The conversation that requested the call** — the last ~24 turns of the chat
   session with the newest *message* (not the newest session; a long-running
   thread must not lose to one that merely started later).
4. **The note** the agent passed to `call_start`, if any.

Plus voice-specific instructions: speak in short turns, never read out URLs or
code, expect to be interrupted.

### When context is captured, and why it matters

Context is snapshotted **when the link is minted**, not when it is tapped, and
written to a `0600` file the room reads at connect time.

That ordering is a security property, not an implementation detail. Resolving it
at tap time meant taking whichever chat had spoken most recently — so a
non-owner's link, tapped after the owner happened to send a message, would open
the call with the *owner's* memory and recent conversation. Minting time is the
only moment the request can be attributed to a caller.

### Privacy model

A Hermes instance is often shared with a household. Memory is injected **only
when the requesting chat's stable platform id matches the configured home
channel** (`whatsapp.home_channel.chat_id`, or `LIVE_CALL_OWNER_CHAT_ID`), and it
fails closed when that id is unknown. Anyone else gets their own conversation
plus an explicit instruction not to disclose the owner's information.

A display *name* is never used for this — it is chosen by the person on the
other end, so matching on it would let anyone claim the owner's memory by
renaming themselves.

All reads are read-only (`mode=ro` + `PRAGMA query_only`), and any failure
degrades to less context rather than a failed call.

## Swapping the model

Default is Gemini Live: currently the only major realtime API with a native live
*video* input path (which the planned camera feature needs), and it publishes
per-minute pricing.

To use something else, edit `_build_processors` in `room_server.py` — a short
function returning a Pipecat processor list. Pipecat supports OpenAI Realtime,
AWS Nova Sonic, xAI, Ultravox and others.

> Gemini Live model IDs are **not** returned by the `ListModels` endpoint. An
> empty listing does not mean your key lacks access — open a Live session to
> check. (This cost an hour once.)

## Components

| File | Role |
|---|---|
| `__init__.py` | Plugin registration (`register(ctx)`), tools, session hook. |
| `tools.py` | Agent-facing tool schemas and handlers. |
| `service.py` | Runs **inside the gateway**: tokens, room lifecycle, context snapshot, tunnel selection, orphan adoption. Dependency-free by design. |
| `room_server.py` | **Subprocess**: join page, audio WebSocket, Pipecat pipeline, transcript, self-teardown. |
| `serializer.py` | Wire format — raw PCM16 binary both ways, JSON control channel (reserved for camera frames). |
| `persona.py` | Builds the call's identity from the assistant's own configuration. |
| `context.py` | Memory + recent-conversation injection, with the owner gate. |
| `tunnel_supervisor.py` | systemd service keeping one public tunnel up and publishing its URL. |
| `web/index.html` | The join page — self-contained, no external resources. |

The split matters: the room server runs in **its own virtualenv**, so pipecat and
the audio stack never enter the agent's environment. The gateway side imports
nothing heavier than the standard library.

## Reachability, in detail

The room binds to loopback. A public entry point is chosen in this order:

1. **`LIVE_CALL_PUBLIC_URL`** — a stable URL you control (named tunnel, reverse
   proxy). Recommended for anything regular.
2. **The tunnel supervisor's published URL** — one long-lived Cloudflare quick
   tunnel, its hostname written to `tunnel_url.txt` and re-read per call.
3. **A per-call tunnel** — last resort, because a tunnel started inside an agent
   turn dies when that turn ends, leaving the user holding a dead link.

Quick tunnels are Cloudflare's testing tier: the hostname changes on restart,
they cap at 200 concurrent in-flight requests, and creating many in a short
period gets the source IP rate-limited (HTTP 429 / error 1015). The supervisor
detects that, withholds the URL rather than publishing a dead hostname, and
backs off — so the agent reports an actionable error instead of handing out a
link that cannot resolve.

## Why WebSocket audio instead of WebRTC

The first implementation used peer-to-peer WebRTC. It failed in the field: a
phone on mobile data and a server behind home NAT never established a media
path, and the public TURN relay tried as a fallback allocated no relay
candidates at all.

Moving audio onto the same WSS connection that already serves the page removed
ICE, TURN and NAT from the problem entirely, at the cost of roughly 50–150 ms of
added latency — invisible next to model response time. It is the same approach
telephony media-stream APIs use.

## Development

```bash
python run_tests.py              # hermetic suite; no network, no pytest needed
python run_tests.py context      # one file
.venv/bin/python tests/verify_live_call.py    # end-to-end: real room + audio round-trip
```

`tests/ws_client.py` is a headless caller: it streams a tone into a live room and
measures what comes back, which is how the audio path is verified without a
phone. `run_tests.py` exists because the plugin is usually deployed on a bare
interpreter where pytest isn't present (pytest works too).

## Troubleshooting

| Symptom | Cause |
|---|---|
| Agent says no public link is available | Tunnel has no healthy URL — check `systemctl --user status hermes-live-call-tunnel`, or set `LIVE_CALL_PUBLIC_URL`. |
| Link opens but the page says the room rejected it | The link was already used, or it expired. Ask for a new one. |
| You hear your own voice instead of the assistant | No model key — the echo pipeline is running. Add `GEMINI_API_KEY`. |
| Call connects but nobody speaks | Check the room log in the recordings directory; a model auth failure appears there. |
| Assistant answers as generic Hermes | The active personality isn't being found — check `display.personality` in `config.yaml`, or set `LIVE_CALL_SYSTEM_FILE`. |
