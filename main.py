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

# ---------------- CONFIG ----------------
CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN", "")
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "0"))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
BOT_TAG = "[BOT1]"

assert CONTROLLER_TOKEN
assert COMMAND_CHANNEL_ID
assert TARGET_CHANNEL_ID

ROOT_DIR = Path(__file__).parent
_latest_screenshot: dict[str, str] = {"url": "", "message_id": "", "ts": ""}
STATE_FILE = ROOT_DIR / "receivers.json"
_receivers: dict[str, dict] = {}  # id -> {"name": str, "last_seen": float, "tag": str}
_selected_receiver: Optional[str] = None

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

_load_state()

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
    # Track latest screenshot posted by receiver
    if message.channel.id == TARGET_CHANNEL_ID and message.attachments:
        for att in message.attachments:
            name = (att.filename or "").lower()
            if name.endswith((".png", ".jpg", ".jpeg")):
                _latest_screenshot["url"] = att.url
                _latest_screenshot["message_id"] = str(message.id)
                _latest_screenshot["ts"] = str(message.created_at)
                break
    # Track receiver announcements
    if message.channel.id == COMMAND_CHANNEL_ID:
        if message.content.startswith(f"{BOT_TAG} ONLINE") or message.content.startswith(f"{BOT_TAG} PING"):
            parts = message.content.split(maxsplit=3)
            if len(parts) >= 4:
                receiver_id = parts[2].strip()
                tag = parts[3].strip()
                now = time.time()
                entry = _receivers.get(receiver_id, {"name": tag, "tag": tag, "last_seen": now})
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

# ---------------- FASTAPI ----------------
app = FastAPI(title="Controller")

