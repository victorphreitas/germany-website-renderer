#!/usr/bin/env bash
# =============================================================================
# entrypoint.sh — Start the full VNC + secured control panel stack.
#
# All configuration is read from environment variables injected by Docker
# from the .env file via docker-compose.yml.
#
# Startup order:
#   1. Validate required environment variables (fail fast on missing config)
#   2. Write the VNC password to a credential file (-rfbauth, never -passwd)
#   3. Xvfb          — virtual display (:99)
#   4. Fluxbox        — minimal window manager
#   5. x11vnc         — VNC server on port 5900 (with password auth)
#   6. websockify     — noVNC WebSocket bridge on ${VNC_PORT}
#   7. uvicorn        — FastAPI control panel on ${CONTROL_PANEL_PORT}
#
# The Playwright browser is NOT started here.
# Use the control panel to launch/kill the browser on demand.
# =============================================================================
set -euo pipefail

# ── Required environment variables ────────────────────────────────────────────
# Abort immediately with a clear message if any are missing.
: "${VNC_PASSWORD:?ERROR: VNC_PASSWORD is not set in .env}"
: "${CONTROL_PANEL_PORT:?ERROR: CONTROL_PANEL_PORT is not set in .env}"
: "${VNC_PORT:?ERROR: VNC_PORT is not set in .env}"
: "${API_SECRET_KEY:?ERROR: API_SECRET_KEY is not set in .env}"
: "${TARGET_URL:?ERROR: TARGET_URL is not set in .env}"

# ── Display configuration ─────────────────────────────────────────────────────
DISPLAY_NUM=99
RAW_VNC_PORT=5900             # internal x11vnc port; always 5900
SCREEN_RESOLUTION="1280x900x24"

export DISPLAY=":${DISPLAY_NUM}"

# ── VNC password file ─────────────────────────────────────────────────────────
# x11vnc -storepasswd writes a hashed credential file.
# Using -rfbauth <file> means the plaintext password is NEVER passed as a
# command-line argument (which would be visible in `ps aux`).
#
# VNC protocol (RFB) enforces an 8-character maximum — characters beyond
# position 8 are silently ignored during authentication.
VNC_PASSWD_FILE="/tmp/vnc_passwd"
echo "[entrypoint] Writing VNC credential file …"
x11vnc -storepasswd "${VNC_PASSWORD}" "${VNC_PASSWD_FILE}"
chmod 600 "${VNC_PASSWD_FILE}"

# ── Xvfb — virtual framebuffer ───────────────────────────────────────────────
echo "[entrypoint] Starting Xvfb on display ${DISPLAY} (${SCREEN_RESOLUTION}) …"
Xvfb "${DISPLAY}" \
    -screen 0 "${SCREEN_RESOLUTION}" \
    -ac +extension GLX +render -noreset \
    &

# Give Xvfb time to initialise before attaching a window manager
sleep 1

# ── Fluxbox — lightweight window manager ──────────────────────────────────────
echo "[entrypoint] Starting Fluxbox window manager …"
fluxbox &>/dev/null &

# ── x11vnc — VNC server (password-protected) ──────────────────────────────────
echo "[entrypoint] Starting x11vnc on port ${RAW_VNC_PORT} (password-protected) …"
x11vnc \
    -display "${DISPLAY}" \
    -rfbauth  "${VNC_PASSWD_FILE}" \
    -listen   0.0.0.0 \
    -rfbport  "${RAW_VNC_PORT}" \
    -forever \
    -shared \
    &>/var/log/x11vnc.log &

# Allow x11vnc to bind before websockify tries to connect
sleep 1

# ── websockify / noVNC — WebSocket bridge ─────────────────────────────────────
# Listens on ${VNC_PORT} (e.g. 8080), proxies to the raw VNC socket on 5900.
# The VNC-level password (set above) protects access through this bridge.
echo "[entrypoint] Starting noVNC / websockify on port ${VNC_PORT} …"
websockify \
    --web /usr/share/novnc/ \
    "${VNC_PORT}" \
    "localhost:${RAW_VNC_PORT}" \
    &>/var/log/websockify.log &

sleep 2

# ── Summary ───────────────────────────────────────────────────────────────────
echo "[entrypoint] ════════════════════════════════════════════════"
echo "[entrypoint]  Control Panel : http://<DROPLET_IP>:${CONTROL_PANEL_PORT}/?key=<API_SECRET_KEY>"
echo "[entrypoint]  noVNC Stream  : http://<DROPLET_IP>:${VNC_PORT}/vnc.html"
echo "[entrypoint]  VNC Password  : (set via VNC_PASSWORD in .env)"
echo "[entrypoint] ════════════════════════════════════════════════"

# ── uvicorn — FastAPI control panel ───────────────────────────────────────────
# exec replaces this shell with uvicorn so Docker's SIGTERM is forwarded
# directly — uvicorn triggers the FastAPI lifespan shutdown hook which closes
# any open browser session cleanly before the container exits.
echo "[entrypoint] Starting FastAPI control panel on port ${CONTROL_PANEL_PORT} …"
exec uvicorn main:app \
    --host      0.0.0.0 \
    --port      "${CONTROL_PANEL_PORT}" \
    --log-level info
