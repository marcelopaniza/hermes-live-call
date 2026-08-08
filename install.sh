#!/usr/bin/env bash
# Install live_call into a Hermes deployment.
#
#   ./install.sh                 # venv + deps + optional tunnel service
#   ./install.sh --no-tunnel     # skip the tunnel service (you provide LIVE_CALL_PUBLIC_URL)
#
# Deliberately conservative: it never edits Hermes' config.yaml (use
# `hermes plugins enable live_call`) and never writes secrets.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PLUGIN_DIR/.venv"
WANT_TUNNEL=1
[[ "${1:-}" == "--no-tunnel" ]] && WANT_TUNNEL=0

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- interpreter -------------------------------------------------------------
# Match the interpreter Hermes itself runs on where possible; the room server
# needs 3.10+ for pipecat.
PY="${LIVE_CALL_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for cand in "$HERMES_HOME/hermes-agent/venv/bin/python" python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 || [[ -x "$cand" ]]; then
      ver="$("$cand" -c 'import sys;print("%d%d"%sys.version_info[:2])' 2>/dev/null || echo 0)"
      if [[ "$ver" -ge 310 ]]; then PY="$("$cand" -c 'import sys;print(sys.base_prefix+"/bin/python3")' 2>/dev/null || echo "$cand")"; break; fi
    fi
  done
fi
[[ -n "$PY" && -x "$PY" ]] || PY="$(command -v python3)"
[[ -n "$PY" ]] || die "no suitable python found (need 3.10+); set LIVE_CALL_PYTHON"
say "interpreter: $PY ($("$PY" --version 2>&1))"

# --- venv --------------------------------------------------------------------
say "creating venv at $VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
say "installing dependencies (this takes a few minutes)"
"$VENV/bin/pip" -q install -r "$PLUGIN_DIR/requirements.txt"
"$VENV/bin/python" -c 'import pipecat, fastapi, uvicorn; print("pipecat", pipecat.__version__)'

# --- cloudflared (optional) ---------------------------------------------------
CLOUDFLARED="$(command -v cloudflared || true)"
[[ -x "$PLUGIN_DIR/bin/cloudflared" ]] && CLOUDFLARED="$PLUGIN_DIR/bin/cloudflared"
if [[ $WANT_TUNNEL -eq 1 && -z "$CLOUDFLARED" ]]; then
  say "cloudflared not found — downloading to $PLUGIN_DIR/bin"
  mkdir -p "$PLUGIN_DIR/bin"
  arch="$(uname -m)"; case "$arch" in x86_64) a=amd64;; aarch64|arm64) a=arm64;; *) a="";; esac
  if [[ -n "$a" ]]; then
    # Pinned rather than "latest" so installs are reproducible; the checksum is
    # printed so you can compare it against Cloudflare's published release
    # before trusting a binary that then runs as a persistent service.
    ver="${CLOUDFLARED_VERSION:-2026.7.3}"
    curl -fsSL -o "$PLUGIN_DIR/bin/cloudflared" \
      "https://github.com/cloudflare/cloudflared/releases/download/$ver/cloudflared-linux-$a"
    chmod +x "$PLUGIN_DIR/bin/cloudflared"
    CLOUDFLARED="$PLUGIN_DIR/bin/cloudflared"
    say "cloudflared $ver sha256: $(sha256sum "$CLOUDFLARED" | cut -d" " -f1)"
    say "verify against https://github.com/cloudflare/cloudflared/releases/tag/$ver"
  else
    say "unknown arch $arch — install cloudflared yourself, or use --no-tunnel"
  fi
fi

# --- tunnel service (optional) ------------------------------------------------
if [[ $WANT_TUNNEL -eq 1 && -n "$CLOUDFLARED" ]]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  sed -e "s|@PYTHON@|$VENV/bin/python|g" \
      -e "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" \
      -e "s|@HERMES_HOME@|$HERMES_HOME|g" \
      "$PLUGIN_DIR/systemd/hermes-live-call-tunnel.service.in" \
      > "$UNIT_DIR/hermes-live-call-tunnel.service"
  systemctl --user daemon-reload
  systemctl --user enable --now hermes-live-call-tunnel.service
  say "tunnel service started; public URL will appear in"
  say "  $HERMES_HOME/workspace/live_calls/tunnel_url.txt"
fi

cat <<EOF

$(say "installed")

Next:
  1. Add your realtime-model key to $HERMES_HOME/.env:
       GEMINI_API_KEY=...            # never paste keys into a shell history
  2. Enable the plugin:
       hermes plugins enable live_call
  3. Restart the gateway:
       systemctl --user restart hermes-gateway.service
  4. Ask your assistant for a call.

Run the tests any time:  $VENV/bin/python $PLUGIN_DIR/run_tests.py
EOF
