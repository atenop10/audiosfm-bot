#!/usr/bin/env python3
"""AUDIOS FM — Telegram bulk episode uploader.
Telegram -> download -> ffmpeg compress -> auto-rename -> Cloudflare R2 -> register on website.
"""
import asyncio, base64, json, mimetypes, os, re, shutil, subprocess, time, uuid, zipfile
from pathlib import Path

import html, threading
import requests
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import BotCommand, Message

load_dotenv(os.environ.get("ENV_FILE", ".env"))  # ENV_FILE lets one VPS run several bots from this code
ENV = os.environ
ADMINS = {int(x) for x in ENV.get("ADMIN_IDS", "").split(",") if x.strip()}
DATA = Path(ENV.get("DATA_DIR", ".")); DATA.mkdir(parents=True, exist_ok=True)  # per-bot folder: session, settings, temp files
WORK = DATA / "work"; WORK.mkdir(exist_ok=True)
SETTINGS_FILE = DATA / "settings.json"
PENDING = DATA / "pending"; shutil.rmtree(PENDING, ignore_errors=True); PENDING.mkdir(exist_ok=True)  # unpacked ZIP files waiting in the queue
RETRIES = 3
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".webm"}

app = Client("audiosfm_bot", api_id=int(ENV["API_ID"]), api_hash=ENV["API_HASH"], bot_token=ENV["BOT_TOKEN"], workdir=str(DATA))
# ---------------- per-user state ----------------
DEFAULTS = {"bitrate": int(ENV.get("DEFAULT_BITRATE", 64)), "mono": ENV.get("DEFAULT_MONO", "1") == "1",
            "compress": ENV.get("DEFAULT_COMPRESS", "0") == "1", "pattern": ENV.get("DEFAULT_NAME_PATTERN", "Ep {n:03d}"),
            "rename": True, "onfail": "stop", "mode": "detect", "start": 0, "story_id": None}
_settings = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
queues: dict[int, list[Message]] = {}
running: dict[int, bool] = {}
cancel: dict[int, bool] = {}

def S(uid: int) -> dict:
    s = _settings.setdefault(str(uid), {}); [s.setdefault(k, v) for k, v in DEFAULTS.items()]; return s
def save(): SETTINGS_FILE.write_text(json.dumps(_settings, indent=1))

