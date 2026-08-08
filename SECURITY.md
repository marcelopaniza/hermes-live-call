# Security

`live_call` puts a microphone endpoint on the public internet. This page states
plainly what it defends against, what it does not, and what was found and fixed
before release — so you can decide whether the trade is acceptable for you.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/marcelopaniza/hermes-live-call/security/advisories/new)
rather than a public issue. There is no bounty; this is a personal project.

## Threat model

The join page and the audio socket are reachable by anyone who learns the public
hostname, so assume the hostname is public knowledge. What must hold:

| Actor | Must not be able to |
|---|---|
| Anonymous internet visitor with no token | start a call, stop a call, learn who you are or what was said, or make your machine spend model credits |
| Someone who obtains an old or already-used link | start any call, or replay the link after it has been used |
| Another user of the same assistant (a household member with their own chat) | receive the owner's memory, profile, or conversations |
| A local unprivileged user on the host | read call transcripts or the room's control secret |

## What protects each of those

- **Join links** carry a 128-bit `secrets.token_urlsafe` token, are single-use
  (the used flag is checked by the audio socket, not just the page), expire on a
  TTL, and die with the room. Comparisons use `hmac.compare_digest`.
- **One room at a time**, reserved before the first `await`, so two callers
  cannot both be accepted on the same link.
- **Calls are capped** (`LIVE_CALL_MAX_CALL_S`, default 30 min) so a held-open
  line cannot run up model spend or squat the single room slot.
- **`/control/stop`** requires a 192-bit secret that never leaves the host, and
  additionally refuses any request arriving through the tunnel — so a stranger
  cannot use it to kill your calls even if the secret leaked.
- **`/healthz`** answers only `{"ok": true}` to public callers; room state is
  visible over loopback only. FastAPI's `/docs`, `/redoc` and `/openapi.json`
  are disabled.
- **Owner-only memory.** Memory is injected only when the requesting chat's
  *stable platform id* matches the configured home channel, and it fails closed
  when that id is unknown. The context is captured when the link is minted, so a
  link tapped later cannot pick up whoever happened to speak most recently.
- **At rest**, transcripts are `0600` inside a `0700` directory, and the room
  state file (which holds the control secret) is created `0600` atomically.
- **No shell, no traversal, no XSS.** Every subprocess call uses argv form with
  non-user-controlled executables; the join route's path parameter is compared
  for equality and never used to build a filesystem path; the page writes only
  through `textContent` and loads nothing from third parties.

## Found and fixed before release

An adversarial review (plus a second pass over the fixes) produced these. All
are corrected in the current release; they are listed because "we looked" is
worth less than "here is what looking found":

| Issue | Why it mattered |
|---|---|
| Owner memory was gated on a display **name** substring | A display name is chosen by the other party — renaming yourself would have unlocked the owner's profile |
| Context resolved at tap time, unbound to the caller | A non-owner's link, tapped after the owner messaged, would have opened with the owner's memory and recent messages |
| A used token could open a second call once the first ended | "Single-use" was not enforced where it mattered |
| Two callers could race one link | Both accepted, doubling cost and breaking the one-room invariant |
| `/control/stop` reachable from the internet | Free call-killer for anyone who knew the hostname |
| `/healthz` exposed room state publicly | Told strangers whether you were on a call |
| Token/secret compared with `!=` | Not constant-time |
| Calls had no maximum duration | Unbounded model spend, room squatted indefinitely |
| Transcripts inherited the umask; state file was chmod'd after creation | Locally readable private content, and a brief window on the control secret |
| Orphan cleanup signalled a stored pid unverified | Pid reuse could signal an unrelated process |

## Known limitations (accepted, not fixed)

- **The token is in the URL.** It therefore appears in browser history and in
  your tunnel provider's logs. That is inherent to a "tap a link" flow; the page
  itself loads no third-party resources, so it never leaks further.
- **`/healthz` is publicly reachable** (liveness only). The tunnel supervisor
  needs an unauthenticated endpoint to know the edge is alive.
- **Your tunnel provider is in the audio path.** With a Cloudflare tunnel, TLS
  terminates at Cloudflare's edge, so they are technically positioned to see
  traffic. A self-hosted reverse proxy with your own certificate avoids this.
- **The model provider processes your audio.** Unavoidable — it is the thing
  answering you. Nothing else is uploaded; transcripts stay local.
- **Dependencies are version-ranged, not hash-locked**, and `cloudflared` is
  pinned by version with its checksum printed at install rather than verified
  automatically.

## If you record

Transcripts are written for every call. Recording another person generally
requires their consent, and the rules differ by jurisdiction — that is your
responsibility, not the software's.
