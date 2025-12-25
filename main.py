# main.py
import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import secrets
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
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
    .card { max-width: 500px; background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 18px; box-shadow: 0 10px 30px rgba(0,0,0,0.35); }
    h1 { margin: 0 0 12px; font-size: 20px; }
    label { display: block; margin-bottom: 8px; color: #cbd5e1; }
    input[type=file] { width: 100%; margin-bottom: 12px; color: #e2e8f0; }
    button { cursor: pointer; background: #22d3ee; color: #0b1224; border: none; border-radius: 8px; padding: 10px 14px; font-weight: 600; }
    button.secondary { background: #ef4444; color: #fff; }
    .row { display: flex; gap: 10px; align-items: center; }
    small { color: #94a3b8; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Display GIF</h1>
    <form method="post" action="/ui/gif" enctype="multipart/form-data">
      <label for="gif_file">Upload GIF/PNG</label>
      <input id="gif_file" name="gif_file" type="file" accept=".gif,.png" required />
      <div class="row">
        <button type="submit">Send to Receiver</button>
        <small>Sends Discord command with the file attached.</small>
      </div>
    </form>
    <form method="post" action="/ui/gif/stop" style="margin-top:12px;">
      <button class="secondary" type="submit">Stop Display</button>
    </form>
  </div>
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

@app.post("/cmd/gif/start")
async def gif_start():
    await _send_cmd(f"{BOT_TAG} DISPLAY_GIF_START")
    return JSONResponse({"ok": True})

@app.post("/cmd/gif/stop")
async def gif_stop():
    await _send_cmd(f"{BOT_TAG} DISPLAY_GIF_STOP")
    return JSONResponse({"ok": True})

@app.post("/ui/gif")
async def ui_gif(gif_file: UploadFile = File(...)):
    data = await gif_file.read()
    if not data:
        return HTMLResponse("Upload failed: empty file.", status_code=400)

    suffix = Path(gif_file.filename or "upload.gif").suffix or ".gif"
    tmp_dir = ROOT_DIR / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fname = f"gif_{secrets.token_hex(4)}{suffix}"
    tmp_path = tmp_dir / fname
    tmp_path.write_bytes(data)

    try:
        await _send_cmd_with_files(f"{BOT_TAG} DISPLAY_GIF_START", [tmp_path])
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    return RedirectResponse(url="/", status_code=303)

@app.post("/ui/gif/stop")
async def ui_gif_stop():
    await gif_stop()
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