class AdminSite:
    """Logs in to r2admin.php with your admin PIN and uses the same endpoints your browser uses."""
    def __init__(self):
        base = ENV["SITE_URL"].rstrip("/") + "/"
        self.page, self.upload_api = base + "r2admin.php", base + "r2_upload_api.php"
        self.pin, self.lock = ENV["ADMIN_PIN"], threading.Lock()
        self.http, self.csrf = None, None

    @staticmethod
    def _csrf(text):
        m = re.search(r'name="csrf"\s+value="([^"]+)"', text)
        return m.group(1) if m else None

    def login(self):
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "Mozilla/5.0 (AudiosFM-Bot)"
        t = self.http.get(self.page, timeout=60).text
        tok = self._csrf(t)
        if not tok: raise RuntimeError("Could not open r2admin.php — check SITE_URL")
        self.http.post(self.page, data={"csrf": tok, "action": "login", "pin": self.pin}, timeout=60)
        t = self.http.get(self.page, timeout=60).text
        if "audio-upload-form" not in t and "story-card" not in t and "No stories yet" not in t:
            raise RuntimeError("Admin login failed — check ADMIN_PIN")
        self.csrf = self._csrf(t)
        return t

    def _html(self):
        t = self.http.get(self.page, timeout=60).text if self.http else ""
        if "name=\"pin\"" in t or not self.http: t = self.login()
        else: self.csrf = self._csrf(t) or self.csrf
        return t

    def stories(self):
        with self.lock:
            t = self._html()
        out = []
        for card in t.split('<article class="story-card">')[1:]:
            title = re.search(r'class="story-title">(.*?)</div>', card, re.S)
            meta = re.search(r"ID\s*(\d+)\s*\S+\s*(\d+)\s*episodes", card)
            if title and meta:
                out.append({"id": int(meta.group(1)), "title": html.unescape(title.group(1)).strip(), "episodes": int(meta.group(2))})
        return out

    def story(self, sid):
        for s in self.stories():
            if s["id"] == sid: return s
        raise RuntimeError("Story not found")

    def _post(self, data, files=None):
        with self.lock:
            self._html()
            self.http.post(self.page, data={"csrf": self.csrf, **data}, files=files, timeout=120)

    def create_story(self, title, poster: bytes | None = None):
        before = {s["id"] for s in self.stories()}
        files = {"poster": ("poster.jpg", poster)} if poster else None
        self._post({"action": "add_story", "title": title, "description": ""}, files)
        new = [s for s in self.stories() if s["id"] not in before]
        if not new: raise RuntimeError("Story was not created (check the admin panel)")
        return max(new, key=lambda s: s["id"])["id"]

    def set_poster(self, sid, poster: bytes):
        with self.lock:
            t = self._html()
        card = next((c for c in t.split('<article class="story-card">')[1:] if re.search(rf'name="story_id"\s+value="{sid}"', c)), None)
        if not card: raise RuntimeError("Story not found")
        title = html.unescape(re.search(r'name="title"[^>]*value="([^"]*)"', card).group(1))
        desc = html.unescape((re.search(r'<textarea name="description">(.*?)</textarea>', card, re.S) or [None, ""])[1])
        popular = bool(re.search(r'name="is_popular"[^>]*checked', card))
        data = {"action": "update_story", "story_id": str(sid), "title": title, "description": desc}
        if popular: data["is_popular"] = "1"
        self._post(data, {"poster": ("poster.jpg", poster)})

    def upload_episode(self, sid, path: Path, name: str, mime: str, progress=None):
        size = path.stat().st_size
        def call(payload):
            for attempt in range(2):
                with self.lock:
                    if not self.csrf: self._html()
                    r = self.http.post(self.upload_api, json={"csrf": self.csrf, **payload}, timeout=120)
                if r.status_code in (401, 403) and attempt == 0:
                    with self.lock: self.login()
                    continue
                try: d = r.json()
                except Exception: raise RuntimeError(f"Site returned HTTP {r.status_code}")
                if not d.get("ok"): raise RuntimeError(d.get("error", "upload error"))
                return d
        init = call({"action": "initiate", "storyId": sid, "fileName": name, "fileSize": size, "mimeType": mime})
        uid, psize, total = init["uploadId"], init["partSize"], init["totalParts"]
        try:
            parts = []
            with open(path, "rb") as f:
                for n in range(1, total + 1):
                    chunk = f.read(psize)
                    url = call({"action": "sign_part", "uploadId": uid, "partNumber": n})["url"]
                    for attempt in range(4):
                        try:
                            r = requests.put(url, data=chunk, timeout=600)
                            if r.status_code == 200 and r.headers.get("ETag"): break
                            raise RuntimeError(f"R2 part {n} HTTP {r.status_code}")
                        except Exception:
                            if attempt == 3: raise
                            time.sleep(2 * (attempt + 1))
                    parts.append({"partNumber": n, "etag": r.headers["ETag"]})
                    if progress: progress(n, total)
            call({"action": "complete", "uploadId": uid, "parts": parts})
        except Exception:
            try: call({"action": "abort", "uploadId": uid})
            except Exception: pass
            raise

site = AdminSite()

def api(action: str, **kw):
    """Thin wrapper so the command handlers stay simple."""
    if action == "list_stories":
        q = kw.get("q", "").lower()
        st = [s for s in site.stories() if q in s["title"].lower()][:30]
        return {"ok": True, "stories": [{**s, "last_ep": s["episodes"]} for s in st]}
    if action == "story_info":
        s = site.story(kw["story_id"]); return {"ok": True, "story": {**s, "last_ep": s["episodes"]}}
    if action == "create_story": return {"ok": True, "story_id": site.create_story(kw["title"])}
    if action == "set_poster": site.set_poster(kw["story_id"], base64.b64decode(kw["poster_b64"])); return {"ok": True}
    raise RuntimeError("Unknown action")

def admin_only(func):
    async def w(c, m: Message):
        if m.from_user and m.from_user.id in ADMINS: return await func(c, m)
        await m.reply_text("⛔ You are not authorised to use this bot.")
    return w

def natkey(s: str): return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
def detect_number(name: str):
    stem = Path(name).stem
    m = re.search(r"(?:ep(?:isode)?|part|chapter|ch|e)[\s._-]*0*(\d{1,4})", stem, re.I) or re.findall(r"0*(\d{1,4})", stem)
    if isinstance(m, re.Match): return int(m.group(1))
    return int(m[-1]) if m else None
