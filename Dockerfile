# ─────────────────────────────────────────────────────────────────────────────
# Stage: runtime
#
# Stack:
#   Ubuntu 22.04
#   ├── Xvfb          — virtual framebuffer (the "screen" Chrome draws into)
#   ├── Fluxbox       — minimal window manager (keeps Chrome from crashing)
#   ├── x11vnc        — VNC server that mirrors the Xvfb display over TCP
#   ├── noVNC         — WebSockets ↔ VNC bridge served over HTTP (port 8080)
#   ├── Python 3      — runtime for main.py
#   ├── FastAPI       — control panel web server (port 5000)
#   └── Playwright    — browser automation (Chromium) — launched on demand
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

# Prevent apt from asking interactive questions during the build
ENV DEBIAN_FRONTEND=noninteractive

# ── System packages ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Virtual display + window manager
    xvfb \
    fluxbox \
    x11vnc \
    # noVNC dependencies
    novnc \
    websockify \
    # Python
    python3 \
    python3-pip \
    python3-venv \
    # Playwright system-level browser deps (Chromium)
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    # Utilities
    curl \
    wget \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Install the Chromium browser that Playwright manages
RUN playwright install chromium
# Install any remaining OS-level deps Playwright might need
RUN playwright install-deps chromium

# ── Copy application code ─────────────────────────────────────────────────────
COPY main.py .

# ── noVNC HTML client ─────────────────────────────────────────────────────────
# Ubuntu packages noVNC at /usr/share/novnc — we symlink the entrypoint for
# convenience so users can hit /vnc.html directly.
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# ── Entrypoint script ─────────────────────────────────────────────────────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ports below are documentation only — actual host bindings are set in
# docker-compose.yml using ${CONTROL_PANEL_PORT} and ${VNC_PORT} from .env.

# FastAPI control panel — access requires ?key=<API_SECRET_KEY>
EXPOSE 5000

# noVNC live browser stream — access requires VNC password set in .env
EXPOSE 8080

# Raw x11vnc VNC port (optional — for native VNC clients like RealVNC)
EXPOSE 5900

ENTRYPOINT ["/entrypoint.sh"]
