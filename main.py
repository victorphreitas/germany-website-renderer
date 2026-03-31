"""
main.py — Secured Browser Control Panel + Playwright lifecycle manager.

Security model
──────────────
GET  /              → Requires ?key=<API_SECRET_KEY> query param.
                      On success, renders the control panel HTML with the key
                      embedded as a JS constant for subsequent API calls.
GET  /status        → Public (read-only; reveals no secrets).
POST /browser/start → Requires X-API-Key header (validated in JS from embedded key).
POST /browser/stop  → Requires X-API-Key header.

Configuration
──────────────
All values are read from environment variables (injected by Docker from .env).
The app refuses to start if any required variable is missing.

State machine
──────────────
STOPPED ──[POST /browser/start]──► STARTING ──► RUNNING
RUNNING ──[POST /browser/stop ]──► STOPPING ──► STOPPED
"""

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader, APIKeyQuery
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Settings (validated at import time) ──────────────────────────────────────

class Settings(BaseSettings):
    """
    Reads from environment variables.  Docker injects these from .env via
    the `env_file` key in docker-compose.yml.  Raises ValidationError on
    startup if any required variable is absent — fail-fast, no silent defaults
    for security-critical values.
    """
    DROPLET_PUBLIC_IP:  str
    CONTROL_PANEL_PORT: int = 5000
    VNC_PORT:           int = 8080
    TARGET_URL:         str
    API_SECRET_KEY:     str
    VNC_PASSWORD:       str   # consumed by entrypoint.sh; declared here for validation

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Container env vars take precedence over .env file values
        case_sensitive=True,
    )


settings = Settings()


# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Chromium launch arguments ─────────────────────────────────────────────────

CHROMIUM_ARGS = [
    "--no-sandbox",               # mandatory in Docker (no kernel namespace sandbox)
    "--disable-setuid-sandbox",   # same reason
    "--disable-dev-shm-usage",    # /dev/shm is tiny in Docker; redirect to /tmp
    "--disable-gpu",              # no GPU in a headless VM / container
    "--start-maximized",          # fill the Xvfb virtual display
]


# ── Global browser state ──────────────────────────────────────────────────────
# All mutations are guarded by _lock — prevents race conditions if the user
# clicks Launch/Kill rapidly or sends concurrent API requests.

_lock:    asyncio.Lock               # created inside lifespan (after the loop exists)
_pw:      Optional[Playwright]     = None
_browser: Optional[Browser]       = None
_context: Optional[BrowserContext] = None
_page:    Optional[Page]           = None
_status_message: str               = "Browser is stopped."


def _is_running() -> bool:
    """True only when the browser process is alive and connected."""
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

    log.info("Navigating to %s …", settings.TARGET_URL)
    await _page.goto(settings.TARGET_URL, wait_until="domcontentloaded", timeout=60_000)

    _status_message = f"Running  →  {settings.TARGET_URL}"
    log.info("Page loaded. Session is live — view on noVNC (:%s).", settings.VNC_PORT)