def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

_menu_set = False
HELP = """<b>🎧 AUDIOS FM Upload Bot</b>

<b>Quick start</b>
1. /stories — list stories (or /stories king to search)
2. /select <code>ID</code> — choose a story  (or /newstory <code>Title</code>)
3. Send audio files / a ZIP of episodes (any number)
4. /queue to review, then /go — files are handled <b>one at a time</b>: download → compress → upload → delete → next

<b>Story</b>
/stories [text] – list/search stories
/select ID – choose active story
/newstory Title – create a story &amp; select it
/poster – reply to a photo (or send a photo with caption /poster) to set the active story's poster
/info – show active story &amp; settings

<b>Queue</b>
/queue – show queued files
/go – start processing the queue
/clear – empty the queue
/cancel – stop after the current file

<b>Compression</b>
/compress on|off – re-encode to MP3 (default OFF: files are uploaded exactly as sent)
/bitrate 64 – target kbps (32–192)
/mono on|off – mono output (smaller, great for stories)

<b>Auto rename / numbering</b>
/rename off|on – keep original file names, or auto-rename
/rename Ep {n:03d} – name pattern → “Ep 001.mp3” (current site style)
/onfail stop|skip – if a file fails 3 times: stop here (default) or continue; the failed file always stays in the queue
/mode detect|seq – <code>detect</code>: skip files whose Ep number is already on the site (safe to resend); <code>seq</code>: upload everything

<i>Episodes are numbered by the website in upload order (next number after the last episode).</i>"""

# ---------------- commands ----------------
@app.on_message(filters.command(["start", "help"]) & filters.private)
@admin_only
async def cmd_help(c, m):
    global _menu_set
    await m.reply_text(HELP, disable_web_page_preview=True)
    if not _menu_set:  # fills the "/" menu in Telegram (once per run)
        try:
            await c.set_bot_commands([BotCommand(n, d) for n, d in COMMANDS]); _menu_set = True
        except Exception as e: print("Could not set command menu:", e)

@app.on_message(filters.command("stories") & filters.private)
@admin_only
async def cmd_stories(c, m):
    q = " ".join(m.command[1:])
    try: d = await asyncio.to_thread(api, "list_stories", q=q)
    except Exception as e: return await m.reply_text(f"❌ {e}")
    if not d["stories"]: return await m.reply_text("No stories found.")
    lines = [f"<code>{s['id']}</code> · {s['title']} — {s['episodes']} eps (last #{s['last_ep']})" for s in d["stories"]]
    await m.reply_text("<b>Stories</b> (newest first)\n" + "\n".join(lines) + "\n\nUse /select ID")

@app.on_message(filters.command("select") & filters.private)
@admin_only
async def cmd_select(c, m):
    if len(m.command) < 2 or not m.command[1].isdigit(): return await m.reply_text("Usage: /select 12")
    try: d = await asyncio.to_thread(api, "story_info", story_id=int(m.command[1]))
    except Exception as e: return await m.reply_text(f"❌ {e}")
    S(m.from_user.id)["story_id"] = d["story"]["id"]; save()
    s = d["story"]; await m.reply_text(f"✅ Selected <b>{s['title']}</b> — {s['episodes']} episodes, last #{s['last_ep']}.\nNow send audio files / ZIP.")

@app.on_message(filters.command("newstory") & filters.private)
@admin_only
async def cmd_newstory(c, m):
    title = " ".join(m.command[1:]).strip()
    if not title: return await m.reply_text("Usage: /newstory My Story Title")
    try: d = await asyncio.to_thread(api, "create_story", title=title)
    except Exception as e: return await m.reply_text(f"❌ {e}")
    S(m.from_user.id)["story_id"] = d["story_id"]; save()
    await m.reply_text(f"✅ Created <b>{title}</b> (ID <code>{d['story_id']}</code>) and selected it.\nSet a poster with /poster, then send episodes.")