CONTROL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Controller</title>
  <style>
    body { font-family: Arial, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }
    .card { max-width: 700px; background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 18px; box-shadow: 0 10px 30px rgba(0,0,0,0.35); margin-bottom: 16px; }
    h1 { margin: 0 0 12px; font-size: 20px; }
    label { display: block; margin-bottom: 8px; color: #cbd5e1; }
    input[type=file] { width: 100%; margin-bottom: 12px; color: #e2e8f0; }
    button { cursor: pointer; background: #22d3ee; color: #0b1224; border: none; border-radius: 8px; padding: 10px 14px; font-weight: 600; }
    button.secondary { background: #ef4444; color: #fff; }
    .row { display: flex; gap: 10px; align-items: center; }
    small { color: #94a3b8; }
    img { max-width: 100%; border-radius: 8px; border: 1px solid #1f2937; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Display Surprise</h1>
    <p style="margin: 4px 0 12px; color:#cbd5e1;">Sends local <code>suprise.png</code>/<code>surprise.png</code> and <code>sound.mp3</code> to the receiver and triggers display.</p>
    <div class="row">
      <form method="post" action="/ui/surprise">
        <button type="submit">Send & Display</button>
      </form>
      <form method="post" action="/ui/gif/stop">
        <button class="secondary" type="submit">Stop Display</button>
      </form>
    </div>
  </div>
  <div class="card">
    <h1>Select Receiver</h1>
    <div id="receiver-buttons" class="row" style="flex-wrap: wrap;"></div>
    <div id="receiver-offline" class="row" style="flex-wrap: wrap; margin-top:10px;"></div>
    <form method="post" action="/ui/rename" style="margin-top:12px; display:flex; gap:8px; align-items:center;">
      <input name="new_name" type="text" placeholder="Rename selected" required style="flex:1; padding:8px; border-radius:8px; border:1px solid #1f2937; background:#0b1224; color:#e2e8f0;">
      <button type="submit">Rename</button>
    </form>
  </div>
  <div class="card">
    <h1>Live Screen (5s)</h1>
    <p style="margin: 4px 0 12px; color:#cbd5e1;">Latest screenshot posted by receiver every 5 seconds.</p>
    <div id="screen-wrap">
      <img id="screen-img" src="" alt="Waiting for screenshot..." />
    </div>
    <div class="row" style="margin-top:10px;">
      <form method="post" action="/ui/live/start">
        <button type="submit">Start Live</button>
      </form>
      <form method="post" action="/ui/live/stop">
        <button class="secondary" type="submit">Stop Live</button>
      </form>
    </div>
  </div>
  <script>
    async function refreshReceivers() {
      try {
        const res = await fetch('/api/receivers');
        if (!res.ok) return;
        const data = await res.json();
        const wrap = document.getElementById('receiver-buttons');
        const offlineWrap = document.getElementById('receiver-offline');
        wrap.innerHTML = '';
        offlineWrap.innerHTML = '';
        (data.online || []).forEach(rec => {
          const form = document.createElement('form');
          form.method = 'post';
          form.action = '/ui/select/' + encodeURIComponent(rec.id);
          const btn = document.createElement('button');
          btn.type = 'submit';
          btn.textContent = rec.name || rec.tag;
          if (data.selected === rec.id) {
            btn.style.background = '#10b981';
            btn.style.color = '#0b1224';
          }
          form.appendChild(btn);
          wrap.appendChild(form);
        });
        (data.offline || []).forEach(rec => {
          const form = document.createElement('form');
          form.method = 'post';
          form.action = '/ui/select/' + encodeURIComponent(rec.id);
          const btn = document.createElement('button');
          btn.type = 'submit';
          btn.textContent = rec.name || rec.tag;
          btn.style.background = '#475569';
          btn.style.color = '#e2e8f0';
          form.appendChild(btn);
          offlineWrap.appendChild(form);
        });
      } catch (e) { /* ignore */ }
    }
    async function refreshScreen() {
      try {
        const res = await fetch('/api/screenshot');
        if (!res.ok) return;
        const data = await res.json();
        if (data.url) {
          const img = document.getElementById('screen-img');
          img.src = data.url + '?t=' + Date.now();
        }
      } catch (e) { /* ignore */ }
    }
    refreshReceivers();
    refreshScreen();
    setInterval(refreshReceivers, 5000);
    setInterval(refreshScreen, 4000);
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


# ---------------- COMMAND ENDPOINTS ----------------
async def _send_cmd(msg: str):
    await _controller_ready.wait()
    ch = await _get_channel(COMMAND_CHANNEL_ID)
    await ch.send(msg)

async def _send_cmd_with_files(msg: str, file_paths: list[Path]):
    await _controller_ready.wait()
    ch = await _get_channel(COMMAND_CHANNEL_ID)
    files = []
    for p in file_paths:
        try:
            files.append(discord.File(fp=str(p), filename=p.name))
        except Exception:
            continue
    if not files:
        await ch.send(f"{msg} (no files attached)")
        return
    await ch.send(content=msg, files=files)

def _cmd_with_selection(cmd: str) -> str:
    if _selected_receiver and _selected_receiver in _receivers:
        tag = _receivers[_selected_receiver].get("name") or _receivers[_selected_receiver].get("tag")
        return f"{BOT_TAG} {cmd} {tag} {_selected_receiver}"
    return f"{BOT_TAG} {cmd}"

@app.post("/cmd/gif/start")
async def gif_start():
    await _send_cmd(_cmd_with_selection("DISPLAY_GIF_START"))
    return JSONResponse({"ok": True})

@app.post("/cmd/gif/stop")
async def gif_stop():
    await _send_cmd(_cmd_with_selection("DISPLAY_GIF_STOP"))
    return JSONResponse({"ok": True})

@app.get("/api/screenshot")
async def api_screenshot():
    return JSONResponse(_latest_screenshot)

@app.get("/api/receivers")
async def api_receivers():
    now = time.time()
    online = []
    offline = []
    for rid, info in _receivers.items():
        data = {"id": rid, "name": info.get("name") or info.get("tag"), "tag": info.get("tag"), "last_seen": info.get("last_seen", 0)}
        if now - data["last_seen"] <= 120:
            online.append(data)
        else:
            offline.append(data)
    return JSONResponse({"online": online, "offline": offline, "selected": _selected_receiver})

@app.post("/ui/surprise")
async def ui_surprise():
    # Prefer suprise.png (existing file), fall back to surprise.png
    img = None
    for name in ("suprise.png", "surprise.png"):
        candidate = ROOT_DIR / name
        if candidate.is_file():
            img = candidate
            break
    sound = ROOT_DIR / "sound.mp3"

    files = [p for p in (img, sound) if p and p.is_file()]
    if not files:
        return HTMLResponse("Missing suprise.png/surprise.png and sound.mp3 next to main.py", status_code=400)

    await _send_cmd_with_files(_cmd_with_selection("DISPLAY_GIF_START"), files)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/gif/stop")
async def ui_gif_stop():
    await gif_stop()
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/live/start")
async def ui_live_start():
    await _send_cmd(_cmd_with_selection("LIVE_START"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/live/stop")
async def ui_live_stop():
    await _send_cmd(_cmd_with_selection("LIVE_STOP"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/select/{tag}")
async def ui_select(tag: str):
    global _selected_receiver
    if tag in _receivers:
        _selected_receiver = tag
        _save_state()
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/rename")
async def ui_rename(request: Request):
    global _selected_receiver
    new_name = None
    try:
        form = await request.form()
        new_name = form.get("new_name")
    except Exception:
        # fall back to query params if form parsing failed (e.g., multipart lib missing)
        new_name = request.query_params.get("new_name")
    if not new_name:
        new_name = request.query_params.get("new_name")

    if _selected_receiver and _selected_receiver in _receivers and new_name:
        _receivers[_selected_receiver]["name"] = new_name
        _save_state()
        await _send_cmd(f"{BOT_TAG} SET_NAME {_selected_receiver} {new_name}")
    return RedirectResponse(url="/", status_code=303)

# ---------------- STARTUP ----------------
@app.on_event("startup")
async def startup():
    async def run_bot():
        try:
            await bot.start(CONTROLLER_TOKEN)
        except Exception as e:
            print("Bot failed:", e)
    asyncio.create_task(run_bot())