async def _teardown() -> None:
    """Close handles in reverse-creation order: page → context → browser → pw."""
    global _pw, _browser, _context, _page, _status_message

    for handle, close_fn in [
        (_page,    lambda: _page.close()    if _page and not _page.is_closed() else None),
        (_context, lambda: _context.close() if _context else None),
        (_browser, lambda: _browser.close() if _browser and _browser.is_connected() else None),
        (_pw,      lambda: _pw.stop()       if _pw else None),
    ]:
        if handle:
            try:
                await close_fn()
            except Exception:
                pass  # best-effort; we still null out the reference below

    _page = _context = _browser = _pw = None
    _status_message = "Browser is stopped."
    log.info("Browser session closed cleanly.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the lock after the event loop is running; clean up on exit."""
    global _lock
    _lock = asyncio.Lock()
    log.info(
        "Control panel ready → http://%s:%s/?key=<API_SECRET_KEY>",
        settings.DROPLET_PUBLIC_IP,
        settings.CONTROL_PANEL_PORT,
    )
    yield
    if _is_running():
        log.info("Server shutting down — closing browser session …")
        await _teardown()


app = FastAPI(title="Browser Control Panel", lifespan=lifespan)


# ── Auth dependency ───────────────────────────────────────────────────────────

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
_query_scheme  = APIKeyQuery(name="key",        auto_error=False)


async def require_api_key(
    header_key: Annotated[Optional[str], Security(_header_scheme)] = None,
    query_key:  Annotated[Optional[str], Security(_query_scheme)]  = None,
) -> None:
    """
    Accepts the secret via X-API-Key header (API/JS calls) OR ?key= query param
    (initial browser page load).  Uses secrets.compare_digest to prevent
    timing-based key enumeration attacks.
    """
    provided = header_key or query_key
    if not provided or not secrets.compare_digest(provided, settings.API_SECRET_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/status")
async def get_status() -> JSONResponse:
    """
    Polled every 2 s by the UI.  Intentionally public — reveals only a boolean
    running state and a human-readable message; no secrets.
    """
    return JSONResponse({"running": _is_running(), "message": _status_message})


@app.post("/browser/start")
async def browser_start(
    _: Annotated[None, Depends(require_api_key)],
) -> JSONResponse:
    """Launch Chromium and navigate to TARGET_URL.  Requires X-API-Key header."""
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
            await _teardown()   # clean up any half-open handles
            return JSONResponse(
                {"ok": False, "message": f"Launch error: {exc}"},
                status_code=500,
            )


@app.post("/browser/stop")
async def browser_stop(
    _: Annotated[None, Depends(require_api_key)],
) -> JSONResponse:
    """Safely close the active Playwright session.  Requires X-API-Key header."""
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
async def index(
    _: Annotated[None, Depends(require_api_key)],
) -> HTMLResponse:
    """
    Serves the control panel.  Requires ?key=<API_SECRET_KEY> on the URL.
    Embeds a CONFIG block into the HTML so the JS can send the key as a header
    on subsequent API calls — the key never needs to appear in a URL again.
    """
    config_script = (
        "<script>\n"
        "  // Server-injected runtime config — do not edit manually.\n"
        f"  window.__CONFIG__ = {{\n"
        f"    apiKey:    {json.dumps(settings.API_SECRET_KEY)},\n"
        f"    targetUrl: {json.dumps(settings.TARGET_URL)},\n"
        f"    vncPort:   {json.dumps(settings.VNC_PORT)},\n"
        f"    panelPort: {json.dumps(settings.CONTROL_PANEL_PORT)},\n"
        f"  }};\n"
        "</script>"
    )
    html = _CONTROL_PANEL_HTML.replace("<!-- __CONFIG__ -->", config_script)
    return HTMLResponse(html)


# ── Control Panel HTML template ───────────────────────────────────────────────
# <!-- __CONFIG__ --> is replaced at render time with a <script> block that
# injects window.__CONFIG__ containing the API key and runtime values.
# This means the key is embedded in HTML only after the caller has already
# proven they know the key (via the ?key= query param on GET /).

_CONTROL_PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Browser Control Panel</title>
  <!-- __CONFIG__ -->
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet" />

  <style>
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
      --orange:      #ff8c00;
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

    body {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 2rem;
    }

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
      box-shadow: 0 0 0 1px var(--border-hi), 0 24px 64px rgba(0,0,0,0.6);
    }

    /* ── Header ── */
    .header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 14px 20px;
      background: var(--surface-2);
      border-bottom: 1px solid var(--border);
    }
    .header-dots { display: flex; gap: 6px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--border-hi); }
    .header-title {
      flex: 1;
      text-align: center;
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      color: var(--text-muted);
    }

    /* ── Sections ── */
    .section { padding: 24px 28px; border-bottom: 1px solid var(--border); }
    .section:last-child { border-bottom: none; }
    .label {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 12px;
    }

    /* ── Status indicator ── */
    .status-row { display: flex; align-items: center; gap: 12px; }
    .indicator {
      width: 10px; height: 10px;
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
    #status-text { font-size: 13px; color: var(--text); transition: color 0.3s; }

    /* ── Buttons ── */
    .btn-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
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
      transition: background 0.15s, border-color 0.15s, box-shadow 0.15s, opacity 0.15s, transform 0.1s;
    }
    .btn:active:not(:disabled) { transform: scale(0.97); }
    .btn-on  { background: var(--green-dim); border-color: var(--green); color: var(--green); }
    .btn-off { background: var(--red-dim);   border-color: var(--red);   color: var(--red);   }
    .btn-on:hover:not(:disabled)  { background: rgba(0,232,122,0.15); box-shadow: 0 0 16px var(--green-glow), inset 0 0 12px var(--green-glow); }
    .btn-off:hover:not(:disabled) { background: rgba(255,60,90,0.15);  box-shadow: 0 0 16px var(--red-glow),   inset 0 0 12px var(--red-glow);   }
    .btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .btn .spinner {
      display: none;
      position: absolute;
      right: 14px; top: 50%;
      transform: translateY(-50%);
      width: 12px; height: 12px;
      border: 2px solid currentColor;
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
    }
    .btn.loading .spinner { display: block; }
    @keyframes spin { to { transform: translateY(-50%) rotate(360deg); } }

    /* ── Info cards ── */
    .info-grid { display: flex; flex-direction: column; gap: 8px; }
    .info-row {
      display: flex; align-items: baseline; gap: 10px;
      padding: 8px 12px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 3px;
    }
    .info-key {
      font-size: 10px; font-weight: 600; letter-spacing: 0.12em;
      text-transform: uppercase; color: var(--text-muted);
      min-width: 64px; flex-shrink: 0;
    }
    .info-val { font-size: 12px; color: var(--text); word-break: break-all; }
    .info-val a { color: var(--blue); text-decoration: none; }
    .info-val a:hover { text-decoration: underline; }

    /* ── Security badge ── */
    .badge {
      display: inline-flex; align-items: center; gap: 5px;
      font-size: 10px; font-weight: 600; letter-spacing: 0.1em;
      text-transform: uppercase;
      padding: 3px 8px;
      border-radius: 2px;
      background: rgba(255, 140, 0, 0.1);
      border: 1px solid rgba(255, 140, 0, 0.3);
      color: var(--orange);
    }

    /* ── Activity log ── */
    #log-list {
      list-style: none; display: flex; flex-direction: column; gap: 4px;
      max-height: 120px; overflow-y: auto;
    }
    #log-list li {
      font-size: 11px; color: var(--text-muted);
      padding: 4px 8px;
      border-left: 2px solid var(--border-hi);
      line-height: 1.4;
    }
    #log-list li.new { color: var(--text); border-left-color: var(--blue); }

    /* ── Footer ── */
    .footer {
      padding: 10px 20px; background: var(--surface-2);
      border-top: 1px solid var(--border);
      font-size: 10px; color: var(--text-dim);
      display: flex; justify-content: space-between;
    }
    #poll-indicator {
      display: inline-block; width: 6px; height: 6px;
      border-radius: 50%; background: var(--text-dim);
      margin-right: 5px; vertical-align: middle;
      transition: background 0.1s;
    }
    #poll-indicator.active { background: var(--amber); }
  </style>
