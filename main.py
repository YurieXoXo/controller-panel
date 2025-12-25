# main.py
import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


def ensure_dependencies():
    """
    Install the small set of third-party packages needed for the controller.
    """
    required = {
        "discord": "discord.py",
        "fastapi": "fastapi",
    }
    missing = []
    for module_name, package_name in required.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(package_name)
    if not missing:
        return
    print(f"Installing missing packages: {', '.join(missing)}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to install required packages: {exc}")
        sys.exit(1)


ensure_dependencies()

import discord
from discord.ext import commands
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# ====== CONFIG ======
CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN", "")
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "0"))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
BOT_TAG = os.getenv("BOT_TAG", "[BOT1]")
# =====================

assert CONTROLLER_TOKEN, "Set CONTROLLER_TOKEN env var"
assert COMMAND_CHANNEL_ID and TARGET_CHANNEL_ID, "Set COMMAND/TARGET channel IDs"

ROOT_DIR = Path(__file__).resolve().parent

# ---------- Discord controller bot ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)
_controller_ready = asyncio.Event()
_controller_error: Optional[str] = None


async def _get_channel(channel_id: int) -> discord.TextChannel:
    ch = bot.get_channel(channel_id)
    if ch is None:
        ch = await bot.fetch_channel(channel_id)
    return ch


