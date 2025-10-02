import asyncio, os, random, re, json, time, logging
import aiohttp, aiosqlite
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from dotenv import load_dotenv

# Enable logging
logging.basicConfig(level=logging.DEBUG)

# Load .env explicitly
load_dotenv(dotenv_path="/opt/tg-torrent-bot/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
QB_URL = os.getenv("QB_URL", "http://127.0.0.1:8080").rstrip("/")
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = os.getenv("QB_PASS", "adminadmin")
SAVE_PATH = os.getenv("SAVE_PATH", "/media/michael/E26C-95B3")
DB_PATH = os.getenv("DB_PATH", "/opt/tg-torrent-bot/state.db")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

# Some qBittorrent setups reject or require different Referer handling.
# We'll avoid sending a Referer by default to maximize compatibility.
HEADERS = {}

# -------------------
# qBittorrent client
# -------------------
class QBClient:
    def __init__(self):
        self.s = None
        self.last_login = 0

    async def _ensure(self):
        if not self.s or self.s.closed:
            self.s = aiohttp.ClientSession()

    async def _login(self):
        await self._ensure()
        if time.time() - self.last_login < 60:
            return
        logging.info("qBittorrent: logging in to %s as %s", QB_URL, QB_USER)
        async with self.s.post(f"{QB_URL}/api/v2/auth/login",
                               headers=HEADERS,
                               data={"username": QB_USER, "password": QB_PASS}) as r:
            txt = (await r.text()).strip()
            logging.debug("qBittorrent login response: %s %s", r.status, txt)
            if r.status != 200 or txt != "Ok.":
                logging.error("qBittorrent auth failed: %s %s", r.status, txt)
                raise RuntimeError(f"qBittorrent auth failed: {r.status} {txt}")
        self.last_login = time.time()

    def _explain_add_error(self, text: str) -> str:
        """Try to produce a short, user-friendly explanation from qBittorrent's
        error body. Returns a truncated snippet if nothing matched.
        """
        if not text:
            return "(no response body)"
        t = text.strip()
        lt = t.lower()
        # common phrases
        if "already" in lt and ("exists" in lt or "in the list" in lt or "duplicate" in lt):
            return f"torrent already exists: {t[:200]}"
        if "not enough" in lt and ("space" in lt or "disk" in lt):
            return f"not enough disk space: {t[:200]}"
        if "insufficient" in lt and "space" in lt:
            return f"insufficient disk space: {t[:200]}"
        if "error" in lt and "file" in lt:
            return f"file error: {t[:200]}"
        # fallback: return up to 200 chars
        return t[:200]

    async def add_magnet(self, magnet: str, tag: str, savepath: str):
        await self._login()
        data = {"urls": magnet, "tags": tag, "savepath": savepath}
        async with self.s.post(f"{QB_URL}/api/v2/torrents/add",
                               data=data) as r:
            if r.status != 200:
                body = (await r.text()) or ""
                reason = self._explain_add_error(body)
                body_snip = body.replace('\n', ' ')[:500]
                raise RuntimeError(f"add_magnet failed: {r.status} {reason} -- raw: {body_snip}")

    async def add_torrent_file(self, filename: str, content: bytes, tag: str, savepath: str):
        await self._login()
        form = aiohttp.FormData()
        form.add_field("tags", tag)
        form.add_field("savepath", savepath)
        form.add_field("torrents", content, filename=filename,
                       content_type="application/x-bittorrent")
        logging.info("qBittorrent: uploading torrent '%s' tag=%s savepath=%s", filename, tag, savepath)
        try:
            async with self.s.post(f"{QB_URL}/api/v2/torrents/add",
                                   data=form) as r:
                text = (await r.text()) or ""
                if r.status != 200:
                    reason = self._explain_add_error(text)
                    body_snip = text.replace('\n', ' ')[:500]
                    logging.error("add_torrent_file failed: %s %s", r.status, body_snip)
                    raise RuntimeError(f"add_torrent_file failed: {r.status} {reason} -- raw: {body_snip}")
                logging.info("add_torrent_file OK: %s tag=%s", filename, tag)
        except Exception:
            logging.exception("Exception while uploading torrent %s", filename)
            raise

    async def torrents_by_tag(self, tag: str):
        await self._login()
        async with self.s.get(f"{QB_URL}/api/v2/torrents/info",
                              params={"tag": tag}) as r:
            text = await r.text()
            if r.status != 200:
                return []
            try:
                return json.loads(text or "[]")
            except json.JSONDecodeError:
                logging.warning("Non-JSON from /torrents/info: %s", text[:120])
                return []

qb = QBClient()

# -------------------
# DB
# -------------------
CREATE = "CREATE TABLE IF NOT EXISTS jobs(id_tag TEXT PRIMARY KEY, chat_id INTEGER NOT NULL);"

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE)
        await db.commit()