</head>
<body>
  <div class="panel">

    <div class="header">
      <div class="header-dots">
        <div class="dot"></div><div class="dot"></div><div class="dot"></div>
      </div>
      <span class="header-title">Browser Control Panel</span>
      <span class="badge">&#x1F512; Secured</span>
    </div>

    <div class="section">
      <div class="label">Session Status</div>
      <div class="status-row">
        <div class="indicator" id="indicator"></div>
        <span id="status-text">Connecting…</span>
      </div>
    </div>

    <div class="section">
      <div class="label">Controls</div>
      <div class="btn-row">
        <button class="btn btn-on"  id="btn-on"  onclick="doAction('start')" disabled>
          &#x25B6;&nbsp; Launch<span class="spinner"></span>
        </button>
        <button class="btn btn-off" id="btn-off" onclick="doAction('stop')"  disabled>
          &#x25A0;&nbsp; Kill<span class="spinner"></span>
        </button>
      </div>
    </div>

    <div class="section">
      <div class="label">Session Info</div>
      <div class="info-grid">
        <div class="info-row">
          <span class="info-key">Target</span>
          <span class="info-val"><a id="target-link" href="#" target="_blank" rel="noreferrer"></a></span>
        </div>
        <div class="info-row">
          <span class="info-key">noVNC</span>
          <span class="info-val"><a id="vnc-link" href="#" target="_blank" rel="noreferrer"></a></span>
        </div>
        <div class="info-row">
          <span class="info-key">Panel</span>
          <span class="info-val"><a id="panel-link" href="#" target="_blank" rel="noreferrer"></a></span>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="label">Activity Log</div>
      <ul id="log-list"><li>Waiting for first status poll…</li></ul>
    </div>

    <div class="footer">
      <span>germany-browser-panel v3</span>
      <span><span id="poll-indicator"></span>polling every 2 s</span>
    </div>

  </div>

  <script>
    // ── Runtime config (injected by server at render time) ────────────────────
    const CFG = window.__CONFIG__;

    // ── DOM refs ──────────────────────────────────────────────────────────────
    const indicator  = document.getElementById('indicator');
    const statusText = document.getElementById('status-text');
    const btnOn      = document.getElementById('btn-on');
    const btnOff     = document.getElementById('btn-off');
    const logList    = document.getElementById('log-list');
    const pollDot    = document.getElementById('poll-indicator');

    // ── Populate info links from server-injected config ───────────────────────
    (function patchLinks() {
      const host = window.location.hostname;

      const targetLink = document.getElementById('target-link');
      targetLink.href        = CFG.targetUrl;
      targetLink.textContent = CFG.targetUrl;

      const vncHref = `http://${host}:${CFG.vncPort}/vnc.html`;
      const vncLink = document.getElementById('vnc-link');
      vncLink.href        = vncHref;
      vncLink.textContent = vncHref;

      const panelHref = `http://${host}:${CFG.panelPort}/?key=${encodeURIComponent(CFG.apiKey)}`;
      const panelLink = document.getElementById('panel-link');
      panelLink.href        = panelHref;
      panelLink.textContent = `http://${host}:${CFG.panelPort}/`;
    })();

    // ── Activity log ──────────────────────────────────────────────────────────
    let _lastMessage = '';

    function appendLog(msg) {
      if (msg === _lastMessage) return;
      _lastMessage = msg;
      const now = new Date().toLocaleTimeString('en-GB', { hour12: false });
      const li  = document.createElement('li');
      li.textContent = `[${now}]  ${msg}`;
      li.classList.add('new');
      logList.prepend(li);
      setTimeout(() => li.classList.remove('new'), 3000);
      while (logList.children.length > 8) logList.removeChild(logList.lastChild);
    }

    // ── Apply status payload ──────────────────────────────────────────────────
    let _busy = false;

    function applyStatus(running, message) {
      indicator.className = 'indicator ' + (running ? 'on' : 'off');
      statusText.textContent = message;
      if (!_busy) {
        btnOn.disabled  = running;
        btnOff.disabled = !running;
      }
      appendLog(message);
    }

    // ── Poll /status ──────────────────────────────────────────────────────────
    // /status is public — no key needed.  The key is only sent on mutations.
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

    poll();
    setInterval(poll, 2000);

    // ── Button action ─────────────────────────────────────────────────────────
    async function doAction(action) {
      if (_busy) return;
      _busy = true;

      const btn      = action === 'start' ? btnOn : btnOff;
      const endpoint = `/browser/${action}`;

      btnOn.disabled  = true;
      btnOff.disabled = true;
      btn.classList.add('loading');

      try {
        const res = await fetch(endpoint, {
          method: 'POST',
          headers: {
            // API key sent as a header on mutations — never in the URL again
            'X-API-Key': CFG.apiKey,
          },
        });

        if (res.status === 401) {
          appendLog('ERROR: Invalid API key — check your .env configuration.');
          return;
        }

        const data = await res.json();
        appendLog(data.message);
        await poll();   // refresh indicator immediately
      } catch (e) {
        appendLog(`Network error: ${e.message}`);
      } finally {
        _busy = false;
        btn.classList.remove('loading');
      }
    }
  </script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.CONTROL_PANEL_PORT,
        log_level="info",
        reload=False,
    )
