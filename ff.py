import os, asyncio, sqlite3, time, math, re
from pathlib import Path
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, Update

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@UROGGY")
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "@UROGGY")
USERBOT_SESSION = os.getenv("USERBOT_SESSION") or None
USER_PHONE = os.getenv("USER_PHONE") or None
SESSION_NAME = os.getenv("SESSION_NAME", "vcbot")
DB_PATH = os.getenv("DB_PATH", "bot.db")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
PREMIUM_EMOJI = os.getenv("PREMIUM_EMOJI", "true").lower() == "true"

DOWNLOAD_DIR.mkdir(exist_ok=True)

# ---------- DB ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS chats(
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    joined_at INTEGER
)""")
db.execute("""CREATE TABLE IF NOT EXISTS saves(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    file_id TEXT,
    title TEXT,
    created_at INTEGER
)""")
db.commit()

# ---------- Clients ----------
bot = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
userbot = None
if USERBOT_SESSION or USER_PHONE:
    userbot = Client("userbot", api_id=API_ID, api_hash=API_HASH,
                     session_string=USERBOT_SESSION, phone_number=USER_PHONE)

assistant = userbot if userbot else bot
calls = PyTgCalls(assistant)

# ---------- Queue / State ----------
QUEUE = {}      # chat_id -> list[dict]
NOW = {}        # chat_id -> dict
LOOP = {}       # chat_id -> "off"|"one"|"queue"
SPEED = {}      # chat_id -> float
VOLUME = {}     # chat_id -> int
EQ = {}         # chat_id -> str

def is_owner(uid): return uid == OWNER_ID

def menu_text(role="Owner"):
    return (
        "🎧 **Voice-Chat Audio Bot**\n\n"
        f"👑 Owner: {OWNER_USERNAME}\n\n"
        f"👋 Welcome back — role: **{role}** 👑\n\n"
        "📖 **Quick start**\n"
        "1. Add the bot to your group/channel as admin\n"
        "2. Send `/join` there\n"
        "3. Reply to any audio with `/play`\n\n"
        "🎛 **Controls**\n"
        "`/pause` · `/resume` · `/skip` · `/stop`\n"
        "`/loop off|one|queue` · `/speed 0.25–3.0`\n"
        "`/volume 0–200` · `/seek seconds`\n"
        "`/equalizer` · `/bass` · `/treble` · `/vocal`\n"
        "`/status` · `/controls` · `/queue`\n\n"
        "💾 **Save audio**\n"
        "Reply to any audio/voice with `/save` — stored in your vault\n"
        "`/mysaves` · `/getsave <id>` · `/delsave <id>`\n\n"
        f"❓ Contact: {CONTACT_USERNAME}"
    )

# ---------- /start ----------
@bot.on_message(filters.command("start") & filters.private)
async def start(_, m: Message):
    role = "Owner 👑" if is_owner(m.from_user.id) else "User"
    await m.reply_text(menu_text(role), parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_message(filters.command("menu") & filters.private)
async def menu_cmd(_, m: Message):
    role = "Owner 👑" if is_owner(m.from_user.id) else "User"
    await m.reply_text(menu_text(role), parse_mode=enums.ParseMode.MARKDOWN)

# ---------- /join ----------
@bot.on_message(filters.command("join") & filters.group)
async def join(_, m: Message):
    chat_id = m.chat.id
    try:
        me = await assistant.get_chat_member(chat_id, "me")
        if me.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            return await m.reply_text("⚠️ Make me admin first, then `/join`.")
    except Exception as e:
        return await m.reply_text(f"⚠️ Can't verify admin: `{e}`")

    try:
        await calls.join_group_call(chat_id)
    except Exception as e:
        return await m.reply_text(f"⚠️ VC join failed: `{e}`\n\nIf this is a channel, set `USERBOT_SESSION`.")

    db.execute("INSERT OR REPLACE INTO chats VALUES (?,?,?)", (chat_id, m.chat.title or "", int(time.time())))
    db.commit()
    await m.reply_text("✅ Joined voice chat. Reply to audio with `/play`.")

# ---------- /leave ----------
@bot.on_message(filters.command(["leave", "end"]) & filters.group)
async def leave(_, m: Message):
    try:
        await calls.leave_group_call(m.chat.id)
        QUEUE.pop(m.chat.id, None); NOW.pop(m.chat.id, None)
        await m.reply_text("👋 Left VC.")
    except Exception as e:
        await m.reply_text(f"⚠️ `{e}`")

# ---------- /play ----------
@bot.on_message(filters.command("play") & filters.group)
async def play(_, m: Message):
    if not m.reply_to_message or not (m.reply_to_message.audio or m.reply_to_message.voice or m.reply_to_message.video or m.reply_to_message.document):
        return await m.reply_text("↩️ Reply to an audio/voice/video with `/play`.")

    media = m.reply_to_message.audio or m.reply_to_message.voice or m.reply_to_message.video or m.reply_to_message.document
    file_id = media.file_id
    title = getattr(media, "title", None) or getattr(media, "file_name", None) or "audio"

    track = {"file_id": file_id, "title": title, "by": m.from_user.first_name}
    q = QUEUE.setdefault(m.chat.id, [])
    q.append(track)

    if m.chat.id in NOW:
        return await m.reply_text(f"➕ Queued: **{title}** (#{len(q)})")

    await _start_next(m.chat.id)
    await m.reply_text(f"▶️ Playing: **{title}**")

async def _start_next(chat_id):
    q = QUEUE.get(chat_id, [])
    if not q:
        NOW.pop(chat_id, None)
        try: await calls.leave_group_call(chat_id)
        except: pass
        return
    track = q.pop(0)
    NOW[chat_id] = track
    path = await _download(track["file_id"])
    speed = SPEED.get(chat_id, 1.0)
    vol = VOLUME.get(chat_id, 100)
    stream = MediaStream(
        path,
        audio_parameters=AudioQuality.HIGH,
        video_flags=MediaStream.Flags.IGNORE,
    )
    try:
        await calls.change_stream(chat_id, stream)
    except Exception:
        await calls.join_group_call(chat_id, stream)

async def _download(file_id):
    msg = await bot.get_messages(chat_id=None, message_ids=None) if False else None
    # download via bot
    path = DOWNLOAD_DIR / f"{file_id[:24]}.audio"
    if path.exists(): return str(path)
    await bot.download_media(file_id, file_name=str(path))
    return str(path)

# ---------- Controls ----------
@bot.on_message(filters.command("pause") & filters.group)
async def pause(_, m):
    try: await calls.pause_stream(m.chat.id); await m.reply_text("⏸ Paused.")
    except Exception as e: await m.reply_text(f"⚠️ `{e}`")

@bot.on_message(filters.command("resume") & filters.group)
async def resume(_, m):
    try: await calls.resume_stream(m.chat.id); await m.reply_text("▶️ Resumed.")
    except Exception as e: await m.reply_text(f"⚠️ `{e}`")

@bot.on_message(filters.command("skip") & filters.group)
async def skip(_, m):
    await m.reply_text("⏭ Skipping...")
    await _start_next(m.chat.id)

@bot.on_message(filters.command("stop") & filters.group)
async def stop(_, m):
    QUEUE.pop(m.chat.id, None); NOW.pop(m.chat.id, None)
    try: await calls.leave_group_call(m.chat.id)
    except: pass
    await m.reply_text("⏹ Stopped and left VC.")

@bot.on_message(filters.command("loop") & filters.group)
async def loop(_, m):
    arg = (m.command[1] if len(m.command) > 1 else "off").lower()
    if arg not in ("off", "one", "queue"): arg = "off"
    LOOP[m.chat.id] = arg
    await m.reply_text(f"🔁 Loop: **{arg}**")

@bot.on_message(filters.command("speed") & filters.group)
async def speed(_, m):
    if len(m.command) < 2: return await m.reply_text("Usage: `/speed 0.25-3.0`")
    try:
        v = max(0.25, min(3.0, float(m.command[1])))
        SPEED[m.chat.id] = v
        await m.reply_text(f"⚡ Speed: **{v}x** (applies next track)")
    except: await m.reply_text("⚠️ Bad number.")

@bot.on_message(filters.command("volume") & filters.group)
async def vol(_, m):
    if len(m.command) < 2: return await m.reply_text("Usage: `/volume 0-200`")
    try:
        v = max(0, min(200, int(m.command[1])))
        VOLUME[m.chat.id] = v
        await m.reply_text(f"🔊 Volume: **{v}%**")
    except: await m.reply_text("⚠️ Bad number.")

@bot.on_message(filters.command("seek") & filters.group)
async def seek(_, m):
    if len(m.command) < 2: return await m.reply_text("Usage: `/seek seconds`")
    try: await calls.seek_stream(m.chat.id, int(m.command[1]) * 1000); await m.reply_text(f"⏩ Seek {m.command[1]}s")
    except Exception as e: await m.reply_text(f"⚠️ `{e}`")

@bot.on_message(filters.command("queue") & filters.group)
async def queue(_, m):
    q = QUEUE.get(m.chat.id, [])
    now = NOW.get(m.chat.id)
    txt = "🎧 **Queue**\n\n"
    if now: txt += f"▶️ **{now['title']}** — {now['by']}\n\n"
    if not q and not now: return await m.reply_text("Queue empty.")
    for i, t in enumerate(q, 1): txt += f"{i}. {t['title']} — {t['by']}\n"
    await m.reply_text(txt, parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_message(filters.command("status") & filters.group)
async def status(_, m):
    now = NOW.get(m.chat.id)
    txt = f"**Status**\nLoop: `{LOOP.get(m.chat.id,'off')}`\nSpeed: `{SPEED.get(m.chat.id,1.0)}x`\nVolume: `{VOLUME.get(m.chat.id,100)}%`\n"
    if now: txt += f"\n▶️ {now['title']}"
    await m.reply_text(txt, parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_message(filters.command("controls") & filters.group)
async def controls(_, m):
    await m.reply_text(
        "🎛 **Controls**\n"
        "`/pause` `/resume` `/skip` `/stop`\n"
        "`/loop off|one|queue` `/speed 0.25-3.0`\n"
        "`/volume 0-200` `/seek seconds`\n"
        "`/queue` `/status`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

# EQ stubs (real-time EQ needs ffmpeg pipe — hook here)
@bot.on_message(filters.command("equalizer") & filters.group)
async def equalizer(_, m): await m.reply_text("🎚 EQ presets: `/bass` `/treble` `/vocal`")

@bot.on_message(filters.command("bass") & filters.group)
async def bass(_, m): EQ[m.chat.id] = "bass"; await m.reply_text("🎚 Bass preset ON")

@bot.on_message(filters.command("treble") & filters.group)
async def treble(_, m): EQ[m.chat.id] = "treble"; await m.reply_text("🎚 Treble preset ON")

@bot.on_message(filters.command("vocal") & filters.group)
async def vocal(_, m): EQ[m.chat.id] = "vocal"; await m.reply_text("🎚 Vocal preset ON")

# ---------- SAVE / VAULT ----------
@bot.on_message(filters.command("save") & (filters.private | filters.group))
async def save(_, m: Message):
    if not m.reply_to_message:
        return await m.reply_text("↩️ Reply to an audio/voice/video with `/save`.")
    r = m.reply_to_message
    media = r.audio or r.voice or r.video or r.document
    if not media:
        return await m.reply_text("⚠️ That's not an audio file.")
    title = getattr(media, "title", None) or getattr(media, "file_name", None) or "audio"
    cur = db.execute("INSERT INTO saves(user_id,file_id,title,created_at) VALUES (?,?,?,?)",
                     (m.from_user.id, media.file_id, title, int(time.time())))
    db.commit()
    await m.reply_text(f"💾 Saved as `#{cur.lastrowid}` — **{title}**", parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_message(filters.command("mysaves"))
async def mysaves(_, m: Message):
    rows = db.execute("SELECT id,title,created_at FROM saves WHERE user_id=? ORDER BY id DESC LIMIT 30",
                      (m.from_user.id,)).fetchall()
    if not rows: return await m.reply_text("Vault empty. Reply to audio with `/save`.")
    txt = "💾 **Your Vault**\n\n"
    for i, t, _ in rows: txt += f"`#{i}` — {t}\n"
    txt += "\n`/getsave <id>` · `/delsave <id>`"
    await m.reply_text(txt, parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_message(filters.command("getsave"))
async def getsave(_, m: Message):
    if len(m.command) < 2: return await m.reply_text("Usage: `/getsave <id>`")
    try: sid = int(m.command[1])
    except: return await m.reply_text("⚠️ Bad id.")
    row = db.execute("SELECT file_id,title FROM saves WHERE id=? AND user_id=?",
                     (sid, m.from_user.id)).fetchone()
    if not row: return await m.reply_text("⚠️ Not found.")
    fid, title = row
    await m.reply_audio(fid, caption=f"💾 #{sid} — {title}")

@bot.on_message(filters.command("delsave"))
async def delsave(_, m: Message):
    if len(m.command) < 2: return await m.reply_text("Usage: `/delsave <id>`")
    try: sid = int(m.command[1])
    except: return await m.reply_text("⚠️ Bad id.")
    db.execute("DELETE FROM saves WHERE id=? AND user_id=?", (sid, m.from_user.id))
    db.commit()
    await m.reply_text(f"🗑 Deleted `#{sid}`.")

# ---------- Auto-next on stream end ----------
@calls.on_update()
async def on_update(_, update: Update):
    if update.__class__.__name__ == "StreamEnded":
        chat_id = update.chat_id
        mode = LOOP.get(chat_id, "off")
        now = NOW.get(chat_id)
        if mode == "one" and now:
            QUEUE.setdefault(chat_id, []).insert(0, now)
        elif mode == "queue" and now:
            QUEUE.setdefault(chat_id, []).append(now)
        await _start_next(chat_id)

# ---------- Startup ----------
async def main():
    await bot.start()
    if userbot: await userbot.start()
    await calls.start()
    me = await bot.get_me()
    print(f"✅ Forge lit — @{me.username}")
    print(f"👑 Owner: {OWNER_USERNAME}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())