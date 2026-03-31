"""
Germany.py — Browser Control Panel + Playwright lifecycle manager.

Architecture
────────────
FastAPI (uvicorn) runs the asyncio event loop.  Playwright's async API lives in
the SAME event loop, so /browser/start and /browser/stop are plain async
endpoints — no threads, no subprocess juggling, no zombie processes.

Ports
────────────
:5000  →  This control panel (FastAPI / uvicorn)
:8080  →  noVNC live browser stream (started by entrypoint.sh)

State machine
────────────
STOPPED ──[POST /browser/start]──► STARTING ──► RUNNING
RUNNING ──[POST /browser/stop ]──► STOPPING ──► STOPPED
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

# ── Configuration ─────────────────────────────────────────────────────────────
TARGET_URL = "https://www.umbreitshopsolution.de/"

CHROMIUM_ARGS = [
    "--no-sandbox",               # mandatory in Docker (no kernel namespace sandbox)
    "--disable-setuid-sandbox",   # same reason
    "--disable-dev-shm-usage",    # /dev/shm is tiny in Docker; redirect to /tmp
    "--disable-gpu",              # no GPU in a headless VM / container
    "--start-maximized",          # fill the Xvfb virtual display
]

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Global browser state ──────────────────────────────────────────────────────
# Guarded by _lock — prevents race conditions when the user clicks ON/OFF fast.
_lock: asyncio.Lock          # initialised inside lifespan (after the loop exists)
_pw:      Optional[Playwright]     = None
_browser: Optional[Browser]       = None
_context: Optional[BrowserContext] = None
_page:    Optional[Page]           = None
_status_message: str = "Browser is stopped."


def _is_running() -> bool:
    """Return True only when the browser process is alive and connected."""
    return _browser is not None and _browser.is_connected()


# ── Playwright helpers ────────────────────────────────────────────────────────

async def _launch() -> None:
    """Start Playwright → Chromium → BrowserContext → Page → TARGET_URL."""
    global _pw, _browser, _context, _page, _status_message

    _pw = await async_playwright().start()

    _browser = await _pw.chromium.launch(
        headless=False,          # must be False — noVNC streams the real window
        args=CHROMIUM_ARGS,
    )

    _context = await _browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )

    _page = await _context.new_page()

    log.info("Navigating to %s …", TARGET_URL)
    await _page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)

    _status_message = f"Running  →  {TARGET_URL}"
    log.info("Page loaded. Session is live — view on noVNC (:8080).")


async def _teardown() -> None:
    """Close handles in reverse-creation order: page → context → browser → pw."""
    global _pw, _browser, _context, _page, _status_message

    try:
        if _page and not _page.is_closed():
            await _page.close()
    except Exception:
        pass

    try:
        if _context:
            await _context.close()
    except Exception:
        pass

    try:
        if _browser and _browser.is_connected():
            await _browser.close()
    except Exception:
        pass

    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass

    _page = _context = _browser = _pw = None
    _status_message = "Browser is stopped."
    log.info("Browser session closed cleanly.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the lock after the event loop is running; tear down on exit."""
    global _lock
    _lock = asyncio.Lock()
    log.info("Control panel ready on :5000")
    yield
    # Container shutdown → close any open browser session gracefully
    if _is_running():
        log.info("Server shutting down — closing browser session …")
        await _teardown()


app = FastAPI(title="Browser Control Panel", lifespan=lifespan)


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/status")
async def status() -> JSONResponse:
    """Polled by the UI every 2 s to update the live indicator."""
    return JSONResponse({"running": _is_running(), "message": _status_message})


@app.post("/browser/start")
async def browser_start() -> JSONResponse:
    """Launch Chromium and navigate to TARGET_URL."""
    global _status_message
    async with _lock:
        if _is_running():
            return JSONResponse({"ok": False, "message": "Browser is already running."})
        try:
            _status_message = "Starting browser…"
            await _launch()
            return JSONResponse({"ok": True, "message": _status_message})
        except Exception as exc:
            log.exception("Failed to launch browser")
            await _teardown()           # clean up any half-open handles
            return JSONResponse(
                {"ok": False, "message": f"Launch error: {exc}"},
                status_code=500,
            )