async def job_add(tag, chat):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?)", (tag, chat))
        await db.commit()

async def job_list():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id_tag, chat_id FROM jobs") as cur:
            return await cur.fetchall()

async def job_del(tag):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM jobs WHERE id_tag=?", (tag,))
        await db.commit()

# -------------------
# Bot
# -------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

async def gen_id4():
    async with aiosqlite.connect(DB_PATH) as db:
        while True:
            c = str(random.randint(1000, 9999))
            tag = f"id-{c}"
            async with db.execute("SELECT 1 FROM jobs WHERE id_tag=?", (tag,)) as cur:
                if not await cur.fetchone():
                    return c

@dp.message(CommandStart())
async def start(m: types.Message):
    await m.answer(
        f"✅ Bot is running.\nYour chat id: <code>{m.chat.id}</code>\n\n"
        f"Saving to: <code>{SAVE_PATH}</code>\n"
        "Send a .torrent file (as file) or a magnet link."
    )

@dp.message(F.document)
async def handle_document(m: types.Message):
    if ALLOWED_CHAT_ID and str(m.chat.id) != str(ALLOWED_CHAT_ID):
        logging.info("Unauthorized chat %s attempted to send a document", m.chat.id)
        try:
            await m.reply("⚠️ You are not authorized to use this bot.")
        except Exception:
            pass
        return
    name = (m.document.file_name or "").lower()
    if not name.endswith(".torrent"):
        logging.info("Ignored document (not .torrent): %s (%s)", name, m.document.mime_type)
        try:
            await m.reply("⚠️ Please send a .torrent file as a Document (not as a photo).")
        except Exception:
            pass
        return
    id4 = await gen_id4()
    tag = f"id-{id4}"
    try:
        # Acknowledge receipt so the user knows the bot handled the document
        try:
            await m.reply("🔁 Received .torrent; uploading to qBittorrent...")
        except Exception:
            pass
        f = await bot.get_file(m.document.file_id)
        fb = await bot.download_file(f.file_path)
        # fb may be bytes or a file-like object depending on aiogram version
        content = fb.read() if hasattr(fb, "read") else fb
        await qb.add_torrent_file(name, content, tag=tag, savepath=SAVE_PATH)
        # verify the torrent appears in qBittorrent for this tag
        try:
            arr_check = await qb.torrents_by_tag(tag)
        except Exception as e:
            logging.warning("Post-add check failed for %s: %s", tag, e)
            arr_check = []
        if not arr_check:
            logging.warning("Torrent add reported OK but no torrent found for tag %s", tag)
            try:
                await m.reply("⚠️ Torrent was not added to qBittorrent — it may already exist or was rejected. Check qBittorrent UI and bot logs.")
            except Exception:
                pass
            return
        await job_add(tag, m.chat.id)
        await m.reply(f"✅ Added .torrent. ID <b>{id4}</b>\nSave path: <code>{SAVE_PATH}</code>")
        logging.info("Added torrent %s with tag %s", name, tag)
    except Exception as e:
        logging.exception("Failed to add torrent")
        try:
            await m.reply(f"⚠️ Failed to add torrent: <code>{e}</code>")
        except Exception:
            pass