async def _wait_controller_ready(timeout: float = 8.0):
    """
    Wait for the Discord controller bot to be ready; fail fast if it never connects.
    """
    if _controller_error:
        raise HTTPException(status_code=503, detail=f"Discord bot failed to start: {_controller_error}")
    if _controller_ready.is_set():
        return
    try:
        await asyncio.wait_for(_controller_ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        if _controller_error:
            raise HTTPException(status_code=503, detail=f"Discord bot failed to start: {_controller_error}")
        raise HTTPException(status_code=503, detail="Discord bot not connected yet.")
    if _controller_error:
        raise HTTPException(status_code=503, detail=f"Discord bot failed to start: {_controller_error}")


@bot.event
async def on_ready():
    print(f"Controller bot logged in as {bot.user}")
    global _controller_error
    _controller_error = None
    _controller_ready.set()


class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        msg = json.dumps(payload)
        dead = []
        async with self.lock:
            for ws in list(self.clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


hub = Hub()


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot and message.channel.id == TARGET_CHANNEL_ID:
        payload = {
            "author": str(message.author),
            "content": message.content or "",
            "ts": message.created_at.isoformat(),
        }
        await hub.broadcast(payload)


# ---------- FastAPI app ----------
app = FastAPI(title="GIF Controller")

PANEL_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>GIF Display Control</title>
  <style>
    *{box-sizing:border-box;}
    body{margin:0; font:16px/1.6 "Segoe UI",Arial,sans-serif; background:#0b0f1c; color:#e9eef7; display:flex; justify-content:center; align-items:center; min-height:100vh;}
    .card{width:440px; max-width:90vw; background:#12192b; border:1px solid #1e2942; border-radius:14px; padding:24px; box-shadow:0 20px 55px rgba(0,0,0,.35);}
    h1{margin:0 0 8px; font-size:22px;}
    p{margin:0 0 16px; color:#9fb0d2;}
    .buttons{display:flex; gap:12px; margin-bottom:18px;}
    button{flex:1; padding:12px 14px; border-radius:10px; border:1px solid #283556; background:#1a2540; color:#e9eef7; font-weight:700; cursor:pointer; transition:transform .08s ease, filter .16s ease;}
    button.primary{background:linear-gradient(120deg,#4ad9c8,#3f7bff); color:#061021; border:none;}
    button:hover{filter:brightness(1.05); transform:translateY(-1px);}
    #log{border:1px solid #1e2942; background:#0f1526; border-radius:10px; padding:12px; max-height:240px; overflow:auto; display:flex; flex-direction:column; gap:8px; font-family:Consolas,monospace; font-size:13px;}
    .status{display:flex; gap:8px; align-items:center; margin-bottom:14px;}
    .dot{width:10px; height:10px; border-radius:50%; background:#f5c04a;}
    .dot.ok{background:#5fe0c5;}
  </style>
</head>
<body>
  <div class="card">
    <h1>GIF Display</h1>
    <p>Send start/stop commands to the receiver and watch responses.</p>
    <div class="status"><span id="wsDot" class="dot"></span><span id="wsText">Connecting...</span></div>
    <div class="buttons">
      <button id="startBtn" class="primary">Start GIF display</button>
      <button id="stopBtn">Stop GIF display</button>
    </div>
    <div id="log"></div>
  </div>
  <script>
    const logBox = document.getElementById('log');
    const wsDot = document.getElementById('wsDot');
    const wsText = document.getElementById('wsText');
    function addLog(text){ const div=document.createElement('div'); div.textContent=text; logBox.prepend(div); while(logBox.children.length>120){ logBox.lastChild.remove(); } }
    async function call(path){ const res = await fetch(path,{method:'POST'}); const data = await res.json().catch(()=>({})); if(!res.ok){ throw new Error(data.error||'Request failed'); } return data; }
    document.getElementById('startBtn').onclick = ()=> call('/cmd/gif/start').then(()=> addLog('Start requested')).catch(e=> addLog('Error: '+e.message));
    document.getElementById('stopBtn').onclick = ()=> call('/cmd/gif/stop').then(()=> addLog('Stop requested')).catch(e=> addLog('Error: '+e.message));
    function connect(){ const ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://') + location.host + '/ws'); ws.onopen=()=>{ wsDot.className='dot ok'; wsText.textContent='Connected'; }; ws.onclose=()=>{ wsDot.className='dot'; wsText.textContent='Reconnecting...'; setTimeout(connect, 1200); }; ws.onmessage = ev => { try{ const obj=JSON.parse(ev.data); addLog(`${obj.ts || ''} ${obj.author||'bot'}: ${obj.content||''}`);}catch(_e){ addLog(ev.data);} }; }
    connect();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PANEL_HTML)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    try:
        await hub.connect(ws)
        await ws.send_text(json.dumps({"author": "Panel", "content": "Connected to controller.", "ts": ""}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(ws)
    except Exception:
        await hub.disconnect(ws)


# ---------- HTTP endpoints to send Discord commands ----------
async def _send_cmd(msg: str):
    await _wait_controller_ready()
    ch = await _get_channel(COMMAND_CHANNEL_ID)
    await ch.send(msg)


async def _send_cmd_with_files(msg: str, file_paths: list[Path]):
    await _wait_controller_ready()
    ch = await _get_channel(COMMAND_CHANNEL_ID)
    files = []
    handles = []
    try:
        for path in file_paths:
            handle = open(path, "rb")
            handles.append(handle)
            files.append(discord.File(handle, filename=path.name))
        await ch.send(msg, files=files)
    finally:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass


def _require_assets() -> list[str]:
    missing = []
    if not (ROOT_DIR / "suprise.png").is_file():
        missing.append("suprise.png")
    if not (ROOT_DIR / "sound.mp3").is_file():
        missing.append("sound.mp3")
    return missing


@app.post("/cmd/gif/start")
async def cmd_gif_start():
    missing = _require_assets()
    if missing:
        return JSONResponse({"ok": False, "error": f"Missing file(s): {', '.join(missing)} next to main.py"}, status_code=404)
    try:
        await _send_cmd_with_files(
            f"{BOT_TAG} DISPLAY_GIF_START",
            [ROOT_DIR / "suprise.png", ROOT_DIR / "sound.mp3"],
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True})


@app.post("/cmd/gif/stop")
async def cmd_gif_stop():
    try:
        await _send_cmd(f"{BOT_TAG} DISPLAY_GIF_STOP")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/health")
async def health():
    return {
        "ok": True,
        "controller_ready": _controller_ready.is_set(),
        "controller_error": _controller_error,
    }


# ---------- Run Discord bot in the same process ----------
@app.on_event("startup")
async def _startup():
    async def _run_bot():
        global _controller_error
        try:
            await bot.start(CONTROLLER_TOKEN)
        except Exception as exc:
            _controller_error = str(exc)
            _controller_ready.set()
    asyncio.create_task(_run_bot())


@app.on_event("shutdown")
async def _shutdown():
    await bot.close()
