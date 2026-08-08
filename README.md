<p align="center">
  <img src="docs/banner.svg" alt="live_call — talk to your Hermes agent on a real voice call" width="100%">
</p>

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/install-2%20commands-3ddc84?style=flat-square" alt="install"></a>
  <img src="https://img.shields.io/badge/tests-22%20passing-3ddc84?style=flat-square" alt="tests">
  <img src="https://img.shields.io/badge/license-MIT-2f6fed?style=flat-square" alt="MIT">
  <img src="https://img.shields.io/badge/python-3.10%2B-2f6fed?style=flat-square" alt="python 3.10+">
</p>

# live_call — talk to your Hermes agent on a real voice call

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets your
assistant hand you a link and then *talk to you*.

```
you (in any chat): "call me"
        │
        ▼
agent calls call_start  ──►  room server starts on this machine
        │                    (Pipecat + a realtime speech model)
        ◄── "tap to join: https://…/join/<one-time-token>"
        │
      tap ▼
phone browser: mic ⇄ WSS ⇄ your machine ⇄ realtime model
                                   └─ transcript → your workspace
```

It answers **as your assistant** — same persona, same memory, and it already knows
what the chat was about, because the call is a continuation of it, not a fresh bot.

- **No app, no account, no signup.** The join page is a single self-contained HTML
  file served by your own machine. No CDN, no trackers, no third-party JS.
- **Audio over WebSocket, not P2P.** Works from any network that allows HTTPS —
  hotel Wi-Fi, mobile data, CGNAT — with no STUN/TURN to operate. (Same approach
  as telephony media-stream APIs.)
- **Single-use, expiring links.** One room at a time, 128-bit token, TTL, and the
  room tears itself down when the call ends.
- **Runs beside the gateway, not inside it.** The room server is a subprocess in
  its own virtualenv, so heavyweight audio dependencies never touch your agent's
  environment.

## Status

| Capability | State |
|---|---|
| Voice call, full duplex, barge-in | ✅ works |
| Persona = your configured assistant identity | ✅ works |
| Memory + the conversation that requested the call | ✅ works |
| Per-call markdown transcript | ✅ works |
| Camera vision (agent sees your camera live) | 🚧 planned — the page captures video, the model is not yet fed frames |
| Call recording to MP4 | 🚧 planned |
| Outbound calls (agent rings you) | ❌ not planned — the link *is* the ring |

