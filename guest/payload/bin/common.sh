#!/usr/bin/env bash
# Installed root-owned. This file deliberately contains no credentials.
set -euo pipefail
source /etc/onepve-agent/config.env
source /etc/onepve-agent/versions.env
STATE_DIR="$AGENT_HOME/.local/state/onepve-agent"
PROFILE_DIR="$AGENT_HOME/.local/share/onepve-agent/chromium"
FNM_DIR="$AGENT_HOME/.local/share/fnm"
FNM_BIN="$FNM_DIR/fnm"
NODE_VERSION_FILE=/etc/onepve-agent/node.version
MCP_CLI="$AGENT_HOME/.local/share/onepve-agent/mcp/node_modules/@playwright/mcp/cli.js"
MANUAL_LOCK="$STATE_DIR/manual.lock"
mkdir -p "$STATE_DIR"
if [[ $(id -un) != "$AGENT_USER" ]]; then
  printf 'Run this command as the configured agent account.\n' >&2
  exit 1
fi

node_exec() {
  "$FNM_BIN" --fnm-dir "$FNM_DIR" exec --using="$NODE_VERSION_FILE" "$@"
}

require_x11() {
  if [[ ${XDG_SESSION_TYPE:-x11} != x11 || -z ${DISPLAY:-} ]]; then
    printf 'An imported XFCE X11 session is required; run agent-session-start from that desktop.\n' >&2
    exit 1
  fi
  /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || {
    printf 'The imported X11 display is unavailable.\n' >&2
    exit 1
  }
}

mcp_permitted() {
  [[ ! -e "$MANUAL_LOCK" && ! -L "$MANUAL_LOCK" ]]
}

notify_desktop() {
  if command -v notify-send >/dev/null 2>&1; then
    notify-send 'Agent browser' "$1" >/dev/null 2>&1 || true
  fi
}
