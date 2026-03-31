#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — start the full VNC + control panel stack
#
# Execution order:
#   1. Xvfb          — create the virtual display (:99)
#   2. Fluxbox        — attach a window manager to that display
#   3. x11vnc         — expose the display as a VNC stream on port 5900
#   4. websockify     — noVNC WebSocket bridge on port 8080
#   5. uvicorn        — FastAPI control panel on port 5000
#
# The Playwright browser is NOT started here.  Use the control panel (port 5000)
# to launch/kill the browser session on demand.
# ─────────────────────────────────────────────────────────────────────────────
set -e

DISPLAY_NUM=99
VNC_PORT=5900
NOVNC_PORT=8080
SCREEN_RESOLUTION="1280x900x24"   # width × height × colour-depth

export DISPLAY=":${DISPLAY_NUM}"

echo "[entrypoint] Starting Xvfb on display ${DISPLAY} (${SCREEN_RESOLUTION}) …"
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_RESOLUTION}" -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Give Xvfb a moment to initialise before attaching anything to it
sleep 1

echo "[entrypoint] Starting Fluxbox window manager …"
fluxbox &>/dev/null &

echo "[entrypoint] Starting x11vnc on port ${VNC_PORT} …"
x11vnc \
    -display "${DISPLAY}" \
    -nopw \
    -listen 0.0.0.0 \
    -port "${VNC_PORT}" \
    -forever \
    -shared \
    -rfbport "${VNC_PORT}" \
    &>/var/log/x11vnc.log &

# Allow x11vnc to bind before websockify tries to connect
sleep 1

echo "[entrypoint] Starting noVNC / websockify on port ${NOVNC_PORT} …"
# websockify bridges WebSocket clients (the browser) to the raw VNC TCP socket.
# --web points to the noVNC static HTML/JS files.
websockify \
    --web /usr/share/novnc/ \
    "${NOVNC_PORT}" \
    "localhost:${VNC_PORT}" \
    &>/var/log/websockify.log &

echo "[entrypoint] ─────────────────────────────────────────────"
echo "[entrypoint] noVNC ready.  Open in your browser:"
echo "[entrypoint]   http://<DROPLET_IP>:${NOVNC_PORT}/vnc.html"
echo "[entrypoint] ─────────────────────────────────────────────"

# Small pause so the VNC bridge is fully ready before accepting connections.
# The browser itself is NOT launched here — the control panel handles that.
sleep 2

echo "[entrypoint] ─────────────────────────────────────────────"
echo "[entrypoint] Control Panel: http://<DROPLET_IP>:5000"
echo "[entrypoint] noVNC stream:  http://<DROPLET_IP>:${NOVNC_PORT}/vnc.html"
echo "[entrypoint] ─────────────────────────────────────────────"
echo "[entrypoint] Starting FastAPI control panel (uvicorn) …"

# exec replaces this shell with uvicorn so SIGTERM/SIGINT are forwarded
# correctly — no orphan processes on container stop.
exec uvicorn Germany:app --host 0.0.0.0 --port 5000 --log-level info