Tested against Gemini Live (`gemini-3.1-flash-live-preview`). Any Pipecat
speech-to-speech service can be dropped in; see [Model](#model).

## Install

```bash
git clone https://github.com/marcelopaniza/hermes-live-call ~/.hermes/plugins/live_call
cd ~/.hermes/plugins/live_call
./install.sh                      # venv + deps (+ optional tunnel service)

# add your key to $HERMES_HOME/.env — never on a shell command line
hermes plugins enable live_call
systemctl --user restart hermes-gateway.service
```

Then ask your assistant to call you. Requires Python 3.10+ and a Hermes install.

Verify without a model key — the plugin falls back to an **echo** pipeline that
plays your own voice back, which isolates "is audio working?" from "is the model
working?":

```bash
LIVE_CALL_PIPELINE=echo .venv/bin/python run_tests.py
```

## How a call reaches your phone

The room server binds to loopback only. To reach it from a phone you need one
public entry point, in order of preference:

1. **Your own stable URL** — a named Cloudflare tunnel, a reverse proxy, anything
   that terminates TLS and forwards to `127.0.0.1:8199`. Set `LIVE_CALL_PUBLIC_URL`.
   **Recommended for regular use.**
2. **The bundled quick-tunnel supervisor** (default, zero config) — a systemd user
   service keeps one Cloudflare quick tunnel up and publishes its hostname to
   `tunnel_url.txt`, which the plugin reads per call. Convenient, but quick-tunnel
   hostnames are ephemeral: they change whenever the service restarts.
3. **Per-call tunnel** (last-resort fallback) — works, but a tunnel started inside
   an agent turn dies when that turn ends, so links can go stale.

> **Who sees what.** Your audio goes phone → tunnel/proxy → your machine → the
> model provider. If you use a Cloudflare tunnel, Cloudflare terminates TLS and is
> therefore in the path; a self-hosted proxy avoids that. The model provider
> necessarily processes the audio. Transcripts never leave your disk.

## Model

Default is Gemini Live, chosen because it is currently the only major realtime API
with a native live *video* input path (which the planned camera feature needs) and
it publishes per-minute pricing (~US$0.02–0.03/min for voice at time of writing).

To use a different realtime service, edit `_build_processors` in `room_server.py` —
it is a short function returning a Pipecat processor list, and Pipecat supports
OpenAI Realtime, AWS Nova Sonic, xAI, Ultravox and others.

> Gemini Live model IDs are **not** returned by the `ListModels` endpoint. An empty
> listing does not mean your key lacks access; open a Live session to check.

## Configuration

Everything is environment-driven; see [.env.example](.env.example). The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Realtime model key. Absent ⇒ echo mode. |
| `LIVE_CALL_PUBLIC_URL` | quick tunnel | Stable public base URL. |
| `LIVE_CALL_BIND` | `127.0.0.1:8199` | Local listen address. Keep on loopback. |
| `LIVE_CALL_MODEL` | `gemini-3.1-flash-live-preview` | Realtime model id. |
| `LIVE_CALL_PIPELINE` | auto | Force `echo` or `gemini`. |
| `LIVE_CALL_SYSTEM_FILE` | — | Replace the assembled persona. |
| `LIVE_CALL_CONTEXT` | `1` | Set `0` to disable memory/conversation injection. |
| `LIVE_CALL_RECORDINGS_DIR` | `$HERMES_HOME/workspace/live_calls` | Transcripts + logs. |

## Tools the agent gets

| Tool | Purpose |
|---|---|
| `call_start(mode, note?, ttl_minutes?)` | Start a room, return a one-time join URL to send to the user. |
| `call_status()` | Is a call live, has the link been used, where is the transcript. |
| `call_end()` | Tear down the room and invalidate the link. |

## What the assistant knows on a call

Assembled fresh when you *tap the link* (not when it was minted), so it is current:

1. **Identity** — `SOUL.md` plus the active personality from `config.yaml`. Where
   they disagree about who the assistant is, the personality wins; otherwise a
   generic base prompt overrides the character you actually talk to.
2. **Memory** — `memories/USER.md` and `memories/MEMORY.md`.
3. **The conversation that asked for the call** — the last turns of the chat
   session with the newest message, so the call continues the thread.
4. **The agent's own note**, if it passed one.

### Privacy model

A Hermes instance is often shared with a household. Memory is injected **only when
the requesting chat is the owner's**, matched against the gateway's configured home
channel. Anyone else gets their own conversation plus an explicit instruction not
to disclose the owner's information. All reads are read-only (`PRAGMA query_only`),
and any failure degrades to less context rather than a failed call.

## Security

This puts a microphone endpoint on the public internet, so the short version:
join links are single-use, expiring and 128-bit; only one call can run at a
time and it is capped at 30 minutes; the stop endpoint is unreachable from the
internet; memory is released only to the owner's own chat and is bound to the
caller when the link is minted; transcripts stay on your disk at `0600`.

Two things to know before you expose it: **the token travels in the URL** (so
it lands in browser history and your tunnel provider's logs), and **your tunnel
provider terminates TLS** unless you run your own proxy.

Full threat model, the findings from an adversarial review, and the accepted
limitations: **[SECURITY.md](SECURITY.md)**.

## Development

```bash
python run_tests.py              # hermetic suite, no network, no pytest needed
python run_tests.py context      # one file
.venv/bin/python tests/verify_live_call.py   # end-to-end: real room + WS client + audio
```

`tests/ws_client.py` is a headless caller: it streams a tone into a live room and
measures what comes back, which is how the audio path is verified without a phone.

## Architecture

| File | Role |
|---|---|
| `__init__.py` | Plugin registration (`register(ctx)`), tools, session hook. |
| `tools.py` | Agent-facing tool schemas + handlers. |
| `service.py` | Runs in the gateway: tokens, room lifecycle, tunnel selection, orphan adoption. |
| `room_server.py` | Subprocess: join page, audio WebSocket, Pipecat pipeline, transcript. |
| `serializer.py` | Wire format — raw PCM16 binary both ways, JSON control channel. |
| `persona.py` | Builds the call's system prompt from your assistant's identity. |
| `context.py` | Memory + recent-conversation injection, with the owner gate. |
| `tunnel_supervisor.py` | Keeps one public tunnel up and publishes its URL. |
| `web/index.html` | The join page — self-contained, no external resources. |

## Why WebSocket audio instead of WebRTC

The first implementation used peer-to-peer WebRTC. It failed in the field: a phone
on mobile data and a server behind home NAT never established a media path, and the
public TURN relay tried as a fallback allocated no relay candidates at all. Moving
audio onto the same WSS connection that already serves the page removed ICE, TURN
and NAT from the problem entirely, at the cost of ~50–150 ms of added latency —
invisible next to model response time.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Nous Research or Google.