@dp.message(F.text)
async def handle_text(m: types.Message):
    if ALLOWED_CHAT_ID and str(m.chat.id) != str(ALLOWED_CHAT_ID):
        return
    txt = (m.text or "").strip()
    mg = re.search(r"(magnet:\?xt=urn:[^\s]+)", txt, re.IGNORECASE)
    if not mg:
        return
    id4 = await gen_id4()
    tag = f"id-{id4}"
    try:
        await qb.add_magnet(mg.group(1), tag=tag, savepath=SAVE_PATH)
        # verify the magnet was added
        try:
            arr_check = await qb.torrents_by_tag(tag)
        except Exception as e:
            logging.warning("Post-add check failed for %s: %s", tag, e)
            arr_check = []
        if not arr_check:
            logging.warning("Magnet add reported OK but no torrent found for tag %s", tag)
            await m.reply("⚠️ Magnet was not added to qBittorrent — it may already exist or was rejected. Check qBittorrent UI and bot logs.")
            return
        await job_add(tag, m.chat.id)
        await m.reply(f"✅ Added magnet. ID <b>{id4}</b>\nSave path: <code>{SAVE_PATH}</code>")
        logging.info("Added magnet %s with tag %s", mg.group(1)[:40], tag)
    except Exception as e:
        logging.exception("Failed to add magnet")
        await m.reply(f"⚠️ Failed to add magnet: <code>{e}</code>")

async def watcher():
    """Poll jobs and report progress.

    Sends a progress update every minute for active torrents and a finished
    notification when progress >= 1.0. Uses POLL_INTERVAL for the main loop
    (default 10s) but only posts progress messages at 60s intervals per job.
    """
    await asyncio.sleep(3)
    # track last progress message time per tag
    last_progress_ts = {}
    PROGRESS_INTERVAL = 60
    while True:
        try:
            rows = await job_list()
            for tag, chat in rows:
                try:
                    arr = await qb.torrents_by_tag(tag)
                except Exception as e:
                    logging.warning("Watcher error for %s: %s", tag, e)
                    continue
                # finished
                done = [t for t in arr if float(t.get("progress", 0)) >= 1.0]
                if done:
                    names = ", ".join(t.get("name", "?") for t in done)
                    try:
                        await bot.send_message(chat, f"🟢 Finished {tag.replace('id-','ID ')}\n{names}")
                    except Exception:
                        logging.exception("Failed to send finished message for %s", tag)
                    await job_del(tag)
                    last_progress_ts.pop(tag, None)
                    continue

                # if not finished, report progress at most once per PROGRESS_INTERVAL
                now = time.time()
                last = last_progress_ts.get(tag, 0)
                if now - last >= PROGRESS_INTERVAL and arr:
                    # summarize all torrents with this tag
                    parts = []
                    for t in arr:
                        name = t.get("name", "?")
                        progress = float(t.get("progress", 0)) * 100.0
                        dlspeed = int(t.get("dlspeed", 0))
                        parts.append(f"{name}: {progress:.1f}% ({dlspeed} B/s)")
                    msg = f"🔁 Progress {tag.replace('id-','ID ')}\n" + "\n".join(parts)
                    try:
                        await bot.send_message(chat, msg)
                    except Exception:
                        logging.exception("Failed to send progress message for %s", tag)
                    last_progress_ts[tag] = now
        except Exception as e:
            logging.warning("Watcher loop error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")
    await db_init()
    # Check for unfinished jobs at startup and log them so we can diagnose
    try:
        rows = await job_list()
        unfinished = []
        for tag, chat in rows:
            try:
                arr = await qb.torrents_by_tag(tag)
            except Exception as e:
                logging.warning("Startup: failed to query tag %s: %s", tag, e)
                continue
            still = [t for t in arr if float(t.get("progress", 0)) < 1.0]
            if still:
                names = [t.get("name", "?") for t in still]
                unfinished.append((tag, len(still), names))
        if unfinished:
            logging.info("Watcher starting: %d unfinished job(s)", len(unfinished))
            for tag, count, names in unfinished:
                logging.info("  %s: %d torrents: %s", tag, count, ", ".join(names[:5]))
        else:
            logging.info("Watcher starting: no unfinished jobs")
    except Exception as e:
        logging.warning("Watcher startup check failed: %s", e)
    asyncio.create_task(watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