@app.on_message(filters.command("poster") & filters.private)
@admin_only
async def cmd_poster(c, m):
    sid = S(m.from_user.id)["story_id"]
    if not sid: return await m.reply_text("Select a story first: /select ID")
    src = m if m.photo else (m.reply_to_message if m.reply_to_message and (m.reply_to_message.photo or m.reply_to_message.document) else None)
    if not src: return await m.reply_text("Reply to a photo with /poster, or send a photo with the caption /poster.")
    path = await src.download(file_name=str(WORK / f"poster_{uuid.uuid4().hex}.jpg"))
    try:
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        await asyncio.to_thread(api, "set_poster", story_id=sid, poster_b64=b64)
        await m.reply_text("🖼 Poster updated.")
    except Exception as e: await m.reply_text(f"❌ {e}")
    finally: Path(path).unlink(missing_ok=True)

@app.on_message(filters.command("info") & filters.private)
@admin_only
async def cmd_info(c, m):
    s = S(m.from_user.id)
    await m.reply_text(f"<b>Active story:</b> {s['story_id'] or 'none'}\n<b>Compress:</b> {'on' if s['compress'] else 'off'} · {s['bitrate']} kbps · {'mono' if s['mono'] else 'stereo'}\n"
                       f"<b>Rename:</b> {('ON · <code>' + s['pattern'] + '</code>') if s['rename'] else 'OFF (original names)'}\n<b>Mode:</b> {s['mode']} · <b>On failure:</b> {s['onfail']}\n<b>Queued:</b> {len(queues.get(m.from_user.id, []))}")

def _toggle(key):
    async def h(c, m):
        a = (m.command[1].lower() if len(m.command) > 1 else "")
        if a not in ("on", "off"): return await m.reply_text(f"Usage: /{m.command[0]} on|off")
        S(m.from_user.id)[key] = a == "on"; save(); await m.reply_text(f"✅ {key} = {a}")
    return h
for _cmd, _key in (("compress", "compress"), ("mono", "mono")):
    app.on_message(filters.command(_cmd) & filters.private)(admin_only(_toggle(_key)))

@app.on_message(filters.command("bitrate") & filters.private)
@admin_only
async def cmd_bitrate(c, m):
    if len(m.command) < 2 or not m.command[1].isdigit() or not 32 <= int(m.command[1]) <= 192: return await m.reply_text("Usage: /bitrate 64  (32–192)")
    S(m.from_user.id)["bitrate"] = int(m.command[1]); save(); await m.reply_text(f"✅ Bitrate = {m.command[1]} kbps")

@app.on_message(filters.command("rename") & filters.private)
@admin_only
async def cmd_rename(c, m):
    pat = m.text.split(None, 1)[1].strip() if len(m.command) > 1 else ""
    if pat.lower() in ("off", "on"):
        S(m.from_user.id)["rename"] = pat.lower() == "on"; save()
        return await m.reply_text("✅ Auto rename ON — files named by your pattern." if pat.lower() == "on"
                                  else "✅ Auto rename OFF — original file names are kept.")
    try: sample = pat.format(n=7)
    except Exception: sample = None
    if not pat or sample is None or "{n" not in pat: return await m.reply_text("Usage:\n/rename off – keep original names\n/rename on – use pattern\n/rename Ep {n:03d} – set pattern (must contain {n})")
    S(m.from_user.id).update(pattern=pat, rename=True); save(); await m.reply_text(f"✅ Pattern saved & rename ON. Example: <code>{sample}.mp3</code>")

@app.on_message(filters.command("onfail") & filters.private)
@admin_only
async def cmd_onfail(c, m):
    a = m.command[1].lower() if len(m.command) > 1 else ""
    if a not in ("stop", "skip"): return await m.reply_text("Usage: /onfail stop|skip\n<b>stop</b> (default): after 3 failed tries, stop and keep the file + the rest in the queue (episode order stays correct).\n<b>skip</b>: keep the failed file in the queue and continue with the next ones.")
    S(m.from_user.id)["onfail"] = a; save(); await m.reply_text(f"✅ On failure = {a}")

@app.on_message(filters.command("mode") & filters.private)
@admin_only
async def cmd_mode(c, m):
    a = m.command[1].lower() if len(m.command) > 1 else ""
    if a not in ("detect", "seq"): return await m.reply_text("Usage: /mode detect|seq")
    S(m.from_user.id)["mode"] = a; save(); await m.reply_text(f"✅ Number mode = {a}")