@app.post("/browser/stop")
async def browser_stop() -> JSONResponse:
    """Safely close the active Playwright session."""
    global _status_message
    async with _lock:
        if not _is_running():
            return JSONResponse({"ok": False, "message": "Browser is not running."})
        try:
            _status_message = "Stopping browser…"
            await _teardown()
            return JSONResponse({"ok": True, "message": _status_message})
        except Exception as exc:
            log.exception("Failed to stop browser cleanly")
            return JSONResponse(
                {"ok": False, "message": f"Stop error: {exc}"},
                status_code=500,
            )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_CONTROL_PANEL_HTML)


# ── Control Panel HTML ────────────────────────────────────────────────────────
# Self-contained: no external JS frameworks, no build step.
# Polls /status every 2 s for a live indicator; disables buttons during
# in-flight requests to prevent double-clicks.
_CONTROL_PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Browser Control Panel</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet" />

  <style>
    /* ── Reset & base ───────────────────────────────────────────────── */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:          #08080f;
      --surface:     #0f0f1a;
      --surface-2:   #16162a;
      --border:      #1f1f3a;
      --border-hi:   #2e2e55;
      --text:        #c8ccd8;
      --text-muted:  #5a5e72;
      --text-dim:    #383c50;
      --green:       #00e87a;
      --green-dim:   #004d28;
      --green-glow:  rgba(0, 232, 122, 0.18);
      --red:         #ff3c5a;
      --red-dim:     #4d0018;
      --red-glow:    rgba(255, 60, 90, 0.18);
      --blue:        #4d9fff;
      --amber:       #ffb800;
      --font:        'IBM Plex Mono', 'Courier New', monospace;
    }

    html, body {
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }

    /* ── Layout ─────────────────────────────────────────────────────── */
    body {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 2rem;
    }

    /* Subtle grid background */
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(var(--border) 1px, transparent 1px),
        linear-gradient(90deg, var(--border) 1px, transparent 1px);
      background-size: 40px 40px;
      opacity: 0.35;
      pointer-events: none;
      z-index: 0;
    }

    .panel {
      position: relative;
      z-index: 1;
      width: 100%;
      max-width: 560px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 4px;
      overflow: hidden;
      box-shadow:
        0 0 0 1px var(--border-hi),
        0 24px 64px rgba(0,0,0,0.6);
    }

    /* ── Header bar ─────────────────────────────────────────────────── */
    .header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 14px 20px;
      background: var(--surface-2);
      border-bottom: 1px solid var(--border);
    }

    .header-dots { display: flex; gap: 6px; }
    .dot {
      width: 10px; height: 10px;
      border-radius: 50%;
      background: var(--border-hi);
    }

    .header-title {
      flex: 1;
      text-align: center;
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      color: var(--text-muted);
    }

    /* ── Body sections ──────────────────────────────────────────────── */
    .section {
      padding: 24px 28px;
      border-bottom: 1px solid var(--border);
    }
    .section:last-child { border-bottom: none; }

    .label {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 12px;
    }

    /* ── Status indicator ───────────────────────────────────────────── */
    .status-row {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .indicator {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      flex-shrink: 0;
      background: var(--text-dim);
      transition: background 0.3s, box-shadow 0.3s;
    }
    .indicator.on {
      background: var(--green);
      box-shadow: 0 0 0 3px var(--green-dim), 0 0 12px var(--green);
      animation: pulse-green 2s ease-in-out infinite;
    }
    .indicator.off {
      background: var(--red);
      box-shadow: 0 0 0 3px var(--red-dim);
    }

    @keyframes pulse-green {
      0%, 100% { box-shadow: 0 0 0 3px var(--green-dim), 0 0 12px var(--green); }
      50%       { box-shadow: 0 0 0 6px var(--green-dim), 0 0 20px var(--green); }
    }

    #status-text {
      font-size: 13px;
      color: var(--text);
      transition: color 0.3s;
    }

    /* ── Control buttons ────────────────────────────────────────────── */
    .btn-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }

    .btn {
      position: relative;
      padding: 16px 0;
      border: 1px solid transparent;
      border-radius: 3px;
      font-family: var(--font);
      font-size: 13px;
      font-weight: 600;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      cursor: pointer;
      transition:
        background 0.15s,
        border-color 0.15s,
        box-shadow 0.15s,
        opacity 0.15s,
        transform 0.1s;
    }

    .btn:active:not(:disabled) { transform: scale(0.97); }

    .btn-on {
      background: var(--green-dim);
      border-color: var(--green);
      color: var(--green);
    }
    .btn-on:hover:not(:disabled) {
      background: rgba(0, 232, 122, 0.15);
      box-shadow: 0 0 16px var(--green-glow), inset 0 0 12px var(--green-glow);
    }

    .btn-off {
      background: var(--red-dim);
      border-color: var(--red);
      color: var(--red);
    }
    .btn-off:hover:not(:disabled) {
      background: rgba(255, 60, 90, 0.15);
      box-shadow: 0 0 16px var(--red-glow), inset 0 0 12px var(--red-glow);
    }

    .btn:disabled {
      opacity: 0.35;
      cursor: not-allowed;
    }

    /* Spinner overlay on pending state */
    .btn .spinner {
      display: none;
      position: absolute;
      right: 14px;
      top: 50%;
      transform: translateY(-50%);
      width: 12px; height: 12px;
      border: 2px solid currentColor;
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
    }
    .btn.loading .spinner { display: block; }

    @keyframes spin { to { transform: translateY(-50%) rotate(360deg); } }

    /* ── Info cards ─────────────────────────────────────────────────── */
    .info-grid {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .info-row {
      display: flex;
      align-items: baseline;
      gap: 10px;
      padding: 8px 12px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 3px;
    }

    .info-key {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--text-muted);
      min-width: 64px;
      flex-shrink: 0;
    }

    .info-val {
      font-size: 12px;
      color: var(--text);
      word-break: break-all;
    }

    .info-val a {
      color: var(--blue);
      text-decoration: none;
    }
    .info-val a:hover { text-decoration: underline; }

    /* ── Activity log ───────────────────────────────────────────────── */
    #log-list {
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 4px;
      max-height: 120px;
      overflow-y: auto;
    }

    #log-list li {
      font-size: 11px;
      color: var(--text-muted);
      padding: 4px 8px;
      border-left: 2px solid var(--border-hi);
      line-height: 1.4;
    }

    #log-list li.new {
      color: var(--text);
      border-left-color: var(--blue);
    }

    /* ── Footer ─────────────────────────────────────────────────────── */
    .footer {
      padding: 10px 20px;
      background: var(--surface-2);
      border-top: 1px solid var(--border);
      font-size: 10px;
      color: var(--text-dim);
      display: flex;
      justify-content: space-between;
    }

    #poll-indicator {
      display: inline-block;
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--text-dim);
      margin-right: 5px;
      vertical-align: middle;
      transition: background 0.1s;
    }
    #poll-indicator.active { background: var(--amber); }
  </style>
</head>
<body>
  <div class="panel">

    <!-- Title bar -->
    <div class="header">
      <div class="header-dots">
        <div class="dot"></div>
        <div class="dot"></div>
        <div class="dot"></div>
      </div>
      <span class="header-title">Browser Control Panel</span>
    </div>

    <!-- Status -->
    <div class="section">
      <div class="label">Session Status</div>
      <div class="status-row">
        <div class="indicator" id="indicator"></div>
        <span id="status-text">Connecting…</span>
      </div>
    </div>

    <!-- Controls -->
    <div class="section">
      <div class="label">Controls</div>
      <div class="btn-row">
        <button class="btn btn-on" id="btn-on" onclick="doAction('start')" disabled>
          ▶ &nbsp;Launch
          <span class="spinner"></span>
        </button>
        <button class="btn btn-off" id="btn-off" onclick="doAction('stop')" disabled>
          ■ &nbsp;Kill
          <span class="spinner"></span>
        </button>
      </div>
    </div>

    <!-- Info -->
    <div class="section">
      <div class="label">Session Info</div>
      <div class="info-grid">
        <div class="info-row">
          <span class="info-key">Target</span>
          <span class="info-val">
            <a href="https://www.umbreitshopsolution.de/" target="_blank" rel="noreferrer">
              umbreitshopsolution.de
            </a>
          </span>
        </div>
        <div class="info-row">
          <span class="info-key">noVNC</span>
          <span class="info-val">
            <a id="vnc-link" href="#" target="_blank" rel="noreferrer">
              http://&lt;DROPLET_IP&gt;:8080/vnc.html
            </a>
          </span>
        </div>
        <div class="info-row">
          <span class="info-key">Panel</span>
          <span class="info-val">
            <a id="panel-link" href="#" target="_blank" rel="noreferrer">
              http://&lt;DROPLET_IP&gt;:5000
            </a>
          </span>
        </div>
      </div>
    </div>

    <!-- Activity log -->
    <div class="section">
      <div class="label">Activity Log</div>
      <ul id="log-list">
        <li>Waiting for first status poll…</li>
      </ul>
    </div>

    <!-- Footer -->
    <div class="footer">
      <span>germany-browser-panel v2</span>
      <span><span id="poll-indicator"></span>polling every 2 s</span>
    </div>

  </div>

  <script>
    // ── State ─────────────────────────────────────────────────────────────────
    let _lastMessage = '';
    let _busy        = false;   // true while a start/stop request is in-flight

    // ── DOM refs ──────────────────────────────────────────────────────────────
    const indicator  = document.getElementById('indicator');
    const statusText = document.getElementById('status-text');
    const btnOn      = document.getElementById('btn-on');
    const btnOff     = document.getElementById('btn-off');
    const logList    = document.getElementById('log-list');
    const pollDot    = document.getElementById('poll-indicator');

    // ── Fill in the real host on first load ──────────────────────────────────
    (function patchLinks() {
      const host = window.location.hostname;
      document.getElementById('vnc-link').href = `http://${host}:8080/vnc.html`;
      document.getElementById('vnc-link').textContent = `http://${host}:8080/vnc.html`;
      document.getElementById('panel-link').href = `http://${host}:5000`;
      document.getElementById('panel-link').textContent = `http://${host}:5000`;
    })();

    // ── Activity log ──────────────────────────────────────────────────────────
    function appendLog(msg) {
      if (msg === _lastMessage) return;   // deduplicate
      _lastMessage = msg;

      const now  = new Date().toLocaleTimeString('en-GB', { hour12: false });
      const li   = document.createElement('li');
      li.textContent = `[${now}]  ${msg}`;
      li.classList.add('new');

      logList.prepend(li);

      // Remove 'new' highlight after 3 s
      setTimeout(() => li.classList.remove('new'), 3000);

      // Keep only the last 8 entries
      while (logList.children.length > 8) {
        logList.removeChild(logList.lastChild);
      }
    }

    // ── Apply status payload to the UI ────────────────────────────────────────
    function applyStatus(running, message) {
      // Indicator dot
      indicator.className = 'indicator ' + (running ? 'on' : 'off');

      // Status text
      statusText.textContent = message;

      // Button states — only re-enable when no request is in-flight
      if (!_busy) {
        btnOn.disabled  = running;    // can't start if already running
        btnOff.disabled = !running;   // can't stop if already stopped
      }

      appendLog(message);
    }

    // ── Poll /status every 2 s ────────────────────────────────────────────────
    async function poll() {
      pollDot.classList.add('active');
      try {
        const res  = await fetch('/status');
        const data = await res.json();
        applyStatus(data.running, data.message);
      } catch (e) {
        statusText.textContent = 'Control panel unreachable…';
      } finally {
        pollDot.classList.remove('active');
      }
    }

    poll();   // immediate first poll
    setInterval(poll, 2000);

    // ── Button action (start / stop) ──────────────────────────────────────────
    async function doAction(action) {
      if (_busy) return;
      _busy = true;

      const btn      = action === 'start' ? btnOn : btnOff;
      const endpoint = `/browser/${action}`;

      // Disable both buttons and show spinner on the active one
      btnOn.disabled  = true;
      btnOff.disabled = true;
      btn.classList.add('loading');

      try {
        const res  = await fetch(endpoint, { method: 'POST' });
        const data = await res.json();
        appendLog(data.message);
        // Immediately poll so the indicator updates without waiting 2 s
        await poll();
      } catch (e) {
        appendLog(`Network error: ${e.message}`);
      } finally {
        _busy = false;
        btn.classList.remove('loading');
        // poll() already re-evaluated button states
      }
    }
  </script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "Germany:app",
        host="0.0.0.0",
        port=5000,
        log_level="info",
        # reload=True is fine for local dev but must be False in Docker
        reload=False,
    )