@app.on_message(filters.command("queue") & filters.private)
@admin_only
async def cmd_queue(c, m):
    q = queues.get(m.from_user.id, [])
    if not q: return await m.reply_text("Queue is empty. Send audio files or a ZIP.")
    names = [fname(x) for x in q]
    await m.reply_text(f"📋 <b>{len(q)} queued</b>\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names[:40])) + ("\n…" if len(names) > 40 else "") + "\n\n/go to start")

@app.on_message(filters.command("clear") & filters.private)
@admin_only
async def cmd_clear(c, m):
    if running.get(m.from_user.id): return await m.reply_text("⏳ Upload running — /cancel first, then /clear.")
    for x in queues.get(m.from_user.id, []):
        if isinstance(x, LocalFile): x.path.unlink(missing_ok=True)
    queues[m.from_user.id] = []; await m.reply_text("🗑 Queue cleared.")

@app.on_message(filters.command("cancel") & filters.private)
@admin_only
async def cmd_cancel(c, m):
    cancel[m.from_user.id] = True; await m.reply_text("🛑 Will stop after the current file.")

# ---------------- receiving files ----------------
class LocalFile:
    """A file already on the VPS (unpacked from a ZIP) waiting in the queue."""
    def __init__(self, name: str, path: Path): self.name, self.path = name, path

def fname(m) -> str:
    if isinstance(m, LocalFile): return m.name
    media = m.audio or m.document or m.voice
    return getattr(media, "file_name", None) or f"voice_{m.id}.ogg"

@app.on_message((filters.audio | filters.document | filters.voice) & filters.private)
@admin_only
async def on_file(c, m: Message):
    name = fname(m); ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXT | {".zip"} and not m.audio and not m.voice:
        return await m.reply_text("⚠️ Unsupported file. Send audio (mp3, m4a, wav…) or a .zip of audio files.")
    uid = m.from_user.id
    queues.setdefault(uid, []).append(m)
    n = len(queues[uid])
    if n == 1 or n % 10 == 0:
        await m.reply_text(f"📥 Queued ({n}). Send more, or /go to start." + ("" if S(uid)["story_id"] else "\n⚠️ No story selected — /select ID first."))

# ---------------- processing ----------------
def probe_bitrate(p: Path) -> int:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,bit_rate",
                              "-of", "csv=p=0", str(p)], capture_output=True, text=True, timeout=60).stdout.strip().split(",")
        return int(out[1]) // 1000 if out[0] == "mp3" and out[1].isdigit() else 10**6
    except Exception: return 10**6

async def compress(src: Path, dst: Path, s: dict) -> Path:
    if not s["compress"]:
        return src
    if src.suffix.lower() == ".mp3" and probe_bitrate(src) <= s["bitrate"] and not s["mono"]:
        return src  # already small enough — avoid quality loss from re-encoding
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-map_metadata", "-1", "-c:a", "libmp3lame",
           "-b:a", f"{s['bitrate']}k", "-ar", "44100"] + (["-ac", "1"] if s["mono"] else []) + [str(dst)]
    p = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode: raise RuntimeError("ffmpeg failed: " + err.decode()[-200:])
    return dst

def extract_zip(p: Path, ex: Path) -> list[Path]:
    """Safely extract only audio files from a ZIP (no path traversal). Returns files in natural order."""
    ex.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p) as z:
        for zi in z.infolist():
            nm = Path(zi.filename).name
            if zi.is_dir() or Path(nm).suffix.lower() not in AUDIO_EXT or nm.startswith("."): continue
            (ex / nm).write_bytes(z.read(zi))
    return sorted(ex.glob("*"), key=lambda f: natkey(f.name))

async def safe_edit(msg: Message, text: str):
    try: await msg.edit_text(text)
    except Exception: pass  # "message not modified" / flood limits must never stop an upload

@app.on_message(filters.command("go") & filters.private)
@admin_only
async def cmd_go(c, m: Message):
    uid = m.from_user.id; s = S(uid)
    if running.get(uid): return await m.reply_text("⏳ Already processing. /cancel to stop.")
    if not s["story_id"]: return await m.reply_text("Select a story first: /select ID")
    if not queues.get(uid): return await m.reply_text("Queue is empty. Send audio files or a ZIP first.")
    if not shutil.which("ffmpeg") and s["compress"]: return await m.reply_text("❌ ffmpeg is not installed on the server.")
    running[uid] = True; cancel[uid] = False
    tmp = WORK / f"job_{uid}_{int(time.time())}"; tmp.mkdir(parents=True)
    status = await m.reply_text("🚀 Starting…")
    ok, skipped, failed, deferred = [], [], [], []
    st = {"k": 0, "total": len(queues[uid]), "saved": 0, "nxt": 1, "start": 0}
    stopped_at = None
    try:
        info = (await asyncio.to_thread(api, "story_info", story_id=s["story_id"]))["story"]
        st["start"] = info["episodes"]; st["nxt"] = info["episodes"] + 1  # site numbers episodes in upload order
        queues[uid].sort(key=lambda x: natkey(fname(x)))  # Ep 2 before Ep 10

        def already_on_site(orig):
            det = detect_number(orig)
            return det if (s["mode"] == "detect" and det is not None and det <= st["start"]) else None

        async def site_count():
            return (await asyncio.to_thread(api, "story_info", story_id=info["id"]))["story"]["episodes"]

        async def handle(orig: str, path: Path) -> bool:
            """Prepare + upload ONE local file with automatic retries. True = done/skipped, False = failed."""
            st["k"] += 1; k = st["k"]
            det = already_on_site(orig)
            if det is not None:
                skipped.append(f"{orig} → Ep {det} already on site"); return True
            base = s["pattern"].format(n=st["nxt"]) if s["rename"] else Path(orig).stem
            title = base + ".mp3"
            last_err = None
            for attempt in range(1, RETRIES + 1):
                out = None
                try:
                    tag = f" · retry {attempt}/{RETRIES}" if attempt > 1 else ""
                    await safe_edit(status, f"🎚 {k}/{st['total']} · {orig}\n→ <b>{title}</b>\n{'Compressing…' if s['compress'] else 'Preparing…'}{tag}")
                    out = await compress(path, tmp / f"c_{uuid.uuid4().hex[:6]}.mp3", s)
                    is_mp3 = out.suffix.lower() == ".mp3"
                    name = title if is_mp3 else base + out.suffix.lower()
                    mime = "audio/mpeg" if is_mp3 else (mimetypes.guess_type(out.name)[0] or "application/octet-stream")
                    before, after = path.stat().st_size, out.stat().st_size
                    loop_ = asyncio.get_running_loop(); last = [0.0]
                    def prog(n, total):
                        if time.time() - last[0] < 3 and n < total: return
                        last[0] = time.time()
                        asyncio.run_coroutine_threadsafe(safe_edit(status,
                            f"☁️ {k}/{st['total']} · {name}\n{human(before)} → {human(after)}\nUploading part {n}/{total}…{tag}"), loop_)
                    await safe_edit(status, f"☁️ {k}/{st['total']} · {name}\n{human(before)} → {human(after)}\nUploading…{tag}")
                    await asyncio.to_thread(site.upload_episode, info["id"], out, name, mime, prog)
                    st["nxt"] += 1; st["saved"] += before - after
                    ok.append(f"{name} ({human(before)} → {human(after)})")
                    if out != path: out.unlink(missing_ok=True)
                    return True
                except Exception as e:
                    last_err = e
                    if out is not None and out != path: out.unlink(missing_ok=True)
                    try:  # did the site actually save it even though we got an error? (avoid duplicates)
                        if await site_count() == st["start"] + len(ok) + 1:
                            st["nxt"] += 1; ok.append(f"{title} (saved; reply was lost)"); return True
                    except Exception: pass
                    if attempt < RETRIES:
                        await safe_edit(status, f"⚠️ {orig}\n{e}\nRetrying in {5 * attempt}s ({attempt}/{RETRIES})…")
                        await asyncio.sleep(5 * attempt)
            failed.append(f"{orig}: {last_err}")
            return False

        while queues[uid] and not cancel.get(uid):
            item = queues[uid][0]; orig = fname(item)
            is_local = isinstance(item, LocalFile)
            is_zip = (not is_local) and Path(orig).suffix.lower() == ".zip"
            if not is_zip and already_on_site(orig) is not None:   # skip BEFORE downloading
                st["k"] += 1; skipped.append(f"{orig} → Ep {already_on_site(orig)} already on site")
                queues[uid].pop(0)
                if is_local: item.path.unlink(missing_ok=True)
                continue
            k = st["k"] + 1; path = None
            if is_local: path = item.path
            else:
                last_dl = [0.0]
                async def dl_prog(cur, tot):
                    if time.time() - last_dl[0] < 3: return
                    last_dl[0] = time.time()
                    await safe_edit(status, f"⬇️ {k}/{st['total']} · {orig}\n{human(cur)} / {human(tot)}")
                err = None
                for attempt in range(1, RETRIES + 1):
                    try:
                        await safe_edit(status, f"⬇️ {k}/{st['total']} · {orig}\nDownloading…" + (f" · retry {attempt}/{RETRIES}" if attempt > 1 else ""))
                        path = Path(await item.download(file_name=str(tmp / f"{uuid.uuid4().hex[:8]}_{Path(orig).name}"), progress=dl_prog)); break
                    except Exception as e:
                        err = e; path = None
                        if attempt < RETRIES: await asyncio.sleep(5 * attempt)
                if path is None:
                    st["k"] += 1; failed.append(f"{orig}: download failed ({err})")
                    if s["onfail"] == "stop": stopped_at = orig; break
                    deferred.append(queues[uid].pop(0)); continue
            if is_zip:   # unpack into the queue; each file is then handled (and retried) on its own
                try:
                    files = await asyncio.to_thread(extract_zip, path, PENDING / uuid.uuid4().hex[:8])
                except Exception as e:
                    st["k"] += 1; failed.append(f"{orig}: not a valid ZIP ({e}) — removed from queue")
                    path.unlink(missing_ok=True); queues[uid].pop(0); continue
                path.unlink(missing_ok=True); queues[uid].pop(0)
                queues[uid][0:0] = [LocalFile(f.name, f) for f in files]
                st["total"] += max(len(files) - 1, 0)
                if not files: failed.append(f"{orig}: no audio files inside")
                continue
            good = await handle(orig, path)
            if good or not is_local: path.unlink(missing_ok=True)   # downloaded copies are re-downloaded on retry; unpacked ZIP files are kept
            if good: queues[uid].pop(0)
            elif s["onfail"] == "stop": stopped_at = orig; break
            else: deferred.append(queues[uid].pop(0))

        if deferred: queues[uid] = deferred + queues[uid]   # failed files stay in the queue
        try:
            end_count = await site_count()
            if end_count - st["start"] != len(ok):
                failed.append(f"⚠️ Site episode count changed by {end_count - st['start']}, bot uploaded {len(ok)} — check the admin panel")
        except Exception: pass
        rep = f"{'⛔' if stopped_at else '✅'} <b>{'Stopped' if stopped_at else 'Done'} — {info['title']}</b>\nUploaded: {len(ok)} · Skipped: {len(skipped)} · Failed: {len(failed)}\n💾 Saved ≈ {human(max(st['saved'], 0))}"
        if stopped_at: rep += f"\n\n⛔ Stopped at <b>{stopped_at}</b> after {RETRIES} tries. Nothing was lost: it and the rest are still queued.\nFix the problem, then send /go to continue from this file."
        if cancel.get(uid): rep += "\n🛑 Cancelled by you"
        if queues[uid]: rep += f"\n📋 Still queued: {len(queues[uid])} — /go to continue, /clear to empty"
        if deferred: rep += "\n⚠️ Failed files were skipped; if you upload them later they get later episode numbers."
        if ok: rep += "\n\n" + "\n".join(ok[:25]) + ("\n…" if len(ok) > 25 else "")
        if skipped: rep += "\n\n⏭ <b>Skipped</b>\n" + "\n".join(skipped[:15])
        if failed: rep += "\n\n❌ <b>Failed</b>\n" + "\n".join(failed[:15])
        await safe_edit(status, rep[:4000])
    except Exception as e:
        await safe_edit(status, f"❌ Job failed: {e}\nYour queue is unchanged — send /go to retry.")
    finally:
        running[uid] = False; shutil.rmtree(tmp, ignore_errors=True)

COMMANDS = [("help", "Show all commands"), ("stories", "List / search stories"), ("select", "Choose story: /select ID"),
            ("newstory", "Create a story"), ("poster", "Set story poster"), ("info", "Current story & settings"),
            ("queue", "Show queued files"), ("go", "Start uploading the queue"), ("cancel", "Stop after current file"),
            ("clear", "Empty the queue"), ("compress", "Compression on/off"), ("bitrate", "Set bitrate (kbps)"),
            ("mono", "Mono on/off"), ("rename", "Rename off/on or set pattern"), ("onfail", "On failure: stop / skip"), ("mode", "detect / seq")]

if __name__ == "__main__":
    print("AUDIOS FM bot running…")
    app.run()
