"""
ULUS Security Bot — Kanal / Grup koruma botu
Ayarlar .env dosyasından okunur (bkz. .env).
"""
from telegram.ext import (
    ChatMemberHandler,
    Application,
    ApplicationHandlerStop,
    AIORateLimiter,
    MessageHandler,
    filters,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    TypeHandler,
    JobQueue,
)
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, Bot, MessageEntity,
    ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonRequestChat, ForceReply, BotCommand,
    BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
)
from telegram.constants import ParseMode, KeyboardButtonStyle
from telegram.ext import ContextTypes
from telegram.error import TelegramError, Forbidden, BadRequest, InvalidToken
from telegram.helpers import mention_html
import sqlite3
import json
import time
import random
import copy
import html
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone, time as dtime
import asyncio
import contextvars
import functools
import logging
import os
import re
import signal

from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantRequest, GetFullChannelRequest
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.errors import UserNotParticipantError, FloodWaitError
from telethon.tl.types import InputPeerUser, ChannelParticipantBanned

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

def _env_path(name: str, default: str) -> str:
    """Boş bırakılan .env değeri varsayılana düşer; göreli yollar main.py klasörüne göre çözülür
    (PythonAnywhere'de görev farklı bir klasörden başlatılsa da aynı dosyalar kullanılır)."""
    p = (os.getenv(name) or "").strip() or default
    return p if os.path.isabs(p) else os.path.join(BASE_DIR, p)

logging.basicConfig(
    level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").strip().upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────── AYARLAR (.env) ───────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN tanımlı değil! .env dosyasını doldurun.")

def _env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name, str(default)) or str(default)).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

FOUNDER_ID_RAW = (os.getenv("FOUNDER_ID", "") or "").strip()
FOUNDER_ID = _env_int("FOUNDER_ID", 0)
FOUNDER_ID_VALID = bool(FOUNDER_ID_RAW) and str(FOUNDER_ID) == FOUNDER_ID_RAW and FOUNDER_ID > 0

DB_FILE = _env_path("DB_FILE", "bot_data.db")
BACKUP_DIR = _env_path("BACKUP_DIR", "backups")
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7") or 7)
BACKUP_CHAT_ID = int(os.getenv("BACKUP_CHAT_ID", "0") or 0) or FOUNDER_ID

# Telethon istemcisi (opsiyonel). my.telegram.org'dan alınır.
# Not: Bot token ile giriş yapar; gerçek kullanıcı hesabı (userbot) değildir.
USERBOT_API_ID = int(os.getenv("API_ID", "0") or 0)
USERBOT_API_HASH = os.getenv("API_HASH", "").strip()
USERBOT_SESSION = _env_path("TELETHON_SESSION", "ulus_userbot")

TZ_TR = timezone(timedelta(hours=3))
MESSAGE_STATS_RETENTION_DAYS = max(1, _env_int("MESSAGE_STATS_RETENTION_DAYS", 30))
MOD_LOG_RETENTION_DAYS = 180  # /sicil ve /info için ceza geçmişi

# Premium emoji: ENV'den custom ID verilirse HTML parse ile kullanılır, yoksa standart emojiye düşer.
EMOJI_FALLBACKS = {
    'success': '✅', 'error': '❌', 'warn': '⚠️', 'ban': '🚫', 'mute': '🔇',
    'unmute': '🔊', 'unban': '✅', 'info': 'ℹ️', 'clock': '⏰', 'shield': '🛡️'
}
EMOJIS = {
    key: {
        'id': (os.getenv(f"EMOJI_{key.upper()}_ID", "") or "").strip(),
        'fallback': fallback,
    }
    for key, fallback in EMOJI_FALLBACKS.items()
}

TELEGRAM_SERVICE_ID = 777000        # Bağlı kanaldan otomatik iletilen gönderiler
GROUP_ANON_BOT_ID = 1087968824      # Anonim adminler
CHANNEL_BOT_ID = 136817688          # "Kanal olarak" yazan kullanıcılar

userbot: TelegramClient = None
BOT_ID: int = 0


class UlusRateLimiter(AIORateLimiter):
    """Telegram'ın grup başına dakikada ~20 mesaj sınırı yalnızca mesaj GÖNDERMEYE uygulanır.
    Varsayılan AIORateLimiter bu sınırı grup chat_id'li her isteğe (silme, ban, mute...) uyguladığı
    için spam saldırısında moderasyon işlemleri 3 sn arayla sıraya giriyordu. Burada gönderim dışı
    istekler yalnızca genel (saniyede 30) sınıra tabidir; 429 gelirse yine max_retries kadar denenir."""
    _GROUP_LIMITED = ("send", "copy", "forward")

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        if data.get("chat_id") is not None and not endpoint.startswith(self._GROUP_LIMITED):
            data = {**data, "chat_id": 0}  # genel sınır uygulanır, grup sınırı uygulanmaz
        return await super().process_request(callback, args, kwargs, endpoint, data, rate_limit_args)

async def get_userbot() -> TelegramClient:
    global userbot
    if userbot and userbot.is_connected():
        return userbot
    return None

# Uygulama oluşturulunca main() içinde application.bot ile değiştirilir
# (rate limiter dahil tek bot nesnesi kullanılır).
# ═══════════════════════════ ÇOKLU BOT (ANA BOT + KLONLAR) ═══════════════════════════
# Tüm botlar tek süreçte, aynı veritabanıyla çalışır. Modüldeki `bot` bir vekildir: işlem bir gruba/kanala
# yönelikse o sohbeti yöneten botu (channels.bot_id), değilse güncellemenin geldiği botu, o da yoksa ana botu kullanır.
_ctx_bot: contextvars.ContextVar = contextvars.ContextVar('ulus_ctx_bot', default=None)
MAIN_BOT: Bot = None
RUNNING_BOTS: dict = {}     # bot_id -> Bot (ana bot + çalışan klonlar)
CLONES: dict = {}           # bot_id -> klon kaydı (dict)
CLONE_APPS: dict = {}       # bot_id -> Application
_chat_bot_map: dict = {}    # chat_id -> yöneten bot_id (0 = eski kayıt → ana bot, -1 = sahipsiz)

def chat_bot_id(chat_id) -> int | None:
    """Sohbeti yöneten botun ID'si (bilinmiyorsa None)."""
    cid = str(chat_id)
    if cid not in _chat_bot_map:
        try:
            with get_db() as conn:
                row = conn.execute("SELECT bot_id FROM channels WHERE chat_id = ?", (cid,)).fetchone()
        except sqlite3.Error:
            row = None
        _chat_bot_map[cid] = (row['bot_id'] or 0) if row else None
    v = _chat_bot_map[cid]
    if v is None or v == -1:
        return None
    return v or BOT_ID or None

def set_chat_bot(chat_id, bot_id: int):
    with get_db() as conn:
        conn.execute("UPDATE channels SET bot_id = ? WHERE chat_id = ?", (bot_id, str(chat_id)))
        conn.commit()
    _chat_bot_map.pop(str(chat_id), None)

def cur_bot_id() -> int:
    """Şu an işlem yapan botun ID'si (güncellemenin geldiği bot; yoksa ana bot)."""
    b = _ctx_bot.get()
    return b.id if b is not None else BOT_ID

def bot_id_for(chat_id) -> int:
    """Bu sohbette işlem yapan botun ID'si (sohbeti yöneten çalışan bot; yoksa şu anki bot)."""
    b = chat_bot_id(chat_id)
    return b if b in RUNNING_BOTS else cur_bot_id()

def current_clone() -> dict | None:
    bid = cur_bot_id()
    return CLONES.get(bid) if bid and bid != BOT_ID else None

def bot_owner_id() -> int:
    c = current_clone()
    return c['owner_id'] if c else FOUNDER_ID

def is_bot_owner(user_id: int) -> bool:
    """Bu botun sahibi mi? Ana botta FOUNDER_ID; klonda klon sahibi (FOUNDER_ID her yerde yetkili)."""
    return bool(user_id) and user_id in (bot_owner_id(), FOUNDER_ID)

def brand() -> str:
    c = current_clone()
    return (c.get('brand') or c.get('name') or 'ULUS') if c else 'ULUS'

def _help_title() -> str:
    c = current_clone()
    return (c.get('help_title') or f"{brand()} Security Bot") if c else "ULUS Security Bot"

def brand_footer() -> str:
    """Klonlarda /start ve /help mesajının en altına ana bot imzası."""
    if not current_clone() or not MAIN_BOT:
        return ''
    try:
        return f"\n\n⚡ Main bot : @{MAIN_BOT.username}"
    except RuntimeError:
        return ''

def bot_chat_ids(bot_id: int | None = None) -> list:
    """Bu botun yönettiği kayıtlı sohbetler (ana bot için eski kayıtlar dahil)."""
    bid = bot_id or cur_bot_id()
    with get_db() as conn:
        if bid == BOT_ID:
            rows = conn.execute("SELECT chat_id FROM channels WHERE bot_id IS NULL OR bot_id IN (0, ?)", (bid,))
        else:
            rows = conn.execute("SELECT chat_id FROM channels WHERE bot_id = ?", (bid,))
        return [r['chat_id'] for r in rows]

class _BotProxy:
    """`bot.xxx(...)` çağrılarını doğru bota yönlendirir (bkz. yukarıdaki açıklama)."""
    def _pick(self, args, kwargs):
        cid = kwargs.get('chat_id', args[0] if args else None)
        if isinstance(cid, (int, str)) and str(cid).startswith('-'):
            b = RUNNING_BOTS.get(chat_bot_id(cid) or 0)
            if b is not None:
                return b
        return _ctx_bot.get() or MAIN_BOT

    def __getattr__(self, name):
        base = _ctx_bot.get() or MAIN_BOT
        if base is None:
            raise AttributeError(name)
        attr = getattr(base, name)
        if not asyncio.iscoroutinefunction(attr):
            return attr
        pick = self._pick

        async def call(*args, **kwargs):
            return await getattr(pick(args, kwargs), name)(*args, **kwargs)
        return call

    def __bool__(self):
        return (_ctx_bot.get() or MAIN_BOT) is not None

bot = _BotProxy()

class UlusJobQueue(JobQueue):
    """Zamanlanmış işler, kuyruğun ait olduğu botun adına çalışır (bir klonun güncellemesi sırasında
    kurulan zamanlayıcı yüzünden ana botun işleri klon bağlamına kaymasın)."""
    @staticmethod
    async def job_callback(job_queue, job) -> None:
        _ctx_bot.set(job_queue.application.bot)  # her iş kendi görevinde çalışır; ayar sadece o işe özel
        await job.run(job_queue.application)

_main_absent_checked: dict = {}

async def _main_bot_absent(cid: str, via) -> bool:
    """Ana bot bu grupta değil mi? (klonun gözünden, sohbet başına 1 saatte bir kontrol)"""
    if time.time() - _main_absent_checked.get(cid, 0) < 3600:
        return False
    _main_absent_checked[cid] = time.time()
    try:
        m = await via.get_chat_member(cid, BOT_ID)
    except TelegramError:
        return True
    except Exception:
        return False
    return getattr(m, 'status', 'left') in ('left', 'kicked')

async def bot_context_handler(update: Update, context):
    """Her güncellemenin en başında: hangi botla çalışıldığını işaretler; başka botumuzun yönettiği sohbetin
    güncellemelerini (aynı grupta iki botumuz varsa çift işlem olmasın diye) yok sayar."""
    _ctx_bot.set(context.bot)
    chat = update.effective_chat
    if not chat or chat.type == 'private' or update.my_chat_member:
        return
    cid = str(chat.id)
    if cid not in _chat_bot_map:
        chat_bot_id(cid)
    raw = _chat_bot_map.get(cid)
    if raw is None:
        return
    if raw == -1:  # sahipsiz kalmış sohbet: ilk gelen botumuz sahiplenir
        set_chat_bot(cid, context.bot.id)
        return
    if raw == 0 and BOT_ID and context.bot.id != BOT_ID and await _main_bot_absent(cid, context.bot):
        # eski sürümde ayar kaydı bot_id'yi siliyordu: ana botun olmadığı bu grubu klon geri sahiplenir
        set_chat_bot(cid, context.bot.id)
        return
    owner = raw or BOT_ID
    if owner != context.bot.id and owner in RUNNING_BOTS:
        raise ApplicationHandlerStop


_db_lock = asyncio.Lock()


class _ClosingConnection(sqlite3.Connection):
    """`with get_db() as conn:` bloğu bitince commit/rollback yapar VE bağlantıyı kapatır."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _sql_mybot(bot_id) -> int:
    """SQL: mybot(channels.bot_id) — sohbet şu an çalışan bota mı ait (ana bot: NULL/0/-1/kendi ID'si)."""
    cur = cur_bot_id()
    if cur == BOT_ID or not cur:
        return int(bot_id in (None, 0, -1, BOT_ID))
    return int(bot_id == cur)

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.create_function('mybot', 1, _sql_mybot, deterministic=False)  # bu botun sohbeti mi? (klon filtreleri)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                log_chat_id TEXT,
                chat_type TEXT,
                settings TEXT NOT NULL,
                stats TEXT NOT NULL,
                invites TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_bot INTEGER DEFAULT 0,
                joined_at REAL NOT NULL,
                last_seen REAL,
                PRIMARY KEY (user_id, chat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_users_chat ON users(chat_id);

            CREATE TABLE IF NOT EXISTS roles (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('kurucu','yardimci_kurucu','basadmin','admin')),
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS role_permissions (
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                can_ban INTEGER DEFAULT 1,
                can_kick INTEGER DEFAULT 1,
                can_mute INTEGER DEFAULT 1,
                can_warn INTEGER DEFAULT 1,
                can_delete INTEGER DEFAULT 1,
                can_pin INTEGER DEFAULT 0,
                can_manage_settings INTEGER DEFAULT 0,
                can_manage_roles INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, role)
            );

            CREATE TABLE IF NOT EXISTS forward_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_forward ON forward_history(chat_id, user_id);

            CREATE TABLE IF NOT EXISTS media_flood_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_media ON media_flood_history(chat_id, user_id);

            CREATE TABLE IF NOT EXISTS flood_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_flood ON flood_history(chat_id, user_id);

            CREATE TABLE IF NOT EXISTS warnings (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                warn_count INTEGER DEFAULT 0,
                last_warn_at REAL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS giveaways (
                chat_id TEXT PRIMARY KEY,
                message_id INTEGER NOT NULL,
                participants TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS captcha_pending (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                answer TEXT NOT NULL,
                message_id INTEGER,
                joined_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS ban_list (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                reason TEXT,
                banned_at REAL NOT NULL,
                banned_by INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS mute_list (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                until_date REAL,
                muted_at REAL NOT NULL,
                muted_by INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS raid_joins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raid ON raid_joins(chat_id, timestamp);

            CREATE TABLE IF NOT EXISTS temp_bans (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                unban_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS join_requests (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                status TEXT DEFAULT 'pending',
                requested_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS nightmod_settings (
                chat_id TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                start_hour INTEGER DEFAULT 23,
                start_minute INTEGER DEFAULT 0,
                end_hour INTEGER DEFAULT 7,
                end_minute INTEGER DEFAULT 0,
                restrictions TEXT NOT NULL DEFAULT '{}',
                saved_permissions TEXT,
                is_active INTEGER DEFAULT 0,
                configured INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS mod_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_user_id INTEGER,
                target_username TEXT,
                by_user_id INTEGER,
                by_username TEXT,
                reason TEXT,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_modlog ON mod_log(chat_id, target_user_id);

            CREATE TABLE IF NOT EXISTS message_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                msg_type TEXT NOT NULL DEFAULT 'text',
                sent_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msgstats_chat ON message_stats(chat_id, sent_at);
            CREATE INDEX IF NOT EXISTS idx_msgstats_user ON message_stats(chat_id, user_id, sent_at);

            CREATE INDEX IF NOT EXISTS idx_ban_list_user ON ban_list(user_id, banned_at);
            CREATE INDEX IF NOT EXISTS idx_ban_list_chat_time ON ban_list(chat_id, banned_at);
            CREATE INDEX IF NOT EXISTS idx_temp_bans_unban ON temp_bans(unban_at);
            CREATE INDEX IF NOT EXISTS idx_temp_bans_chat_unban ON temp_bans(chat_id, unban_at);
            CREATE INDEX IF NOT EXISTS idx_mute_list_until ON mute_list(chat_id, until_date);
            CREATE INDEX IF NOT EXISTS idx_warnings_chat_user ON warnings(chat_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_modlog_chat_time ON mod_log(chat_id, timestamp);

            CREATE TABLE IF NOT EXISTS bot_given_admins (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                given_by INTEGER NOT NULL,
                given_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS user_perm_off (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                perm TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id, perm)
            );

            CREATE TABLE IF NOT EXISTS user_permissions (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                can_ban INTEGER DEFAULT NULL,
                can_kick INTEGER DEFAULT NULL,
                can_mute INTEGER DEFAULT NULL,
                can_warn INTEGER DEFAULT NULL,
                can_delete INTEGER DEFAULT NULL,
                can_pin INTEGER DEFAULT NULL,
                can_manage_settings INTEGER DEFAULT NULL,
                can_manage_roles INTEGER DEFAULT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS blocked_entities (
                entity_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL DEFAULT 'user',
                reason TEXT,
                blocked_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS member_tags (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                set_by INTEGER NOT NULL,
                set_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS spam_incidents (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                incident_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_spam_inc ON spam_incidents(chat_id, incident_at);

            CREATE TABLE IF NOT EXISTS admin_flood (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                msg_type TEXT NOT NULL DEFAULT 'text',
                msg_id INTEGER DEFAULT 0,
                sent_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_adminflood ON admin_flood(chat_id, user_id, sent_at);

            CREATE TABLE IF NOT EXISTS saved_admins (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                custom_title TEXT,
                can_post_messages INTEGER DEFAULT 0,
                can_edit_messages INTEGER DEFAULT 0,
                can_delete_messages INTEGER DEFAULT 0,
                can_invite_users INTEGER DEFAULT 0,
                can_restrict_members INTEGER DEFAULT 0,
                can_promote_members INTEGER DEFAULT 0,
                can_manage_chat INTEGER DEFAULT 0,
                saved_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS channel_settings (
                chat_id TEXT PRIMARY KEY,
                admin_spam_enabled INTEGER DEFAULT 0,
                admin_spam_limit INTEGER DEFAULT 10,
                admin_spam_window INTEGER DEFAULT 300,
                admin_spam_action TEXT DEFAULT 'demote',
                admin_media_enabled INTEGER DEFAULT 0,
                admin_media_limit INTEGER DEFAULT 10,
                admin_media_window INTEGER DEFAULT 300,
                admin_media_action TEXT DEFAULT 'demote_ban',
                link_protection INTEGER DEFAULT 0,
                clone_protection INTEGER DEFAULT 0,
                bot_add_protection INTEGER DEFAULT 0,
                bulk_ban_protection INTEGER DEFAULT 0,
                bulk_ban_limit INTEGER DEFAULT 5,
                bulk_ban_window INTEGER DEFAULT 300,
                safe_admins TEXT DEFAULT '[]',
                channel_title TEXT,
                channel_description TEXT,
                lockdown_mode INTEGER DEFAULT 0,
                lockdown_saved_admins TEXT DEFAULT '[]',
                weekly_log_day INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS channel_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                action TEXT NOT NULL,
                user_id INTEGER,
                username TEXT,
                detail TEXT,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_channellog ON channel_log(chat_id, timestamp);

            CREATE TABLE IF NOT EXISTS admin_blacklist (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                reason TEXT,
                added_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS global_bans (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_by INTEGER,
                banned_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS newcomers (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS appeals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                text TEXT,
                status TEXT DEFAULT 'pending',
                created_at REAL NOT NULL,
                handled_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_appeals ON appeals(chat_id, user_id, created_at);

            CREATE TABLE IF NOT EXISTS join_captcha (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                answer TEXT NOT NULL,
                message_id INTEGER,
                created_at REAL NOT NULL,
                passed INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS clones (
                bot_id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                token TEXT NOT NULL,
                username TEXT,
                name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                brand TEXT,
                start_text TEXT,
                support_link TEXT,
                help_title TEXT,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS clone_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                decided_at REAL,
                bot_id INTEGER,
                token TEXT,
                bot_username TEXT,
                bot_name TEXT,
                admin_msg_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS clone_bans (
                bot_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                banned_by INTEGER,
                banned_at REAL NOT NULL,
                PRIMARY KEY (bot_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS rich_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE (chat_id, kind, value)
            );

            CREATE TABLE IF NOT EXISTS auto_delete (
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_autodel_at ON auto_delete(delete_at);

            CREATE TABLE IF NOT EXISTS scheduled_msgs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                rich TEXT NOT NULL,
                interval INTEGER NOT NULL,
                next_at REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                delete_prev INTEGER NOT NULL DEFAULT 1,
                last_msg_ids TEXT,
                created_by INTEGER,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sched_next ON scheduled_msgs(enabled, next_at);

            CREATE TABLE IF NOT EXISTS afk (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                since REAL NOT NULL,
                name TEXT
            );

            CREATE TABLE IF NOT EXISTS name_history (
                user_id INTEGER NOT NULL,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                seen_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_namehist ON name_history(user_id, seen_at);

            CREATE TABLE IF NOT EXISTS member_events (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_member_events ON member_events(chat_id, at);

            CREATE TABLE IF NOT EXISTS tag_optout (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS chat_filters (
                chat_id TEXT NOT NULL,
                trigger_norm TEXT NOT NULL,
                trigger_text TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'exact',
                reply_text TEXT,
                file_id TEXT,
                file_type TEXT,
                created_by INTEGER,
                created_at REAL NOT NULL,
                PRIMARY KEY (chat_id, trigger_norm)
            );

            CREATE TABLE IF NOT EXISTS notes (
                chat_id TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT,
                file_id TEXT,
                file_type TEXT,
                created_by INTEGER,
                created_at REAL NOT NULL,
                PRIMARY KEY (chat_id, name)
            );

            CREATE TABLE IF NOT EXISTS blocked_media (
                chat_id TEXT NOT NULL,
                uid TEXT NOT NULL,
                kind TEXT NOT NULL,
                note TEXT,
                added_by INTEGER,
                added_at REAL NOT NULL,
                PRIMARY KEY (chat_id, uid)
            );

            CREATE TABLE IF NOT EXISTS msg_cache (
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                text TEXT,
                sent_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_msgcache_ts ON msg_cache(sent_at);

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                text TEXT,
                copies TEXT DEFAULT '[]',
                status TEXT DEFAULT 'open',
                handled_by INTEGER,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reports ON reports(chat_id, message_id);

            CREATE TABLE IF NOT EXISTS networks (
                owner_id INTEGER PRIMARY KEY,
                ban_sync INTEGER DEFAULT 1,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS network_members (
                chat_id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                added_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS network_bans (
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                source_chat TEXT,
                until_date REAL,
                banned_at REAL NOT NULL,
                PRIMARY KEY (owner_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS admin_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                data TEXT NOT NULL,
                admin_count INTEGER,
                taken_at REAL NOT NULL,
                reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_adminsnap ON admin_snapshots(chat_id, taken_at);

            CREATE INDEX IF NOT EXISTS idx_forward_ts ON forward_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_media_ts ON media_flood_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_flood_ts ON flood_history(timestamp);
        """)

def _enable_wal():
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()

_enable_wal()
init_db()

def migrate_db():
    with get_db() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(admin_flood)").fetchall()}
        if 'msg_id' not in existing:
            conn.execute("ALTER TABLE admin_flood ADD COLUMN msg_id INTEGER DEFAULT 0")
            conn.commit()

        existing = {row[1] for row in conn.execute("PRAGMA table_info(channel_settings)").fetchall()}
        needed = [
            ('admin_spam_enabled', 'INTEGER DEFAULT 0'),
            ('admin_spam_limit', 'INTEGER DEFAULT 10'),
            ('admin_spam_window', 'INTEGER DEFAULT 300'),
            ('admin_spam_action', "TEXT DEFAULT 'demote'"),
            ('admin_media_enabled', 'INTEGER DEFAULT 0'),
            ('admin_media_limit', 'INTEGER DEFAULT 10'),
            ('admin_media_window', 'INTEGER DEFAULT 300'),
            ('admin_media_action', "TEXT DEFAULT 'demote_ban'"),
            ('link_protection', 'INTEGER DEFAULT 0'),
            ('clone_protection', 'INTEGER DEFAULT 0'),
            ('bot_add_protection', 'INTEGER DEFAULT 0'),
            ('bulk_ban_protection', 'INTEGER DEFAULT 0'),
            ('bulk_ban_limit', 'INTEGER DEFAULT 5'),
            ('bulk_ban_window', 'INTEGER DEFAULT 300'),
            ('safe_admins', "TEXT DEFAULT '[]'"),
            ('channel_title', 'TEXT'),
            ('channel_description', 'TEXT'),
            ('lockdown_mode', 'INTEGER DEFAULT 0'),
            ('lockdown_saved_admins', "TEXT DEFAULT '[]'"),
            ('weekly_log_day', 'INTEGER DEFAULT 0'),
        ]
        for col, typedef in needed:
            if col not in existing:
                conn.execute(f"ALTER TABLE channel_settings ADD COLUMN {col} {typedef}")
        conn.commit()

        for tbl in ['saved_admins', 'channel_log', 'admin_blacklist', 'join_requests', 'nightmod_settings', 'mod_log', 'message_stats', 'bot_given_admins', 'blocked_entities', 'member_tags', 'spam_incidents']:
            try:
                conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
            except Exception as e:
                logger.debug(f"migrate_db: {e}")

migrate_db()

def _migrate_installer():
    """channels.added_by (botu ekleyen kişi) sütunu. Eski kayıtlarda bot sahibi, kurucu eşitlemesiyle yardımcı
    kurucuya düşürüldüyse (elle verilmemiş yardımcı kurucu) botu ekleyen kişi olarak kurucuya geri alınır."""
    with get_db() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)")}
        if 'added_by' not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN added_by INTEGER")
        if 'bot_id' not in cols:  # sohbeti yöneten bot (NULL = ana bot)
            conn.execute("ALTER TABLE channels ADD COLUMN bot_id INTEGER")
        gcols = {r[1] for r in conn.execute("PRAGMA table_info(giveaways)")}
        for col, typedef in (('prize', 'TEXT'), ('winners', 'INTEGER DEFAULT 1'), ('ends_at', 'REAL'), ('req', 'TEXT'),
                             ('started_by', 'INTEGER')):
            if col not in gcols:  # çekiliş: ödül, kazanan sayısı, otomatik bitiş, katılım şartları
                conn.execute(f"ALTER TABLE giveaways ADD COLUMN {col} {typedef}")
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if 'left_at' not in ucols:  # gruptan ayrılan üye (/etiket listesine girmez; tekrar yazınca/katılınca silinir)
            conn.execute("ALTER TABLE users ADD COLUMN left_at REAL")
        fcols = {r[1] for r in conn.execute("PRAGMA table_info(chat_filters)")}
        if 'is_html' not in fcols:  # eski filtreler düz metin; yeniler biçimli (HTML) kaydedilir
            conn.execute("ALTER TABLE chat_filters ADD COLUMN is_html INTEGER DEFAULT 0")
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(clone_requests)")}
        for col, typedef in (('bot_id', 'INTEGER'), ('token', 'TEXT'), ('bot_username', 'TEXT'), ('bot_name', 'TEXT'),
                             ('admin_msg_id', 'INTEGER')):
            if col not in rcols:  # eski sürüm: istek = sadece izin; yeni: istek = token ile açılacak bot
                conn.execute(f"ALTER TABLE clone_requests ADD COLUMN {col} {typedef}")
        if FOUNDER_ID:
            rows = conn.execute("""
                SELECT c.chat_id FROM channels c JOIN roles r ON r.chat_id = c.chat_id
                WHERE c.added_by IS NULL AND r.user_id = ? AND r.role = 'yardimci_kurucu'
                  AND NOT EXISTS (SELECT 1 FROM bot_given_admins b WHERE b.chat_id = c.chat_id AND b.user_id = ?)
            """, (FOUNDER_ID, FOUNDER_ID)).fetchall()
            for r in rows:
                conn.execute("UPDATE channels SET added_by = ? WHERE chat_id = ?", (FOUNDER_ID, r['chat_id']))
                conn.execute("UPDATE roles SET role = 'kurucu' WHERE chat_id = ? AND user_id = ?", (r['chat_id'], FOUNDER_ID))
        conn.commit()

_migrate_installer()

def _drop_implicit_founder_roles():
    """Eski sürüm bot sahibini her gruba 'kurucu' yazıyordu; sahibi olmadığı gruplardaki bu kayıtlar silinir."""
    if not FOUNDER_ID:
        return
    with get_db() as conn:
        conn.execute("DELETE FROM roles WHERE user_id = ? AND role = 'kurucu' AND chat_id NOT IN "
                     "(SELECT chat_id FROM channels WHERE owner_id = ? OR added_by = ?)", (FOUNDER_ID, FOUNDER_ID, FOUNDER_ID))
        conn.commit()

_drop_implicit_founder_roles()

def _restore_if_empty() -> str | None:
    """Veritabanı boş açıldıysa (dosya silinmiş/yenilenmiş) en son yedekten geri yükler. Dönüş: yüklenen yedek."""
    with get_db() as conn:
        if conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]:
            return None
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('bot_data_') and f.endswith('.db'))
    except OSError:
        return None
    for name in reversed(files):
        path = os.path.join(BACKUP_DIR, name)
        try:
            src = sqlite3.connect(path)
            try:
                if not src.execute("SELECT COUNT(*) FROM channels").fetchone()[0]:
                    continue
                dst = sqlite3.connect(DB_FILE)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.Error as e:
            logger.warning(f"Yedek okunamadı {name}: {e}")
            continue
        logger.warning(f"Veritabanı boş açıldı; yedekten geri yüklendi: {name}")
        init_db()
        migrate_db()
        _migrate_installer()
        return name
    return None

RESTORED_FROM = _restore_if_empty()

# ═══════════════════════════ RÜTBE HİYERARŞİSİ ═══════════════════════════
# Kurucu (grup sahibi) > Yardımcı Kurucu > Üst Admin > Admin. Herkes sadece kendinden alt rütbeye işlem yapar.
ROLE_LEVELS = {
    'kurucu': 100,
    'yardimci_kurucu': 90,
    'basadmin': 70,
    'admin': 50,
    None: 0
}
ROLE_ORDER = ['kurucu', 'yardimci_kurucu', 'basadmin', 'admin']
ROLE_NAMES = {'kurucu': '👑 Kurucu', 'yardimci_kurucu': '🔱 Yardımcı Kurucu',
              'basadmin': '⭐ Üst Admin', 'admin': '🛡 Admin'}
LVL_KURUCU, LVL_YARDIMCI, LVL_UST, LVL_ADMIN = 100, 90, 70, 50
ADMIN_MAX_MUTE = 86400  # Admin en fazla 24 saat susturabilir

# Bot yetkileri: anahtar → (etiket, en düşük rütbe). Kişiye özel panel bunları rütbenin sınırı içinde kısar.
PERMS = {
    'can_warn':            ('⚠️ Uyarı verme', LVL_ADMIN),
    'can_delete':          ('🗑 Mesaj silme', LVL_ADMIN),
    'can_mute':            ('🔇 Susturma', LVL_ADMIN),
    'can_kick':            ('👢 Atma (kick)', LVL_ADMIN),
    'can_ban':             ('🔨 Ban / ban kaldırma', LVL_UST),
    'can_unwarn':          ('↩️ Uyarı geri alma', LVL_UST),
    'can_pin':             ('📌 Sabitleme', LVL_UST),
    'can_purge':           ('🧹 Toplu silme / yavaş mod', LVL_UST),
    'can_requests':        ('📩 Katılım isteği / itiraz', LVL_UST),
    'can_content':         ('📜 Kurallar / karşılama / notlar', LVL_UST),
    'can_lock':            ('🚨 Acil kilit (açma)', LVL_UST),
    'can_manage_settings': ('⚙️ Koruma ayarları', LVL_YARDIMCI),
    'can_manage_roles':    ('👑 Rütbe verme / alma', LVL_YARDIMCI),
    'can_filters':         ('🧩 Filtre (otomatik yanıt) yönetimi', LVL_ADMIN),
    'can_tag':             ('🏷 Toplu etiketleme (/etiket)', LVL_ADMIN),
}

# Rütbeye göre Telegram admin hakları. Admin'e kısıtlama hakkı verilmez (Telegram'da ban'ı da kapsar);
# admin susturma/atmayı bot komutlarıyla yapar. Admin atama hakkı sadece yardımcı kurucuda.
TG_RIGHTS = {
    'can_delete_messages':    ('🗑 Mesaj silme', LVL_ADMIN),
    'can_invite_users':       ('🔗 Davet linki', LVL_ADMIN),
    'can_restrict_members':   ('🚫 Kısıtlama / ban', LVL_UST),
    'can_pin_messages':       ('📌 Sabitleme', LVL_UST),
    'can_manage_video_chats': ('🎙 Sesli sohbet', LVL_UST),
    'can_manage_topics':      ('💬 Konu yönetimi', LVL_UST),
    'can_post_stories':       ('📖 Hikaye paylaşma', LVL_UST),
    'can_edit_stories':       ('✏️ Hikaye düzenleme', LVL_UST),
    'can_delete_stories':     ('🗑 Hikaye silme', LVL_UST),
    'can_change_info':        ('ℹ️ Grup bilgisi', LVL_YARDIMCI),
    'can_promote_members':    ('⭐ Admin atama', LVL_YARDIMCI),
}

# Telegram/Python hata metinleri kullanıcıya ham gösterilmez; anlaşılır Türkçe karşılığı verilir.
TG_ERRORS = [
    (('not enough rights', 'chat_admin_required', 'have no rights', 'need administrator rights'),
     "Botun bu işlem için yetkisi yok. Botu gerekli yetkilerle yönetici yap."),
    (('right_forbidden',), "Bot, kendisinde olmayan bir yetkiyi başkasına veremez."),
    (('user is an administrator', 'user_admin_invalid', "can't remove chat owner", "can't restrict self",
      'can_not_restrict', 'administrator of the chat'), "Bu kişi grupta yönetici; bu işlem yöneticilere uygulanamaz."),
    (('user not found', 'participant_id_invalid', 'user_not_participant', 'member not found', 'user_id_invalid'),
     "Kullanıcı grupta bulunamadı."),
    (('message to delete not found', 'message to pin not found', 'message not found'), "Mesaj bulunamadı (silinmiş olabilir)."),
    (("message can't be deleted",), "Bu mesaj silinemiyor (48 saatten eski olabilir)."),
    (('retry after', 'too many requests', 'flood'), "Telegram şu an çok fazla istek alıyor, biraz sonra tekrar dene."),
    (('chat not found',), "Sohbet bulunamadı. Bot o sohbette mi?"),
    (('only for supergroups', 'supergroup'), "Bu işlem sadece süper gruplarda çalışır."),
    (('bot was blocked', 'bot can\'t initiate', 'forbidden'), "Bota erişim yok (bot engellenmiş ya da sohbetten çıkarılmış)."),
    (('timed out', 'timeout', 'network'), "Telegram'a ulaşılamadı, biraz sonra tekrar dene."),
]

def friendly_error(e: Exception) -> str:
    low = str(e).lower()
    for keys, text in TG_ERRORS:
        if any(k in low for k in keys):
            return f"❌ {text}"
    logger.warning(f"Beklenmeyen hata: {e!r}")
    return "❌ İşlem yapılamadı, lütfen tekrar dene."

def valid_id(x) -> bool:
    """Telegram kullanıcı/sohbet ID'si makul aralıkta mı? (dev sayılar SQLite'ı çökertir)"""
    try:
        return 0 < abs(int(x)) < 10 ** 16
    except (TypeError, ValueError):
        return False

def role_of_level(level: int) -> str | None:
    return next((r for r in ROLE_ORDER if ROLE_LEVELS[r] <= level), None)

def user_level(chat_id: str, user_id: int) -> int:
    """Kullanıcının bu sohbetteki rütbe seviyesi. Grubun sahibi her zaman 100.
    Bot sahibinin (FOUNDER_ID) başkasının grubunda kendiliğinden rütbesi yoktur."""
    if not valid_id(user_id):
        return 0
    with get_db() as conn:
        row = conn.execute("SELECT role FROM roles WHERE chat_id = ? AND user_id = ?", (str(chat_id), user_id)).fetchone()
        owner = conn.execute("SELECT owner_id, added_by FROM channels WHERE chat_id = ?", (str(chat_id),)).fetchone()
    if owner and user_id in (owner['owner_id'], owner['added_by']):
        return LVL_KURUCU  # Telegram'daki sahip ve botu ekleyen kişi
    return ROLE_LEVELS.get(row['role'] if row else None, 0)

def has_permission(chat_id: str, user_id: int, min_level: int = 50) -> bool:
    return user_level(chat_id, user_id) >= min_level

def perm_overrides(chat_id: str, user_id: int) -> set:
    """Bu kişiye özel olarak kapatılmış bot yetkileri."""
    with get_db() as conn:
        return {r['perm'] for r in conn.execute(
            "SELECT perm FROM user_perm_off WHERE chat_id = ? AND user_id = ?", (str(chat_id), user_id))}

def has_specific_permission(chat_id: str, user_id: int, permission: str) -> bool:
    """Rütbe yeterli mi ve bu yetki kişiye özel kapatılmamış mı?"""
    level = user_level(chat_id, user_id)
    if level >= LVL_KURUCU:
        return True
    if level < PERMS[permission][1]:
        return False
    return permission not in perm_overrides(chat_id, user_id)

def can_act_on(chat_id: str, caller_id: int, target_id: int) -> bool:
    """Hiyerarşi: sadece kendinden düşük rütbeye işlem yapılır."""
    return user_level(chat_id, target_id) < user_level(chat_id, caller_id)

def hierarchy_block(chat_id: str, caller_id: int, target_id: int) -> str | None:
    """Moderasyon hedefi uygun değilse kibar açıklama, uygunsa None."""
    if target_id == caller_id:
        return "Bu işlemi kendine uygulayamazsın."
    if target_id in (cur_bot_id(), GROUP_ANON_BOT_ID, TELEGRAM_SERVICE_ID):
        return "Bu hesaba işlem uygulanamaz."
    if not can_act_on(chat_id, caller_id, target_id):
        role = role_of_level(user_level(chat_id, target_id))
        return f"⛔ {ROLE_NAMES[role] if role else 'Bu kişi'} senin rütbende veya üstünde; işlem yapamazsın."
    return None

async def deny(update: Update, perm: str | None = None, level: int | None = None):
    """Yetki yok mesajı: gereken rütbeyi söyler."""
    need = PERMS[perm][1] if perm else level
    text = f"⛔ Yetkin yok! ({ROLE_NAMES[role_of_level(need)]} ve üstü)"
    query = getattr(update, 'callback_query', None)
    if query:
        try:
            await query.answer(text, show_alert=True)
            return
        except TelegramError:  # sorgu zaten yanıtlanmışsa uyarı mesaj olarak gider
            pass
    if update.effective_message:
        await update.effective_message.reply_text(text)

async def _is_real_chat_admin(chat_id: str, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ('administrator', 'creator')
    except Exception:
        return False

async def _is_bot_admin(chat_id: str) -> bool:
    if not cur_bot_id():
        return True
    try:
        me = await bot.get_chat_member(chat_id, bot_id_for(chat_id))
        return me.status in ('administrator', 'creator')
    except Exception:
        return False

async def require(update: Update, chat_id: str, perm: str) -> bool:
    user_id = update.effective_user.id if update.effective_user else 0
    if not has_specific_permission(chat_id, user_id, perm):
        await deny(update, perm)
        return False
    if chat_id and str(chat_id).startswith('-'):
        if not await _is_real_chat_admin(str(chat_id), user_id):
            await update.effective_message.reply_text("⛔ Bu komut için grupta gerçekten yönetici olmalısın.")
            return False
        if perm in ('can_ban', 'can_mute', 'can_warn', 'can_delete', 'can_manage_settings', 'can_manage_roles') and not await _is_bot_admin(str(chat_id)):
            await update.effective_message.reply_text("⛔ Bot bu grupta yönetici değil veya yetkileri yetersiz.")
            return False
    return True

def tg_rights_for(level: int, off: set = frozenset()) -> dict:
    """Rütbenin Telegram admin hakları (kişiye özel kapatılanlar hariç)."""
    return {k: (lvl <= level and k not in off) for k, (_, lvl) in TG_RIGHTS.items()}  # can_manage_chat ayrıca verilir

async def sync_creator(chat_id: str, admins) -> None:
    """Telegram'daki grup sahibi kurucu rütbesini alır; eski kayıtlı kurucu yardımcı kurucuya iner."""
    creator = next((a.user.id for a in admins if getattr(a, 'status', None) == 'creator'), None)
    if not creator:
        return
    # _db_lock alınmaz: admin listesi kilit tutulurken de istenebilir (asyncio.Lock yeniden girilemez)
    with get_db() as conn:
        ch = conn.execute("SELECT owner_id, added_by FROM channels WHERE chat_id = ?", (chat_id,)).fetchone()
        if not ch:
            return
        row = conn.execute("SELECT role FROM roles WHERE chat_id = ? AND user_id = ?", (chat_id, creator)).fetchone()
        if ch['owner_id'] == creator and row and row['role'] == 'kurucu':
            return
        # eski kayıtlı "kurucu" yardımcıya iner; botu ekleyen kişi kurucu kalır
        conn.execute("UPDATE roles SET role = 'yardimci_kurucu' WHERE chat_id = ? AND role = 'kurucu' "
                     "AND user_id NOT IN (?, ?)", (chat_id, creator, ch['added_by'] or 0))
        conn.execute("INSERT OR REPLACE INTO roles (chat_id, user_id, role) VALUES (?, ?, 'kurucu')", (chat_id, creator))
        conn.execute("UPDATE channels SET owner_id = ? WHERE chat_id = ?", (creator, chat_id))
        conn.commit()
    _invalidate_settings(chat_id)
    logger.info(f"Kurucu eşitlendi {chat_id}: {creator}")

_settings_cache: dict = {}
SETTINGS_CACHE_TTL = 60

def _invalidate_settings(chat_id: str | None = None):
    if chat_id is None:
        _settings_cache.clear()
    else:
        _settings_cache.pop(str(chat_id), None)

def get_channel_settings(chat_id: str) -> dict | None:
    """Ayarları önbellekten döndürür (her mesajda DB+JSON okumasını önler).
    Çağıran kod sözlüğü değiştirip save_channel_settings ile kaydedebilir; kopya döner."""
    if chat_id is None:
        return None
    chat_id = str(chat_id)
    now = time.time()
    cached = _settings_cache.get(chat_id)
    if cached and now - cached[0] < SETTINGS_CACHE_TTL:
        return copy.deepcopy(cached[1]) if cached[1] is not None else None
    with get_db() as conn:
        row = conn.execute(
            "SELECT owner_id, log_chat_id, chat_type, settings, stats, invites, added_by FROM channels WHERE chat_id = ?",
            (chat_id,)
        ).fetchone()
    if not row:
        _settings_cache[chat_id] = (now, None)
        return None
    data = dict(row)
    data['settings'] = {**_default_channel_settings(), **json.loads(data['settings'])}
    data['stats'] = json.loads(data['stats'])
    data['invites'] = json.loads(data['invites'] or '{}')
    data['owner'] = data.pop('owner_id')
    _settings_cache[chat_id] = (now, data)
    return copy.deepcopy(data)

def save_channel_settings(chat_id: str, data: dict):
    chat_id = str(chat_id)
    settings_json = json.dumps(data['settings'], ensure_ascii=False)
    stats_json = json.dumps(data.get('stats', {}))
    invites_json = json.dumps(data.get('invites', {}))
    with get_db() as conn:
        # REPLACE satırı silip yeniden yazar ve added_by / bot_id / created_at sıfırlanırdı: sadece bu sütunlar güncellenir
        conn.execute("""
            INSERT INTO channels
            (chat_id, owner_id, log_chat_id, chat_type, settings, stats, invites)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET owner_id = excluded.owner_id, log_chat_id = excluded.log_chat_id,
                chat_type = excluded.chat_type, settings = excluded.settings, stats = excluded.stats,
                invites = excluded.invites
        """, (
            chat_id, data['owner'], data.get('log_chat_id'),
            data.get('chat_type'), settings_json, stats_json, invites_json
        ))
        conn.commit()
    _settings_cache[chat_id] = (time.time(), copy.deepcopy(data))

async def send_log(chat_id: str, message: str, parse_mode: str | None = None, reply_markup=None):
    channel = get_channel_settings(chat_id)
    log_id = channel.get('log_chat_id') if channel else None
    if log_id:
        try:
            await safe_send_message(log_id, message, parse_mode=parse_mode, disable_web_page_preview=True,
                                    reply_markup=reply_markup)
        except Exception as e:
            logger.debug(f"Log gönderilemedi ({chat_id}): {e}")

# ─────────────────────────── ORTAK YARDIMCILAR ───────────────────────────

def mention(user) -> str:
    """Kullanıcı adı olsun olmasın tıklanabilir HTML etiketi."""
    if user is None:
        return "bilinmeyen"
    name = (getattr(user, 'first_name', None) or getattr(user, 'username', None) or str(user.id))
    return mention_html(user.id, name)

GREEN = KeyboardButtonStyle.SUCCESS   # açık / onay
RED = KeyboardButtonStyle.DANGER      # kapalı / ban / sil
BLUE = KeyboardButtonStyle.PRIMARY    # menü / geçiş

def ibtn(text: str, data: str | None = None, style: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    """Renkli inline buton (Telegram'ın Şubat 2026 sonrası sürümlerinde renkli, eskilerde normal görünür)."""
    return InlineKeyboardButton(text, callback_data=data, url=url, style=style)

def _emoji(name: str, html_mode: bool = False) -> str:
    e = EMOJIS.get(name, {})
    fallback = e.get('fallback') or ''
    eid = (e.get('id') or '').strip()
    if html_mode and eid:
        return f'<tg-emoji emoji-id="{html.escape(eid)}">{fallback}</tg-emoji>'
    return fallback

def _strip_custom_emoji_tags(text: str) -> str:
    return re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text or '')

async def safe_send_message(chat_id, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if 'emoji' in msg or 'entity' in msg:
            clean = _strip_custom_emoji_tags(text)
            return await bot.send_message(chat_id, clean, **kwargs)
        raise

def toggle_btn(label: str, on, data: str) -> InlineKeyboardButton:
    icon = _emoji('success') if on else _emoji('error')
    return ibtn(f"{icon} {label}", data, GREEN if on else RED)

def thread_kw(msg, chat_id=None) -> dict:
    """Konulara (topic) bölünmüş gruplarda bot mesajının aynı konuya gitmesi için send_message parametresi."""
    if msg is None or not getattr(msg, 'is_topic_message', False) or not msg.message_thread_id:
        return {}
    if chat_id is not None and str(msg.chat_id) != str(chat_id):
        return {}
    return {'message_thread_id': msg.message_thread_id}

def fold(section: str) -> str:
    """İlk satır kalın başlık, gövde açılır alıntı (<blockquote expandable>) olarak döner. Girdi düz metindir."""
    head, _, body = section.strip().partition("\n")
    return f"<b>{html.escape(head)}</b>\n<blockquote expandable>{html.escape(body.strip())}</blockquote>"

def parse_duration(s: str) -> int | None:
    """30m / 2h / 7d / 45s → saniye"""
    if not s:
        return None
    m = re.fullmatch(r'(\d+)\s*([smhd])', s.strip().lower())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    return val * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]

def human_duration(sec: int) -> str:
    if sec % 86400 == 0:
        return f"{sec // 86400} gün"
    if sec % 3600 == 0:
        return f"{sec // 3600} saat"
    if sec % 60 == 0:
        return f"{sec // 60} dakika"
    return f"{sec} saniye"

_tg_admin_cache: dict = {}
TG_ADMIN_CACHE_TTL = 300

async def get_tg_admin_ids(chat_id: str) -> set:
    """Telegram'daki gerçek grup adminleri (5 dk önbellekli)."""
    chat_id = str(chat_id)
    now = time.time()
    cached = _tg_admin_cache.get(chat_id)
    if cached and now - cached[0] < TG_ADMIN_CACHE_TTL:
        return cached[1]
    try:
        admins = await bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins}
        await sync_creator(chat_id, admins)
    except Exception as e:
        logger.debug(f"Admin listesi alınamadı {chat_id}: {e}")
        ids = cached[1] if cached else set()
    _tg_admin_cache[chat_id] = (now, ids)
    return ids

def invalidate_admin_cache(chat_id: str):
    _tg_admin_cache.pop(str(chat_id), None)

async def is_staff_user(chat_id: str, user_id: int, channel: dict | None = None) -> bool:
    """Bot rolleri + Telegram adminleri + muaf liste."""
    if not user_id:
        return False
    if user_id in (TELEGRAM_SERVICE_ID, GROUP_ANON_BOT_ID, cur_bot_id()):
        return True
    if has_permission(chat_id, user_id, 50):
        return True
    if channel is None:
        channel = get_channel_settings(chat_id)
    if channel and user_id in channel['settings'].get('spam_whitelist', []):
        return True
    return user_id in await get_tg_admin_ids(chat_id)

async def is_exempt_message(chat_id: str, msg, channel: dict | None = None) -> bool:
    """Koruma filtrelerinden muaf mesajlar:
    bağlı kanal otomatik iletileri, anonim adminler, kanalın kendi gönderileri,
    bot yetkilileri, Telegram adminleri ve muaf liste."""
    if msg is None:
        return True
    if getattr(msg, 'is_automatic_forward', False):
        return True
    if msg.sender_chat and str(msg.sender_chat.id) == str(msg.chat_id):
        return True  # anonim admin veya kanalın kendi gönderisi
    if msg.chat and msg.chat.type == 'channel':
        return True
    user = msg.from_user
    if not user:
        return True
    if msg.sender_chat:
        return False  # "kanal olarak" yazan kullanıcı — muaf değil
    return await is_staff_user(chat_id, user.id, channel)

def message_text(msg) -> str:
    return (msg.text or msg.caption or '') if msg else ''

def message_entities(msg):
    return list(msg.entities or ()) + list(msg.caption_entities or ())


def _get_effective_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if update.effective_chat.type == 'private':
        return context.user_data.get('selected_channel')
    return str(update.effective_chat.id)

def _default_channel_settings():
    return {
        'spam_protection': False,
        'word_ban_enabled': False,
        'banned_words': [],
        'anti_forward': False,
        'anti_media_flood': False,
        'media_flood_limit': 5,
        'media_flood_timeframe': 10,
        'anti_spam_flood': False,
        'flood_limit': 7,
        'flood_timeframe': 5,
        'anti_link': False,
        'captcha_enabled': False,
        'captcha_timeout': 120,
        'anti_raid': False,
        'raid_limit': 10,
        'raid_timeframe': 30,
        'auto_accept': False,
        'auto_reject': False,
        'auto_reject_bot': False,
        'welcome_msg': 'Merhaba {kullanıcı}, {kanal} grubuna hoş geldin!',
        'warn_limit': 5,
        'spam_whitelist': [],
        'rules': '',
        # Uyarı limiti dolunca uygulanacak ceza: ban / tempban / kick / mute
        'warn_action': 'ban',
        'warn_action_duration': 86400,
        # Yeni üye kısıtlaması: katıldıktan sonraki X dakika link/medya/forward yasak (0 = kapalı)
        'newbie_minutes': 0,
        # Link filtresinden muaf alan adları
        'link_whitelist': [],
        # Kullanıcı adı olmayan yeni üyeleri sustur (varsayılan kapalı; çok sayıda gerçek kullanıcıyı etkiler)
        'restrict_no_username': False,
        # Koruma başına ceza (panel): delete / warn / mute / kick / ban
        'action_link': 'warn', 'action_word': 'warn', 'action_spam': 'warn',
        'action_flood': 'mute', 'action_forward': 'mute', 'action_media': 'mute',
        # "Sustur" cezası süresi (dk) — flood/forward/medya kademeli süre kullanır
        'mute_minutes': 60,
        # Katılım isteği gönderene özelden captcha
        'join_captcha': False,
        'welcome_enabled': True,
        # Uygunsuz medya kalkanı (porno / engelli medya / tehlikeli dosya)
        'media_shield': False, 'action_badmedia': 'ban', 'nsfw_scan': True, 'file_block': True,
        'media_autolock': True, 'media_lock_minutes': 30,
        # Geç düzenleme koruması
        'edit_guard': False, 'edit_guard_minutes': 5, 'edit_notify': True,
        # Rapor sistemi
        'reports_enabled': True,
        # Admin kurtarma: güvenilir kişiler (en fazla 3)
        'recovery_ids': [], 'recovery_autorestore': False,
        # Karşılama ekstraları: eskisini sil, X dk sonra sil (0 = kapalı), toplu katılımda tek mesaj, özelden gönder
        'welcome_clean': False, 'welcome_autodel': 0, 'welcome_batch': True, 'welcome_dm': False,
        'goodbye_enabled': False,
        # Kanal zorunluluğu
        'fsub_enabled': False, 'fsub_channel': None, 'fsub_title': None, 'fsub_link': None,
        # /etiket: bir mesajda kaç kişi, isim/emoji, sadece son 7 günün aktifleri
        'tag_size': 5, 'tag_style': 'name', 'tag_active_only': False,
        # Topluluk koruması: ortak kara liste (botun başka grubunda banlı) + CAS, isim takibi, oylamalı susturma
        'shared_blacklist': True, 'blacklist_action': 'mute', 'cas_enabled': False, 'name_track': True,
        'vote_mute': True, 'vote_needed': 5, 'vote_mute_minutes': 60,
    }

async def _register_chat(chat_id: str, owner_id: int, chat_type: str, added_by: int | None = -1):
    """Sohbeti kaydeder. added_by: botu ekleyen kişi (varsayılan owner_id); o da kurucu sayılır."""
    if added_by == -1:
        added_by = owner_id
    default_settings = _default_channel_settings()
    default_stats = {'bans': 0, 'kicks': 0, 'spams': 0, 'joins': 0, 'requests': 0, 'mutes': 0}
    async with _db_lock:
        with get_db() as conn:
            existing = conn.execute("SELECT owner_id, added_by FROM channels WHERE chat_id = ?", (chat_id,)).fetchone()
            if existing and added_by and not existing['added_by']:
                # kaydı olan sohbete bot yeniden eklendi: ekleyen kişi (bilinmiyorduysa) kurucu olur
                conn.execute("UPDATE channels SET added_by = ? WHERE chat_id = ?", (added_by, chat_id))
                conn.execute("INSERT OR REPLACE INTO roles (chat_id, user_id, role) VALUES (?, ?, 'kurucu')", (chat_id, added_by))
                conn.commit()
                _invalidate_settings(chat_id)
            if not existing:
                conn.execute("""
                    INSERT INTO channels
                    (chat_id, owner_id, log_chat_id, chat_type, settings, stats, invites, added_by, bot_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    chat_id, owner_id, None,
                    chat_type,
                    json.dumps(default_settings, ensure_ascii=False),
                    json.dumps(default_stats),
                    json.dumps({}),
                    added_by or None,
                    cur_bot_id() if cur_bot_id() != BOT_ID else None
                ))
                _chat_bot_map.pop(str(chat_id), None)
                for uid in {owner_id, added_by} - {0, None}:
                    conn.execute("INSERT OR REPLACE INTO roles (chat_id, user_id, role) VALUES (?, ?, 'kurucu')",
                                 (chat_id, uid))
                conn.commit()
                _invalidate_settings(chat_id)
                return True
            return False

async def handle_my_chat_member(update: Update, context):
    
    member_update = update.my_chat_member
    if not member_update:
        return

    new_status = member_update.new_chat_member.status
    chat = member_update.chat
    chat_id = str(chat.id)

    invalidate_admin_cache(chat_id)
    await handle_clone_protection(update, context)

    if new_status in ['member', 'administrator'] and is_blocked(chat_id):
        try:
            await context.bot.leave_chat(chat.id)
            logger.info(f"Engelli sohbetten çıkıldı: {chat_id}")
        except Exception as e:
            logger.debug(f"Engelli sohbetten çıkılamadı {chat_id}: {e}")
        return

    if new_status in ('left', 'kicked') and chat_bot_id(chat_id) == context.bot.id and get_channel_settings(chat_id):
        set_chat_bot(chat_id, -1)  # yöneten bot çıkarıldı: gruptaki diğer botumuz sahiplenebilir
    if new_status in ['member', 'administrator']:
        if get_channel_settings(chat_id) and chat_bot_id(chat_id) != context.bot.id:
            set_chat_bot(chat_id, context.bot.id if context.bot.id != BOT_ID else 0)  # son eklenen botumuz yönetir
        owner_id = member_update.from_user.id if member_update.from_user else 0
        chat_type = chat.type
        is_new = await _register_chat(chat_id, owner_id, chat_type)
        if is_new:
            try:
                await bot.send_message(
                    owner_id,
                    f"Bot eklendi: {chat.title or chat_id}\n"
                    f"Tip: {chat_type}\n"
                    f"ID: {chat_id}\n\n"
                    f"Komutlar icin /help yaz."
                )
            except Exception as e:
                logger.debug(f"handle_my_chat_member: {e}")
            await send_log(chat_id, f"Bot eklendi: {chat.title or chat_id} ({chat_type}) | owner: {owner_id}")
            if chat_type in ('group', 'supergroup') and owner_id:
                await send_setup_wizard(owner_id, chat_id, context)

async def handle_bot_added(update: Update, context):
    
    if not update.message or not update.message.new_chat_members:
        return
    for member in update.message.new_chat_members:
        if member.id != context.bot.id:
            continue
        chat_id = str(update.message.chat_id)
        owner_id = update.message.from_user.id
        is_new = await _register_chat(chat_id, owner_id, update.message.chat.type)
        if is_new:
            await update.message.reply_text("Bot eklendi ve kaydedildi! /help ile komutlari gorebilirsin.")

async def resolve_user(chat_id, user_ref=None, replied_user=None):
    if replied_user:
        try:
            member = await bot.get_chat_member(chat_id, replied_user.id)
            return replied_user.id, member
        except Exception:
            return None, None
    if not user_ref:
        return None, None
    user_ref = str(user_ref).strip().lstrip('@')

    if user_ref.isdigit():
        user_id = int(user_ref)
        if not valid_id(user_id):
            return None, None
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            return user_id, member
        except Exception:
            return None, None

    with get_db() as conn:
        row = conn.execute(
            "SELECT DISTINCT user_id FROM message_stats WHERE chat_id = ? AND LOWER(username) = LOWER(?) LIMIT 1",
            (chat_id, user_ref)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT user_id FROM users WHERE chat_id = ? AND LOWER(username) = LOWER(?) LIMIT 1",
                (chat_id, user_ref)
            ).fetchone()
        if row:
            try:
                member = await bot.get_chat_member(chat_id, row['user_id'])
                return row['user_id'], member
            except Exception as e:
                logger.debug(f"resolve_user: {e}")

    ub = await get_userbot()
    if ub:
        try:
            entity = await ub.get_entity(f"@{user_ref}")
            user_id = entity.id
            try:
                member = await bot.get_chat_member(chat_id, user_id)
            except Exception:

                class _FakeMember:
                    class user:
                        id = entity.id
                        username = getattr(entity, 'username', None)
                        first_name = getattr(entity, 'first_name', '') or ''
                        is_bot = getattr(entity, 'bot', False)
                    status = 'left'
                member = _FakeMember()
            return user_id, member
        except FloodWaitError as e:
            logger.warning(f"Telethon FloodWait: {e.seconds}s")
        except Exception as e:
            logger.debug(f"Telethon resolve hata: {e}")

    try:
        chat_member = await bot.get_chat_member(chat_id, f"@{user_ref}")
        return chat_member.user.id, chat_member
    except Exception:
        return None, None

async def log_mod_action(chat_id: str, action: str, target_id: int, target_uname: str, by_id: int, by_uname: str, reason: str = ""):
    
    async with _db_lock:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO mod_log (chat_id, action, target_user_id, target_username, by_user_id, by_username, reason, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, action, target_id, target_uname, by_id, by_uname, reason, time.time()))
            conn.commit()

async def apply_punishment(chat_id: str, user_id: int, username: str, reason: str, channel: dict, user=None,
                           thread: dict | None = None, actor_id: int | None = None, actor_username: str = 'bot'):
    """Uyarı verir; limit dolunca ayarlanan cezayı (ban/tempban/kick/mute) uygular.
    Mesajların altına yetkililer için moderasyon butonları eklenir; thread = konu (topic) parametresi."""
    if await is_staff_user(chat_id, user_id, channel):
        return
    thread = thread or {}
    async with _db_lock:
        with get_db() as conn:
            row = conn.execute(
                "SELECT warn_count FROM warnings WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id)
            ).fetchone()
            warn_count = (row['warn_count'] if row else 0) + 1
            conn.execute("""
                INSERT OR REPLACE INTO warnings (chat_id, user_id, warn_count, last_warn_at)
                VALUES (?, ?, ?, ?)
            """, (chat_id, user_id, warn_count, time.time()))
            conn.commit()

    settings = channel['settings']
    actor_id = actor_id or cur_bot_id()
    warn_limit = int(settings.get('warn_limit', 5) or 5)
    who = mention(user) if user else html.escape(f"@{username}" if username else str(user_id))
    reason_h = html.escape(reason)

    if warn_count < warn_limit:
        markup = mod_markup(chat_id, user_id, 'warn')
        try:
            await safe_send_message(chat_id, f"{_emoji('warn', True)} {who} {reason_h} → <b>{warn_count}/{warn_limit}</b> uyarı",
                                    parse_mode=ParseMode.HTML, reply_markup=markup, **thread)
        except Exception as e:
            logger.debug(f"Uyarı mesajı gönderilemedi: {e}")
        await send_log(chat_id, f"{_emoji('warn', True)} {who} {reason_h} | Uyarı: {warn_count}/{warn_limit} | {chat_id}", ParseMode.HTML,
                       reply_markup=markup)
        await log_mod_action(chat_id, 'warn', user_id, username or '', actor_id, actor_username or '', reason)
        return

    action = settings.get('warn_action', 'ban')
    duration = int(settings.get('warn_action_duration', 86400) or 86400)
    now = time.time()
    try:
        if action == 'tempban':
            await bot.ban_chat_member(chat_id, user_id, until_date=int(now + duration))
            label = f"geçici ban ({human_duration(duration)})"
            channel['stats']['bans'] = channel['stats'].get('bans', 0) + 1
        elif action == 'kick':
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            label = "gruptan atıldı"
            channel['stats']['kicks'] = channel['stats'].get('kicks', 0) + 1
        elif action == 'mute':
            await bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions.no_permissions(),
                                           until_date=int(now + duration))
            label = f"susturuldu ({human_duration(duration)})"
            channel['stats']['mutes'] = channel['stats'].get('mutes', 0) + 1
        else:
            await bot.ban_chat_member(chat_id, user_id)
            label = "kalıcı ban"
            channel['stats']['bans'] = channel['stats'].get('bans', 0) + 1

        save_channel_settings(chat_id, channel)
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                if action in ('ban', 'tempban'):
                    conn.execute("""
                        INSERT OR REPLACE INTO ban_list (chat_id, user_id, username, reason, banned_at, banned_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (chat_id, user_id, username, reason, now, actor_id))
                if action == 'tempban':
                    conn.execute("INSERT OR REPLACE INTO temp_bans (chat_id, user_id, username, unban_at) VALUES (?, ?, ?, ?)",
                                 (chat_id, user_id, username, int(now + duration)))
                if action == 'mute':
                    conn.execute("""
                        INSERT OR REPLACE INTO mute_list (chat_id, user_id, username, until_date, muted_at, muted_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (chat_id, user_id, username, now + duration, now, actor_id))
                conn.commit()

        kind = 'mute' if action == 'mute' else (None if action == 'kick' else 'ban')
        markup = mod_markup(chat_id, user_id, kind) if kind else None
        extra = "\n📨 İtiraz için bota özelden /itiraz yazabilir." if action in ('ban', 'tempban') else ""
        await safe_send_message(chat_id, f"{_emoji('ban', True)} {who} {warn_limit} uyarıya ulaştı → <b>{label}</b> ({reason_h}){extra}",
                                parse_mode=ParseMode.HTML, reply_markup=markup, **thread)
        await send_log(chat_id, f"🚫 {who} {label} | Sebep: {reason_h} | {chat_id}", ParseMode.HTML, reply_markup=markup)
        await log_mod_action(chat_id, label, user_id, username or '', actor_id, actor_username or '', reason)
    except Exception as e:
        logger.error(f"Ceza uygulanamadı ({chat_id}/{user_id}): {e}")

# ─────────────────────────── KORUMA CEZALARI + MODERASYON BUTONLARI ───────────────────────────

async def enforce_action(chat_id: str, msg, channel: dict, prot: str, reason: str, bot_data: dict,
                         mute_minutes: int | None = None):
    """Korumanın panelden seçilen cezasını uygular (ihlal mesajı önceden silinmiş olmalı).
    delete: sadece sil · warn: uyarı (limitte warn_action) · mute · kick · ban.
    mute_minutes verilirse (flood/forward/medya kademeli süre) ayar yerine o kullanılır."""
    user = msg.from_user
    action = channel['settings'].get(f'action_{prot}') or PROTECTIONS[prot][2]
    tkw = thread_kw(msg, chat_id)
    username = user.username or user.first_name
    if action == 'warn':
        await apply_punishment(chat_id, user.id, username, reason, channel, user=user, thread=tkw)
        return
    who = mention(user)
    reason_h = html.escape(reason)
    if action == 'delete':
        await send_log(chat_id, f"🗑 {reason_h} → {who} mesajı silindi | {chat_id}", ParseMode.HTML)
        return
    if await is_staff_user(chat_id, user.id, channel):
        return
    now = time.time()
    markup = None
    try:
        if action == 'mute':
            minutes = mute_minutes or int(channel['settings'].get('mute_minutes', 60) or 60)
            until = now + minutes * 60
            await bot.restrict_chat_member(chat_id, user.id, permissions=ChatPermissions.no_permissions(),
                                           until_date=int(until))
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO mute_list (chat_id, user_id, username, until_date, muted_at, muted_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (chat_id, user.id, username, until, now, cur_bot_id()))
                    conn.commit()
            text = f"🔇 {who} {reason_h} → {human_duration(minutes * 60)} susturuldu"
            stat, markup = 'mutes', mod_markup(chat_id, user.id, 'mute')
        elif action == 'kick':
            await bot.ban_chat_member(chat_id, user.id)
            await bot.unban_chat_member(chat_id, user.id, only_if_banned=True)
            text, stat = f"👢 {who} {reason_h} → gruptan atıldı", 'kicks'
        else:
            await bot.ban_chat_member(chat_id, user.id)
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO ban_list (chat_id, user_id, username, reason, banned_at, banned_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (chat_id, user.id, username, reason, now, cur_bot_id()))
                    conn.commit()
            text = f"🚫 {who} {reason_h} → banlandı\n📨 İtiraz için bota özelden /itiraz yazabilir."
            stat, markup = 'bans', mod_markup(chat_id, user.id, 'ban')
    except Exception as e:
        logger.error(f"Koruma cezası uygulanamadı ({prot}/{action}, {chat_id}/{user.id}): {e}")
        return
    channel['stats'][stat] = channel['stats'].get(stat, 0) + 1
    save_channel_settings(chat_id, channel)
    key = f"flood_notice_{chat_id}_{user.id}"  # aynı kullanıcı için 30 sn'de tek grup duyurusu
    if now - bot_data.get(key, 0) > 30:
        bot_data[key] = now
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup, **tkw)
        except Exception as e:
            logger.debug(f"Ceza duyurusu gönderilemedi: {e}")
    await send_log(chat_id, f"{text} | {chat_id}", ParseMode.HTML, reply_markup=markup)
    await log_mod_action(chat_id, f"{prot}:{action}", user.id, username, cur_bot_id(), 'bot', reason)

# ═══════════════════════════ RAPOR SİSTEMİ (/report, @admin) ═══════════════════════════
REPORT_COOLDOWN = 60
REPORT_PERMS = {'del': 'can_delete', 'warn': 'can_warn', 'mute': 'can_mute', 'ban': 'can_ban'}
REPORT_RESULTS = {'del': "🗑 Mesaj silindi", 'warn': "⚠️ Silindi + uyarıldı", 'mute': "🔇 Silindi + 1 saat susturuldu",
                  'ban': "🚫 Silindi + banlandı", 'ok': "✅ Görmezden gelindi"}
_report_last: dict = {}

def _report_recipients(chat_id: str, channel: dict) -> list:
    """Bu gruptaki bot yetkilileri (bot kurucusu yalnızca grubun sahibiyse)."""
    with get_db() as conn:
        ids = [r['user_id'] for r in conn.execute("SELECT user_id FROM roles WHERE chat_id = ?", (chat_id,))]
    return list(dict.fromkeys(ids))[:25]

def report_markup(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ibtn("🗑 Sil", f"rp|del|{rid}", RED), ibtn("⚠️ Uyar", f"rp|warn|{rid}")],
        [ibtn("🔇 Sustur 1s", f"rp|mute|{rid}"), ibtn("🚫 Banla", f"rp|ban|{rid}", RED)],
        [ibtn("✅ Görmezden gel", f"rp|ok|{rid}", GREEN)],
    ])

async def _temp_reply(msg, text: str, context, seconds: int = 10):
    try:
        sent = await msg.reply_text(text, parse_mode=ParseMode.HTML)
        if context.job_queue:
            context.job_queue.run_once(_delete_message_job, seconds, data={'chat_id': sent.chat_id, 'message_id': sent.message_id})
    except Exception as e:
        logger.debug(f"Geçici cevap gönderilemedi: {e}")

async def cmd_report(update: Update, context):
    """/report [sebep] veya @admin — yanıtlanan mesajı yetkililere bildirir."""
    msg = update.effective_message
    reporter = update.effective_user
    if msg.chat.type not in ('group', 'supergroup'):
        await msg.reply_text("Rapor, grupta bir mesaja yanıt verilerek gönderilir: /report [sebep]")
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('reports_enabled', True) or not reporter:
        return
    target_msg = msg.reply_to_message
    if not target_msg or not target_msg.from_user or target_msg.from_user.id == reporter.id:
        await _temp_reply(msg, "Raporlamak için bir mesaja yanıt vererek /report yaz.", context)
        return
    target = target_msg.from_user
    if await is_staff_user(chat_id, target.id, channel):
        await _temp_reply(msg, "Yetkililer raporlanamaz.", context)
        return
    now = time.time()
    key = (chat_id, reporter.id)
    if now - _report_last.get(key, 0) < REPORT_COOLDOWN:
        await _temp_reply(msg, "Çok sık rapor gönderiyorsun, biraz bekle.", context)
        return
    with get_db() as conn:
        dup = conn.execute("SELECT id FROM reports WHERE chat_id = ? AND message_id = ?",
                           (chat_id, target_msg.message_id)).fetchone()
    if dup:
        await _temp_reply(msg, "Bu mesaj zaten raporlandı.", context)
        return
    _report_last[key] = now
    reason = ' '.join(context.args) if context.args else (msg.text or '').partition(' ')[2] if (msg.text or '').startswith('@') else ''
    content = message_text(target_msg) or f"[{_get_msg_type(target_msg)}]"
    async with _db_lock:
        with get_db() as conn:
            rid = conn.execute("INSERT INTO reports (chat_id, message_id, reporter_id, target_id, created_at) "
                               "VALUES (?, ?, ?, ?, ?)", (chat_id, target_msg.message_id, reporter.id, target.id, now)).lastrowid
            conn.commit()
    text = (f"🚩 <b>Yeni rapor</b> #{rid}\nGrup: <b>{html.escape(await _chat_title(chat_id))}</b>\n"
            f"Raporlayan: {mention(reporter)}\nRaporlanan: {mention(target)} (<code>{target.id}</code>)\n"
            + (f"Sebep: {html.escape(reason[:300])}\n" if reason.strip() else "")
            + f"<blockquote expandable>{html.escape(content[:1500])}</blockquote>"
            + (f"\n<a href=\"{target_msg.link}\">Mesaja git</a>" if target_msg.link else ""))
    copies = []
    for uid in _report_recipients(chat_id, channel):
        try:
            sent = await bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=report_markup(rid),
                                          disable_web_page_preview=True)
            copies.append([uid, sent.message_id])
        except Exception as e:
            logger.debug(f"Rapor iletilemedi ({uid}): {e}")
    log_id = channel.get('log_chat_id')
    if log_id:
        try:
            sent = await bot.send_message(log_id, text, parse_mode=ParseMode.HTML, reply_markup=report_markup(rid),
                                          disable_web_page_preview=True)
            copies.append([log_id, sent.message_id])
        except Exception as e:
            logger.debug(f"Rapor log kanalına iletilemedi: {e}")
    with get_db() as conn:
        conn.execute("UPDATE reports SET text = ?, copies = ? WHERE id = ?", (text, json.dumps(copies), rid))
        conn.commit()
    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"Rapor komutu silinemedi: {e}")
    try:
        ack = await bot.send_message(chat_id, f"✅ {mention(reporter)}, raporun yetkililere iletildi."
                                     if copies else f"⚠️ {mention(reporter)}, rapor alındı ama şu an ulaşılabilir yetkili yok.",
                                     parse_mode=ParseMode.HTML, **thread_kw(msg, chat_id))
        if context.job_queue:
            context.job_queue.run_once(_delete_message_job, 10, data={'chat_id': chat_id, 'message_id': ack.message_id})
    except Exception as e:
        logger.debug(f"Rapor onayı gönderilemedi: {e}")

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """rp|<işlem>|<rapor no> — yetkili DM'sindeki rapor butonları; tüm kopyalar sonuçla güncellenir."""
    query = update.callback_query
    try:
        _, act, rid = query.data.split('|')
        rid = int(rid)
    except ValueError:
        await query.answer("Hata.", show_alert=True)
        return
    with get_db() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
    if not row or act not in REPORT_RESULTS:
        await query.answer("Rapor bulunamadı.", show_alert=True)
        return
    if row['status'] != 'open':
        await query.answer(f"Bu rapor zaten işlendi: {REPORT_RESULTS.get(row['status'], row['status'])}", show_alert=True)
        return
    cid, target, clicker = row['chat_id'], row['target_id'], query.from_user
    channel = get_channel_settings(cid)
    allowed = has_permission(cid, clicker.id, LVL_ADMIN) if act == 'ok' else has_specific_permission(cid, clicker.id, REPORT_PERMS[act])
    if not channel or not allowed:
        await deny(update, None if act == 'ok' else REPORT_PERMS[act], LVL_ADMIN)
        return
    try:
        if act != 'ok':
            try:
                await bot.delete_message(cid, row['message_id'])
            except Exception as e:
                logger.debug(f"Raporlanan mesaj silinemedi: {e}")
        if act == 'warn':
            try:
                tuser = (await bot.get_chat_member(cid, target)).user
            except Exception:
                tuser = None
            await apply_punishment(cid, target, (tuser.username or tuser.first_name) if tuser else '', "rapor", channel, user=tuser)
        elif act in ('mute', 'ban'):
            if await is_staff_user(cid, target, channel):
                await query.answer("Yetkililere uygulanamaz.", show_alert=True)
                return
            await _apply_mod_action(cid, 'mu' if act == 'mute' else 'bn', target, clicker, channel)
    except Exception as e:
        await query.answer(friendly_error(e)[:190], show_alert=True)
        return
    result = f"{REPORT_RESULTS[act]} · {mention(clicker)}"
    with get_db() as conn:
        conn.execute("UPDATE reports SET status = ?, handled_by = ? WHERE id = ?", (act, clicker.id, rid))
        conn.commit()
    await query.answer(REPORT_RESULTS[act])
    for chat, mid in json.loads(row['copies'] or '[]'):
        try:
            await bot.edit_message_text(f"{row['text']}\n\n— {result}", chat_id=chat, message_id=mid,
                                        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.debug(f"Rapor kopyası güncellenemedi ({chat}): {e}")
    await log_mod_action(cid, f"rapor:{act}", target, '', clicker.id, clicker.username or '', f"rapor #{rid}")

def mod_markup(chat_id, user_id: int, kind: str) -> InlineKeyboardMarkup:
    """Bot mesajlarının altındaki yetkili butonları. kind: warn / mute / ban"""
    p = f"{chat_id}|{user_id}"
    if kind == 'warn':
        rows = [[ibtn("↩️ Uyarıyı geri al", f"m|uw|{p}", BLUE), ibtn("🔇 Sustur 1s", f"m|mu|{p}"),
                 ibtn("🚫 Banla", f"m|bn|{p}", RED)]]
    elif kind == 'mute':
        rows = [[ibtn("🔊 Susturmayı kaldır", f"m|um|{p}", GREEN), ibtn("🚫 Banla", f"m|bn|{p}", RED)]]
    else:
        rows = [[ibtn("✅ Banı kaldır", f"m|ub|{p}", GREEN)]]
    return InlineKeyboardMarkup(rows)

MOD_BUTTON_PERMS = {'uw': 'can_unwarn', 'mu': 'can_mute', 'um': 'can_mute', 'bn': 'can_ban', 'ub': 'can_ban'}

async def _apply_mod_action(cid: str, act: str, target: int, clicker, channel: dict):
    """Moderasyon butonlarının ortak çekirdeği. act: uw/mu/um/bn/ub. Dönüş: (sonuç metni, yeni klavye)."""
    try:
        tuser = (await bot.get_chat_member(cid, target)).user
    except Exception:
        tuser = None
    uname = (tuser.username or tuser.first_name) if tuser else ''
    now = time.time()
    new_markup = None
    if act == 'uw':
        async with _db_lock:
            with get_db() as conn:
                row = conn.execute("SELECT warn_count FROM warnings WHERE chat_id = ? AND user_id = ?",
                                   (cid, target)).fetchone()
                n = max(0, (row['warn_count'] if row else 0) - 1)
                if n:
                    conn.execute("UPDATE warnings SET warn_count = ? WHERE chat_id = ? AND user_id = ?", (n, cid, target))
                else:
                    conn.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (cid, target))
                conn.commit()
        result = f"↩️ Uyarı geri alındı ({n}/{channel['settings'].get('warn_limit', 5)})"
    elif act == 'mu':
        await bot.restrict_chat_member(cid, target, permissions=ChatPermissions.no_permissions(),
                                       until_date=int(now + 3600))
        async with _db_lock:
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO mute_list (chat_id, user_id, username, until_date, muted_at, muted_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (cid, target, uname, now + 3600, now, clicker.id))
                conn.commit()
        result, new_markup = "🔇 1 saat susturuldu", mod_markup(cid, target, 'mute')
    elif act == 'um':
        await bot.restrict_chat_member(cid, target, permissions=await _default_member_permissions(cid))
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM mute_list WHERE chat_id = ? AND user_id = ?", (cid, target))
                conn.commit()
        result = "🔊 Susturma kaldırıldı"
    elif act == 'bn':
        await bot.ban_chat_member(cid, target)
        async with _db_lock:
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO ban_list (chat_id, user_id, username, reason, banned_at, banned_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (cid, target, uname, "moderasyon butonu", now, clicker.id))
                conn.commit()
        channel['stats']['bans'] = channel['stats'].get('bans', 0) + 1
        save_channel_settings(cid, channel)
        result, new_markup = "🚫 Banlandı", mod_markup(cid, target, 'ban')
    else:
        await bot.unban_chat_member(cid, target, only_if_banned=True)
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM ban_list WHERE chat_id = ? AND user_id = ?", (cid, target))
                conn.execute("DELETE FROM temp_bans WHERE chat_id = ? AND user_id = ?", (cid, target))
                conn.commit()
        result = "✅ Ban kaldırıldı"
    who = mention(tuser) if tuser else mention_html(target, str(target))
    await log_mod_action(cid, f"buton:{act}", target, uname, clicker.id, clicker.username or '', result)
    await send_log(cid, f"{result}: {who} | {mention(clicker)}", ParseMode.HTML)
    return result, new_markup

async def mod_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """m|<işlem>|<chat_id>|<user_id> — uyarı/ban/mute mesajlarının altındaki butonlar."""
    query = update.callback_query
    try:
        _, act, cid, uid_s = query.data.split('|')
        target = int(uid_s)
    except ValueError:
        await query.answer("Hata.", show_alert=True)
        return
    clicker = query.from_user
    channel = get_channel_settings(cid)
    if act not in MOD_BUTTON_PERMS or not channel:
        await query.answer("Geçersiz işlem.", show_alert=True)
        return
    if not has_specific_permission(cid, clicker.id, MOD_BUTTON_PERMS[act]):
        await deny(update, MOD_BUTTON_PERMS[act])
        return
    if not await _is_real_chat_admin(cid, clicker.id):
        await query.answer("Bu işlem için grupta yönetici olmalısın.", show_alert=True)
        return
    if not await _is_bot_admin(cid):
        await query.answer("Bot bu grupta yönetici değil veya yetkileri yetersiz.", show_alert=True)
        return
    if act in ('mu', 'bn') and await is_staff_user(cid, target, channel):
        await query.answer("Yetkililere uygulanamaz.", show_alert=True)
        return
    try:
        result, new_markup = await _apply_mod_action(cid, act, target, clicker, channel)
    except Exception as e:
        await query.answer(friendly_error(e)[:190], show_alert=True)
        return
    await query.answer(result)
    try:
        await query.edit_message_text(f"{query.message.text_html}\n\n— {result} · {mention(clicker)}",
                                      parse_mode=ParseMode.HTML, reply_markup=new_markup,
                                      disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"Moderasyon mesajı güncellenemedi: {e}")

# ─────────────────────────── CAPTCHA ───────────────────────────

def generate_captcha():
    a = random.randint(1, 15)
    b = random.randint(1, 15)
    op = random.choice(['+', '-', '*'])
    if op == '+':
        answer = a + b
        question = f"{a} + {b}"
    elif op == '-':
        if a < b:
            a, b = b, a
        answer = a - b
        question = f"{a} - {b}"
    else:
        answer = a * b
        question = f"{a} × {b}"
    return question, str(answer)

async def _default_member_permissions(chat_id: str) -> ChatPermissions:
    """Grubun kendi varsayılan izinleri (captcha/raid sonrası geri vermek için)."""
    try:
        chat = await bot.get_chat(chat_id)
        if chat.permissions:
            return chat.permissions
    except Exception as e:
        logger.debug(f"Grup izinleri alınamadı {chat_id}: {e}")
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
        can_add_web_page_previews=True, can_invite_users=True,
    )

async def send_captcha(chat_id: str, user_id: int, username: str, context: ContextTypes.DEFAULT_TYPE, user=None):
    channel = get_channel_settings(chat_id)
    timeout = int(channel['settings'].get('captcha_timeout', 120)) if channel else 120
    question, answer = generate_captcha()
    who = mention(user) if user else html.escape(f"@{username}")
    text = (
        f"👋 Merhaba {who}!\n\n"
        f"Gruba yazabilmek için aşağıdaki soruyu cevaplamalısın.\n\n"
        f"🧮 <b>{question} = ?</b>\n\n"
        f"⏰ Süren: {human_duration(timeout)}. Yanlış cevap veya süre aşımında gruptan atılırsın."
    )
    correct = int(answer)
    options = {correct}
    while len(options) < 4:
        cand = correct + random.randint(-6, 6)
        if cand >= 0:
            options.add(cand)
    options = list(options)
    random.shuffle(options)

    # Doğru cevap butona YAZILMAZ; sadece veritabanında tutulur.
    keyboard = [[ibtn(str(opt), f"captcha|{chat_id}|{user_id}|{opt}", BLUE) for opt in options]]

    try:
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id,
                                       permissions=ChatPermissions.no_permissions())
        msg = await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                     parse_mode=ParseMode.HTML)
        async with _db_lock:
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO captcha_pending (chat_id, user_id, answer, message_id, joined_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (chat_id, user_id, answer, msg.message_id, time.time()))
                conn.commit()
    except Exception as e:
        logger.error(f"Captcha gönderme hata: {e}")

async def check_captcha_timeouts(context: ContextTypes.DEFAULT_TYPE):
    """Süresi dolan captcha'ları işler. DB tabanlı olduğu için bot yeniden başlasa da çalışır."""
    now = time.time()
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id, user_id, message_id, joined_at FROM captcha_pending").fetchall()
    for r in rows:
        channel = get_channel_settings(r['chat_id'])
        timeout = int(channel['settings'].get('captcha_timeout', 120)) if channel else 120
        if now - r['joined_at'] < timeout:
            continue
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM captcha_pending WHERE chat_id = ? AND user_id = ?", (r['chat_id'], r['user_id']))
                conn.commit()
        try:
            await bot.ban_chat_member(r['chat_id'], r['user_id'])
            await bot.unban_chat_member(r['chat_id'], r['user_id'], only_if_banned=True)
        except Exception as e:
            logger.debug(f"Captcha zaman aşımı kick hatası: {e}")
        if r['message_id']:
            try:
                await bot.delete_message(r['chat_id'], r['message_id'])
            except Exception:
                pass
        await send_log(r['chat_id'], f"⏰ Captcha zaman aşımı → ID:{r['user_id']} atıldı | {r['chat_id']}")

async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    try:
        await context.bot.delete_message(data['chat_id'], data['message_id'])
    except Exception:
        pass

async def captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('|')
    if len(parts) < 4:
        await query.answer("Hata.", show_alert=True)
        return

    chat_id, uid_str, chosen = parts[1], parts[2], parts[3]
    try:
        user_id = int(uid_str)
    except ValueError:
        await query.answer("Hata.", show_alert=True)
        return

    if query.from_user.id != user_id:
        await query.answer("Bu captcha sana ait değil!", show_alert=True)
        return

    with get_db() as conn:
        row = conn.execute(
            "SELECT answer, message_id FROM captcha_pending WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()

    if not row:
        await query.answer("Captcha süresi dolmuş.", show_alert=True)
        return

    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM captcha_pending WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            conn.commit()

    who = mention(query.from_user)
    if chosen == str(row['answer']):
        await query.answer("✅ Doğru!")
        try:
            perms = await _default_member_permissions(chat_id)
            await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=perms)
            await query.edit_message_text(f"✅ {who} doğrulandı!", parse_mode=ParseMode.HTML)
            await send_log(chat_id, f"✅ Captcha geçti: {who} | {chat_id}", ParseMode.HTML)
            if context.job_queue:
                context.job_queue.run_once(_delete_message_job, 15,
                                           data={'chat_id': chat_id, 'message_id': query.message.message_id})
            await send_welcome(chat_id, [query.from_user])  # captcha'dan geçene hoş geldin
        except Exception as e:
            logger.error(f"Captcha doğrulama hata: {e}")
    else:
        await query.answer("❌ Yanlış cevap!", show_alert=True)
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            await query.edit_message_text(f"❌ {who} yanlış cevap verdi ve atıldı.", parse_mode=ParseMode.HTML)
            await send_log(chat_id, f"❌ Captcha başarısız → {who} atıldı | {chat_id}", ParseMode.HTML)
        except Exception as e:
            logger.error(f"Captcha kick hata: {e}")

# ─────────────────────────── KORUMA FİLTRELERİ ───────────────────────────

async def _sender_chat_violation(msg, chat_id: str, what: str):
    """'Kanal olarak' yazılan ihlal mesajı: kullanıcıya ceza verilemez, mesaj silinir."""
    try:
        await msg.delete()
    except Exception:
        pass
    title = html.escape(msg.sender_chat.title or str(msg.sender_chat.id))
    await send_log(chat_id, f"🗑 {what} — kanal kimliğiyle ({title}) gönderilen mesaj silindi | {chat_id}", ParseMode.HTML)

def _is_forwarded(msg) -> bool:
    return (
        getattr(msg, 'forward_origin', None) is not None or
        getattr(msg, 'forward_date', None) is not None or
        getattr(msg, 'forward_from', None) is not None or
        getattr(msg, 'forward_from_chat', None) is not None or
        getattr(msg, 'forward_sender_name', None) is not None
    )

async def newbie_guard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yeni üyeler ilk X dakika link / medya / forward gönderemez."""
    msg = update.effective_message
    if not msg or not msg.from_user or msg.chat.type not in ('group', 'supergroup'):
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    minutes = int(channel['settings'].get('newbie_minutes', 0) or 0)
    if minutes <= 0:
        return
    with get_db() as conn:
        row = conn.execute("SELECT joined_at FROM newcomers WHERE chat_id = ? AND user_id = ?",
                           (chat_id, msg.from_user.id)).fetchone()
    if not row or time.time() - row['joined_at'] > minutes * 60:
        return
    has_media = any(getattr(msg, f, None) for f in MEDIA_FIELDS)
    if not (has_media or _is_forwarded(msg) or _extract_links(msg)):
        return
    if await is_exempt_message(chat_id, msg, channel):
        return
    try:
        await msg.delete()
        left = int((row['joined_at'] + minutes * 60 - time.time()) // 60) + 1
        notice = await bot.send_message(
            chat_id,
            f"🆕 {mention(msg.from_user)}, yeni üyeler ilk {minutes} dakika link/medya/forward gönderemez. "
            f"(~{left} dk kaldı)",
            parse_mode=ParseMode.HTML,
            **thread_kw(msg, chat_id)
        )
        if context.job_queue:
            context.job_queue.run_once(_delete_message_job, 20, data={'chat_id': chat_id, 'message_id': notice.message_id})
    except Exception as e:
        logger.debug(f"Newbie guard hata: {e}")
    raise ApplicationHandlerStop

# ═══════════════════════════ UYGUNSUZ MEDYA KALKANI ═══════════════════════════
# Grubu/kanalı porno atıp şikâyetle kapattırma saldırısına karşı katmanlar:
#   1) Engelli medya listesi (file_unique_id / sticker paketi) — aynı dosyanın her kopyası anında silinir
#   2) Tehlikeli dosya türleri (.apk, .exe …)
#   3) İsteğe bağlı yapay zeka taraması (pip install nudenet) — arka planda, uygunsuz bulunan listeye eklenir
#   4) Saldırı sezilince otomatik medya kilidi; kanalda art arda uygunsuz gönderi → koruma modu (lockdown)
try:
    from nudenet import NudeDetector  # isteğe bağlı; kurulu değilse tarama kapalı kalır
except Exception as _nsfw_import_error:  # kurulu değil ya da platform desteklemiyor
    NudeDetector = None
    logger.info(f"NudeNet yok, yapay zeka medya taraması kapalı: {_nsfw_import_error}")

DANGEROUS_EXTS = {'apk', 'xapk', 'apkm', 'apks', 'exe', 'scr', 'bat', 'cmd', 'com', 'msi', 'jar', 'vbs', 'vbe',
                  'js', 'jse', 'ps1', 'dll', 'lnk', 'hta', 'pif', 'wsf', 'reg', 'cpl'}
DANGEROUS_MIMES = {'application/vnd.android.package-archive', 'application/x-msdownload', 'application/x-dosexec',
                   'application/x-msdos-program', 'application/java-archive'}
# NudeNet sınıfı → en düşük güven puanı
NSFW_CLASSES = {'FEMALE_GENITALIA_EXPOSED': 0.40, 'MALE_GENITALIA_EXPOSED': 0.40, 'ANUS_EXPOSED': 0.40,
                'FEMALE_BREAST_EXPOSED': 0.55, 'BUTTOCKS_EXPOSED': 0.65}
MEDIA_PERM_FIELDS = ('can_send_photos', 'can_send_videos', 'can_send_video_notes', 'can_send_audios',
                     'can_send_documents', 'can_send_voice_notes', 'can_send_other_messages', 'can_add_web_page_previews')

_nsfw_detector = None
_nsfw_cache: dict = {}          # file_unique_id → uygunsuz mu (tekrar taramayı önler)
_blocked_media_cache: set | None = None
_media_attack: dict = {}        # chat_id → [(zaman, user_id)]
_channel_media_hits: dict = {}  # chat_id → [zaman]

def nsfw_available() -> bool:
    return NudeDetector is not None

def _nsfw_detect_bytes(data: bytes) -> str:
    """Görüntüde açık çıplaklık varsa açıklama döner, yoksa boş metin. (İş parçacığında çalışır.)"""
    global _nsfw_detector
    if _nsfw_detector is None:
        _nsfw_detector = NudeDetector()
    hits = [f"{d['class']} {d['score']:.2f}" for d in _nsfw_detector.detect(data)
            if d.get('score', 0) >= NSFW_CLASSES.get(d.get('class'), 2)]
    return ", ".join(hits)

def _media_info(msg):
    """(file_unique_id, taranacak file_id veya None, tür, sticker paketi) — medya yoksa None."""
    if msg.photo:
        scan = next((p for p in reversed(msg.photo) if p.width <= 1280), msg.photo[0])
        return msg.photo[-1].file_unique_id, scan.file_id, 'photo', None
    if msg.sticker:
        s = msg.sticker
        if s.is_animated or s.is_video:
            scan = s.thumbnail.file_id if s.thumbnail else None
        else:
            scan = s.file_id
        return s.file_unique_id, scan, 'sticker', s.set_name
    for field in ('animation', 'video', 'video_note'):
        obj = getattr(msg, field, None)
        if obj:
            return obj.file_unique_id, (obj.thumbnail.file_id if obj.thumbnail else None), field, None
    if msg.document:
        d = msg.document
        if (d.mime_type or '').startswith('image/') and (d.file_size or 0) < 5_000_000:
            scan = d.file_id
        else:
            scan = d.thumbnail.file_id if d.thumbnail else None
        return d.file_unique_id, scan, 'document', None
    return None

def _dangerous_file(document) -> str | None:
    name = (document.file_name or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    if ext in DANGEROUS_EXTS:
        return f".{ext}"
    if (document.mime_type or '').lower() in DANGEROUS_MIMES:
        return document.mime_type
    return None

def _load_blocked_media():
    global _blocked_media_cache
    with get_db() as conn:
        _blocked_media_cache = {(r['chat_id'], r['uid']) for r in conn.execute("SELECT chat_id, uid FROM blocked_media")}

def media_is_blocked(chat_id: str, uid: str, set_name: str | None) -> bool:
    if _blocked_media_cache is None:
        _load_blocked_media()
    keys = {(chat_id, uid), ('*', uid)}
    if set_name:
        keys |= {(chat_id, f"set:{set_name}"), ('*', f"set:{set_name}")}
    return bool(keys & _blocked_media_cache)

def block_media(chat_id: str, uid: str, kind: str, note: str, by: int) -> bool:
    """Medyayı engelli listeye ekler; zaten varsa False."""
    global _blocked_media_cache
    with get_db() as conn:
        n = conn.execute("INSERT OR IGNORE INTO blocked_media (chat_id, uid, kind, note, added_by, added_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (chat_id, uid, kind, note[:100], by, time.time())).rowcount
        conn.commit()
    _blocked_media_cache = None
    return n > 0

def unblock_media(chat_id: str, rowid: int):
    global _blocked_media_cache
    with get_db() as conn:
        conn.execute("DELETE FROM blocked_media WHERE rowid = ? AND chat_id = ?", (rowid, chat_id))
        conn.commit()
    _blocked_media_cache = None

async def media_shield_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gruplarda ve kanallarda (düzenlenen dahil) medyayı kontrol eder. Hızlı kontroller hemen, yapay zeka arka planda."""
    msg = update.effective_message
    if not msg or not msg.chat or msg.chat.type not in ('group', 'supergroup', 'channel'):
        return
    info = _media_info(msg)
    if not info:
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('media_shield'):
        return
    s = channel['settings']
    if msg.chat.type != 'channel' and await is_exempt_message(chat_id, msg, channel):
        return
    uid, scan_id, kind, set_name = info
    reason = None
    if media_is_blocked(chat_id, uid, set_name):
        reason = "engelli sticker paketi" if set_name and not media_is_blocked(chat_id, uid, None) else "engelli medya"
    elif kind == 'document' and s.get('file_block', True) and _dangerous_file(msg.document):
        reason = f"tehlikeli dosya ({_dangerous_file(msg.document)})"
    elif _nsfw_cache.get(uid):
        reason = "uygunsuz içerik"
    if reason:
        await _bad_media_hit(msg, channel, chat_id, reason, context)
        raise ApplicationHandlerStop
    if s.get('nsfw_scan', True) and nsfw_available() and scan_id and uid not in _nsfw_cache:
        context.application.create_task(_nsfw_scan_task(msg, chat_id, uid, scan_id, context), update=update)

async def _nsfw_scan_task(msg, chat_id: str, uid: str, scan_id: str, context):
    """Arka plan taraması: dosyayı indirir, NudeNet ile tarar; uygunsuzsa siler, cezalandırır ve listeye ekler."""
    try:
        tg_file = await bot.get_file(scan_id)
        if (tg_file.file_size or 0) > 10_000_000:
            return
        data = bytes(await tg_file.download_as_bytearray())
        detail = await asyncio.to_thread(_nsfw_detect_bytes, data)
    except Exception as e:
        logger.debug(f"NSFW taraması yapılamadı ({chat_id}): {e}")
        return
    if len(_nsfw_cache) > 5000:
        _nsfw_cache.clear()
    _nsfw_cache[uid] = bool(detail)
    if not detail:
        return
    block_media(chat_id, uid, 'file', f"yapay zeka: {detail}", cur_bot_id())
    channel = get_channel_settings(chat_id)
    if channel:
        await _bad_media_hit(msg, channel, chat_id, f"uygunsuz içerik ({detail})", context)

async def _bad_media_hit(msg, channel: dict, chat_id: str, reason: str, context):
    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"Uygunsuz medya silinemedi: {e}")
    if msg.chat.type == 'channel':
        await _channel_bad_media(chat_id, reason, msg)
        return
    if msg.sender_chat:
        await _sender_chat_violation(msg, chat_id, f"Uygunsuz medya ({reason})")
    elif msg.from_user:
        await enforce_action(chat_id, msg, channel, 'badmedia', reason, context.bot_data)
    await _count_media_attack(chat_id, msg.from_user.id if msg.from_user else 0, channel)

async def _count_media_attack(chat_id: str, user_id: int, channel: dict):
    """5 dk içinde en az 2 farklı kişiden 3 uygunsuz medya → otomatik medya kilidi."""
    s = channel['settings']
    now = time.time()
    hits = [(t, u) for t, u in _media_attack.get(chat_id, []) if now - t < 300] + [(now, user_id)]
    _media_attack[chat_id] = hits
    if s.get('media_autolock', True) and len(hits) >= 3 and len({u for _, u in hits}) >= 2:
        _media_attack[chat_id] = []
        if await media_lock(chat_id, int(s.get('media_lock_minutes', 30) or 30),
                            "otomatik: art arda uygunsuz medya (saldırı şüphesi)"):
            await notify_managers(chat_id, f"🚨 <b>Medya saldırısı</b> sezildi, grupta medya gönderimi kilitlendi.\n"
                                           f"Grup: <b>{html.escape(await _chat_title(chat_id))}</b>",
                                  parse_mode=ParseMode.HTML)

async def _channel_bad_media(chat_id: str, reason: str, msg):
    """Kanalda uygunsuz gönderi: yöneticilere haber ver; 10 dk içinde ikincisi → koruma modu (adminlerin yetkisi alınır)."""
    now = time.time()
    hits = [t for t in _channel_media_hits.get(chat_id, []) if now - t < 600] + [now]
    _channel_media_hits[chat_id] = hits
    who = html.escape(msg.author_signature) if msg.author_signature else "imzasız gönderi"
    await notify_managers(chat_id, f"🔞 <b>Kanalda uygunsuz medya silindi</b>\nSebep: {html.escape(reason)}\n"
                                   f"Gönderen: {who}\nKanal: <code>{chat_id}</code>", parse_mode=ParseMode.HTML)
    await log_channel_action(chat_id, 'bad_media', 0, msg.author_signature or '', reason)
    if len(hits) >= 2 and not get_channel_cfg(chat_id).get('lockdown_mode'):
        _channel_media_hits[chat_id] = []
        await enter_lockdown(chat_id, "Kanalda art arda uygunsuz medya (kanalı kapattırma saldırısı şüphesi)")

async def media_lock(chat_id: str, minutes: int, reason: str) -> bool:
    """Üyelerin medya göndermesini geçici kapatır; eski izinleri saklar. Raid kilidi varken gerek yok."""
    ch = get_channel_settings(chat_id)
    if not ch:
        return False
    s = ch['settings']
    if s.get('media_lock'):
        return True
    if s.get('raid_lock'):
        return False
    try:
        chat = await bot.get_chat(chat_id)
        saved = (chat.permissions or ChatPermissions.all_permissions()).to_dict()
        locked = ChatPermissions.de_json({**saved, **{f: False for f in MEDIA_PERM_FIELDS}}, bot)
        await bot.set_chat_permissions(chat_id, locked, use_independent_chat_permissions=True)
    except Exception as e:
        logger.error(f"Medya kilidi uygulanamadı ({chat_id}): {e}")
        return False
    ch = get_channel_settings(chat_id)
    ch['settings']['media_lock'] = {'until': time.time() + minutes * 60, 'saved_perms': saved}
    save_channel_settings(chat_id, ch)
    text = (f"🔒 <b>Medya kilidi</b>: {human_duration(minutes * 60)} boyunca fotoğraf, video, sticker, GIF ve dosya "
            f"gönderimi kapalı.\nSebep: {html.escape(reason)}")
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug(f"Medya kilidi duyurusu gönderilemedi: {e}")
    await send_log(chat_id, f"{text} | {chat_id}", ParseMode.HTML)
    return True

async def media_unlock(chat_id: str) -> bool:
    ch = get_channel_settings(chat_id)
    lock = ch['settings'].pop('media_lock', None) if ch else None
    if not lock:
        return False
    saved = lock.get('saved_perms')
    if ch['settings'].get('raid_lock'):
        # Raid kilidi sonradan geldi: onun açılışında medya kilidinden önceki gerçek izinler dönsün
        ch['settings']['raid_lock']['saved_perms'] = saved
        save_channel_settings(chat_id, ch)
        return True
    try:
        perms = ChatPermissions.de_json(saved, bot) if saved else ChatPermissions.all_permissions()
        await bot.set_chat_permissions(chat_id, perms, use_independent_chat_permissions=True)
    except Exception as e:
        logger.error(f"Medya kilidi açılamadı ({chat_id}): {e}")
        ch['settings']['media_lock'] = lock
        save_channel_settings(chat_id, ch)
        return False
    save_channel_settings(chat_id, ch)
    try:
        await bot.send_message(chat_id, "🔓 Medya kilidi açıldı, eski izinler geri yüklendi.")
    except Exception as e:
        logger.debug(f"Medya kilidi açılış duyurusu gönderilemedi: {e}")
    await send_log(chat_id, f"🔓 Medya kilidi açıldı | {chat_id}")
    return True

async def check_media_locks(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM channels WHERE settings LIKE '%media_lock%'").fetchall()
    for r in rows:
        ch = get_channel_settings(r['chat_id'])
        lock = ch['settings'].get('media_lock') if ch else None
        if lock and now >= lock.get('until', 0):
            await media_unlock(r['chat_id'])

async def cmd_medya_engel(update: Update, context):
    """/medyaengel (medyaya yanıt) — o medyayı bu grupta engeller; /paketengel sticker paketini; /gmedyaengel tüm gruplarda."""
    msg = update.effective_message
    cmd = (msg.text or '').split()[0].split('@')[0].lower().lstrip('/')
    chat_id = str(msg.chat_id)
    is_global = cmd == 'gmedyaengel'
    if is_global and update.effective_user.id != FOUNDER_ID:
        return
    if msg.chat.type not in ('group', 'supergroup') or not get_channel_settings(chat_id):
        await msg.reply_text("Bu komutu grupta, engellenecek medyaya yanıt vererek kullan.")
        return
    if not is_global and not await require(update, chat_id, 'can_manage_settings'):
        return
    target = msg.reply_to_message
    info = _media_info(target) if target else None
    if not info:
        await msg.reply_text("Engellemek istediğin fotoğraf/video/GIF/sticker/dosyaya yanıt vererek yaz.")
        return
    uid, _, kind, set_name = info
    if cmd == 'paketengel':
        if not set_name:
            await msg.reply_text("Paket engeli için bir sticker'a yanıt ver.")
            return
        added = block_media(chat_id, f"set:{set_name}", 'set', f"paket {set_name}", update.effective_user.id)
        label = f"sticker paketi <code>{html.escape(set_name)}</code>"
    else:
        added = block_media('*' if is_global else chat_id, uid, 'file', kind, update.effective_user.id)
        label = f"bu {kind}" + (" (tüm gruplarda)" if is_global else "")
    try:
        await target.delete()
    except Exception as e:
        logger.debug(f"Engellenen medya silinemedi: {e}")
    await msg.reply_text(f"🚫 {label} engellendi. Aynısı gönderilirse silinir." if added else "Bu zaten engelli.",
                         parse_mode=ParseMode.HTML)
    if added:
        ch = get_channel_settings(chat_id)
        if not ch['settings'].get('media_shield'):
            ch['settings']['media_shield'] = True
            save_channel_settings(chat_id, ch)
        await send_log(chat_id, f"🚫 Medya engellendi ({html.escape(label)}) | {mention(update.effective_user)}",
                       ParseMode.HTML)

async def cmd_medya_kilit(update: Update, context):
    """/medyakilit [dk] ve /medyaac"""
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile seç!")
        return
    cmd = (msg.text or '').split()[0].split('@')[0].lower().lstrip('/')
    if not await require(update, chat_id, 'can_manage_settings' if cmd == 'medyaac' else 'can_lock'):
        return
    if cmd == 'medyaac':
        await msg.reply_text("🔓 Medya kilidi açıldı." if await media_unlock(chat_id) else "Medya kilidi zaten açık.")
        return
    minutes = int(context.args[0]) if context.args and context.args[0].isdigit() else \
        int(get_channel_settings(chat_id)['settings'].get('media_lock_minutes', 30) or 30)
    minutes = max(1, min(minutes, 1440))
    ok = await media_lock(chat_id, minutes, f"manuel: {update.effective_user.first_name}")
    await msg.reply_text("🔒 Medya kilitlendi." if ok else "Kilitlenemedi (raid kilidi aktif olabilir veya botun yetkisi yok).")

# ═══════════════════════════ GEÇ DÜZENLEME KORUMASI ═══════════════════════════
# Telegram düzenlenen mesajın sadece yeni hâlini gönderir; eski hâl için mesajlar koruma açıkken 2 gün saklanır.

def _fmt_age(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} gün {h} saat"
    if h:
        return f"{h} saat {m} dk"
    return f"{max(m, 1)} dk"

async def message_cache_handler(update: Update, context):
    """Geç düzenleme koruması açık gruplarda mesajın ilk hâlini saklar."""
    msg = update.message
    if not msg or not msg.from_user:
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('edit_guard'):
        return
    text = message_text(msg) or (f"[{_get_msg_type(msg)}]" if _media_info(msg) else '')
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO msg_cache (chat_id, message_id, user_id, text, sent_at) VALUES (?, ?, ?, ?, ?)",
                     (chat_id, msg.message_id, msg.from_user.id, text[:4000], msg.date.timestamp()))
        conn.commit()

async def edit_guard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gönderildikten X dk sonra düzenlenen mesajı siler; grubun kurucusuna ve botu ekleyene eski/yeni hâli gönderir."""
    msg = update.edited_message
    if not msg or not msg.from_user or not msg.edit_date or msg.location:  # canlı konum güncellemeleri de "düzenleme" gelir
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('edit_guard'):
        return
    s = channel['settings']
    limit_min = int(s.get('edit_guard_minutes', 5) or 5)
    age = (msg.edit_date - msg.date).total_seconds()
    new_text = message_text(msg) or (f"[{_get_msg_type(msg)}]" if _media_info(msg) else '')
    with get_db() as conn:
        row = conn.execute("SELECT text FROM msg_cache WHERE chat_id = ? AND message_id = ?",
                           (chat_id, msg.message_id)).fetchone()
    if age <= limit_min * 60 or await is_exempt_message(chat_id, msg, channel):
        with get_db() as conn:  # izin verilen düzenleme: sonraki karşılaştırma için son hâli sakla
            conn.execute("UPDATE msg_cache SET text = ? WHERE chat_id = ? AND message_id = ?",
                         (new_text[:4000], chat_id, msg.message_id))
            conn.commit()
        return
    old_text = row['text'] if row else None
    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"Geç düzenlenen mesaj silinemedi: {e}")
    with get_db() as conn:
        conn.execute("DELETE FROM msg_cache WHERE chat_id = ? AND message_id = ?", (chat_id, msg.message_id))
        conn.commit()
    try:
        notice = await bot.send_message(
            chat_id, f"✏️ {mention(msg.from_user)}, {limit_min} dakikadan eski mesajlar düzenlenemez; "
                     f"düzenlediğin mesaj silindi.", parse_mode=ParseMode.HTML, **thread_kw(msg, chat_id))
        if context.job_queue:
            context.job_queue.run_once(_delete_message_job, 20, data={'chat_id': chat_id, 'message_id': notice.message_id})
    except Exception as e:
        logger.debug(f"Düzenleme uyarısı gönderilemedi: {e}")
    report = await _edit_report_text(chat_id, msg, old_text, new_text, age)
    if s.get('edit_notify', True):
        for uid in await _edit_notify_targets(chat_id, channel):
            try:
                await bot.send_message(uid, report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                logger.debug(f"Düzenleme bildirimi gönderilemedi ({uid}): {e}")
    await send_log(chat_id, report, ParseMode.HTML)
    raise ApplicationHandlerStop

async def _edit_notify_targets(chat_id: str, channel: dict) -> list:
    """Grubun Telegram'daki kurucusu + botu gruba ekleyen kişi."""
    targets = []
    try:
        targets += [a.user.id for a in await bot.get_chat_administrators(chat_id) if a.status == 'creator']
    except Exception as e:
        logger.debug(f"Grup kurucusu alınamadı ({chat_id}): {e}")
    if channel.get('owner'):
        targets.append(channel['owner'])
    return list(dict.fromkeys(t for t in targets if t))

async def _edit_report_text(chat_id: str, msg, old_text, new_text: str, age: float) -> str:
    sent = msg.date.astimezone(TZ_TR).strftime('%d.%m.%Y %H:%M')
    old_block = html.escape(old_text) if old_text else "<i>kayıt yok (mesaj, koruma açılmadan önce gönderilmiş)</i>"
    link = f"\n<a href=\"{msg.link}\">Mesajın yeri</a>" if msg.link else ""
    return (f"✏️ <b>Geç düzenlenen mesaj silindi</b>\n"
            f"Grup: <b>{html.escape(await _chat_title(chat_id))}</b>\n"
            f"Kullanıcı: {mention(msg.from_user)} (<code>{msg.from_user.id}</code>)\n"
            f"Gönderilme: {sent} · {_fmt_age(age)} sonra düzenlendi\n\n"
            f"<b>Eski hâli:</b>\n<blockquote expandable>{old_block}</blockquote>\n"
            f"<b>Yeni hâli:</b>\n<blockquote expandable>{html.escape(new_text) or '—'}</blockquote>{link}")

async def anti_forward_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not _is_forwarded(msg):
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('anti_forward', False):
        return
    if await is_exempt_message(chat_id, msg, channel):
        return
    if msg.sender_chat:
        await _sender_chat_violation(msg, chat_id, "Forward")
        return

    user_id = msg.from_user.id
    now = time.time()

    async with _db_lock:
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM forward_history WHERE chat_id = ? AND user_id = ? AND timestamp > ?",
                (chat_id, user_id, now - 3600)
            ).fetchone()[0]
            conn.execute("INSERT INTO forward_history (chat_id, user_id, timestamp) VALUES (?, ?, ?)",
                         (chat_id, user_id, now))
            conn.commit()

    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"Forward silinemedi: {e}")
    await enforce_action(chat_id, msg, channel, 'forward', f"forward ({count + 1}. kez)", context.bot_data,
                         mute_minutes=[10, 30, 300][min(count, 2)])

MEDIA_FIELDS = ['photo', 'video', 'animation', 'document', 'voice', 'sticker', 'video_note', 'audio']

async def anti_media_flood_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not any(getattr(msg, f, None) for f in MEDIA_FIELDS):
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('anti_media_flood', False):
        return
    if await is_exempt_message(chat_id, msg, channel) or msg.sender_chat:
        return

    user_id = msg.from_user.id
    now = time.time()
    timeframe = channel['settings'].get('media_flood_timeframe', 10)
    limit = channel['settings'].get('media_flood_limit', 5)

    async with _db_lock:
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM media_flood_history WHERE chat_id = ? AND user_id = ? AND timestamp > ?",
                (chat_id, user_id, now - timeframe)
            ).fetchone()[0]
            conn.execute("INSERT INTO media_flood_history (chat_id, user_id, timestamp) VALUES (?, ?, ?)",
                         (chat_id, user_id, now))
            conn.commit()

    if count + 1 >= limit:
        level = min(max(0, (count + 1 - limit) // 3), 2)
        try:
            await msg.delete()
        except Exception as e:
            logger.debug(f"Medya silinemedi: {e}")
        await enforce_action(chat_id, msg, channel, 'media', f"medya flood ({count + 1}/{timeframe} sn)",
                             context.bot_data, mute_minutes=[10, 30, 300][level])

async def anti_spam_flood_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('anti_spam_flood', False):
        return
    if await is_exempt_message(chat_id, msg, channel) or msg.sender_chat:
        return

    user_id = msg.from_user.id
    now = time.time()
    timeframe = channel['settings'].get('flood_timeframe', 5)
    limit = channel['settings'].get('flood_limit', 7)

    async with _db_lock:
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM flood_history WHERE chat_id = ? AND user_id = ? AND timestamp > ?",
                (chat_id, user_id, now - timeframe)
            ).fetchone()[0]
            conn.execute("INSERT INTO flood_history (chat_id, user_id, timestamp) VALUES (?, ?, ?)",
                         (chat_id, user_id, now))
            conn.commit()

    if count + 1 >= limit:
        level = min(max(0, (count + 1 - limit) // 5), 2)
        try:
            await msg.delete()
        except Exception as e:
            logger.debug(f"Flood mesajı silinemedi: {e}")
        await enforce_action(chat_id, msg, channel, 'flood', f"flood ({count + 1} mesaj/{timeframe} sn)",
                             context.bot_data, mute_minutes=[10, 30, 300][level])

LINK_PATTERN = re.compile(
    r'((?:https?://|www\.)\S+'
    r'|(?:t|telegram)\.(?:me|dog)/\S+'
    r'|\b[a-z0-9][a-z0-9-]{0,62}\.(?:com\.tr|com|net|org|info|biz|xyz|io|me|ru|tk|ml|ga|cf|gq|top|site|online|shop'
    r'|club|link|click|live|app|dev|co|cc|pw|ly|gg|tr|store|vip|bet|win|fun|space|website)\b(?:/\S*)?)',
    re.IGNORECASE
)

def _extract_links(msg) -> list:
    links = [m.group(0) for m in LINK_PATTERN.finditer(message_text(msg))]
    for e in message_entities(msg):
        if e.type == MessageEntity.TEXT_LINK and e.url:
            links.append(e.url)  # yazının altına gizlenmiş link
    if msg.reply_markup and getattr(msg.reply_markup, 'inline_keyboard', None):
        for row in msg.reply_markup.inline_keyboard:
            for b in row:
                if getattr(b, 'url', None):
                    links.append(b.url)
    return links

def _link_domain(link: str) -> str:
    d = re.sub(r'^[a-z]+://', '', link.strip().lower()).split('/')[0].split('?')[0].split(':')[0]
    return d[4:] if d.startswith('www.') else d

def _link_allowed(link: str, whitelist: list) -> bool:
    d = _link_domain(link)
    low = link.lower()
    for w in whitelist:
        w = str(w).lower().strip()
        if not w:
            continue
        if '/' in w:
            if w in low:
                return True
        elif d == w or d.endswith('.' + w):
            return True
    return False

async def anti_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message  # düzenlenen mesajlar da kontrol edilir
    if not msg or msg.chat.type == 'private':
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('anti_link', False):
        return
    links = _extract_links(msg)
    if not links:
        return
    wl = channel['settings'].get('link_whitelist', [])
    if all(_link_allowed(l, wl) for l in links):
        return
    if await is_exempt_message(chat_id, msg, channel):
        return
    if msg.sender_chat:
        await _sender_chat_violation(msg, chat_id, "Link")
        raise ApplicationHandlerStop

    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"Link mesajı silinemedi: {e}")
    await send_log(chat_id, f"🔗 Link silindi → {mention(msg.from_user)} | {chat_id}", ParseMode.HTML)
    await enforce_action(chat_id, msg, channel, 'link', "link gönderme", context.bot_data)
    raise ApplicationHandlerStop  # silinen mesaj için diğer filtreler çalışmasın

# ── Kelime filtresi: Türkçe karakter, büyük/küçük harf, leetspeak ve harf tekrarına dayanıklı ──
_LEET = str.maketrans({'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', '$': 's', '€': 'e', '!': 'i'})
_TR_FOLD = str.maketrans({'ı': 'i', 'ş': 's', 'ğ': 'g', 'ü': 'u', 'ö': 'o', 'ç': 'c', 'â': 'a', 'î': 'i', 'û': 'u'})

def normalize_text(t: str) -> str:
    t = unicodedata.normalize('NFKC', t or '')
    t = t.replace('İ', 'i').replace('I', 'ı').lower()
    t = t.translate(_LEET).translate(_TR_FOLD)
    t = re.sub(r'(.)\1{2,}', r'\1', t)  # "küüüüfür" → "küfür"
    return t

_wordlist_cache: dict = {}

def _compile_wordlist(words: list):
    key = tuple(words)
    cached = _wordlist_cache.get(key)
    if cached:
        return cached
    word_patterns, regex_patterns, compacts = [], [], []
    for w in words:
        w = str(w).strip()
        if not w:
            continue
        if w.lower().startswith('re:'):
            try:
                regex_patterns.append((w, re.compile(w[3:][:200], re.IGNORECASE)))
            except re.error:
                logger.warning(f"Geçersiz regex yasaklı kelime: {w}")
            continue
        nw = normalize_text(w)
        # Kelime başında eşleşir: "küfür" → "küfürler" yakalanır, masum kelimelerin ortası yakalanmaz
        word_patterns.append((w, re.compile(r'(?<![a-z0-9])' + re.escape(nw))))
        if len(nw) >= 5 and ' ' not in nw:
            compacts.append((w, nw))
    if len(_wordlist_cache) > 500:
        _wordlist_cache.clear()
    _wordlist_cache[key] = (word_patterns, regex_patterns, compacts)
    return _wordlist_cache[key]

def find_banned_word(text: str, words: list) -> str | None:
    if not text or not words:
        return None
    word_patterns, regex_patterns, compacts = _compile_wordlist(words)
    norm = normalize_text(text)
    for w, p in word_patterns:
        if p.search(norm):
            return w
    for w, p in regex_patterns:
        if p.search(text) or p.search(norm):
            return w
    if compacts:
        compact = re.sub(r'[^a-z0-9]', '', norm)  # "k.ü.f.ü.r" → "kufur"
        for w, nw in compacts:
            if nw in compact:
                return w
    return None

async def check_message(update: Update, context):
    message = update.effective_message  # düzenlenen mesajlar da kontrol edilir
    if not message or message.chat.type == 'private':
        return
    text_raw = message_text(message)
    if not text_raw:
        return
    chat_id = str(message.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return

    settings = channel['settings']
    if not settings.get('spam_protection') and not settings.get('word_ban_enabled'):
        return
    if await is_exempt_message(chat_id, message, channel):
        return

    user_id = message.from_user.id

    if settings.get('word_ban_enabled'):
        hit = find_banned_word(text_raw, settings.get('banned_words', []))
        if hit:
            try:
                await message.delete()
            except Exception as e:
                logger.debug(f"Yasaklı kelime mesajı silinemedi: {e}")
            if message.sender_chat:
                await _sender_chat_violation(message, chat_id, "Yasaklı kelime")
                return
            channel['stats']['spams'] = channel['stats'].get('spams', 0) + 1
            save_channel_settings(chat_id, channel)
            await enforce_action(chat_id, message, channel, 'word', "yasaklı kelime", context.bot_data)
            return

    if settings.get('spam_protection') and update.message and not message.sender_chat:
        text = normalize_text(text_raw)
        key = f"spam_{chat_id}_{user_id}"
        now = time.time()
        history = [(t, txt) for t, txt in context.bot_data.get(key, []) if now - t < 60]
        history.append((now, text))
        context.bot_data[key] = history
        if len(history) >= 10:
            last_msgs = [txt for _, txt in history[-10:]]
            if len(set(last_msgs)) <= 3:
                context.bot_data.pop(key, None)
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(f"Spam mesajı silinemedi: {e}")
                channel['stats']['spams'] = channel['stats'].get('spams', 0) + 1
                save_channel_settings(chat_id, channel)
                await enforce_action(chat_id, message, channel, 'spam', "tekrar spam", context.bot_data)

async def spam_memory_cleanup(context: ContextTypes.DEFAULT_TYPE):
    """bot_data içindeki eski spam/flood kayıtlarını temizler (bellek sızıntısını önler)."""
    now = time.time()
    for k in list(context.bot_data.keys()):
        v = context.bot_data.get(k)
        if isinstance(k, str) and k.startswith('spam_') and isinstance(v, list):
            if not v or now - v[-1][0] > 120:
                context.bot_data.pop(k, None)
        elif isinstance(k, str) and k.startswith('flood_notice_') and isinstance(v, (int, float)) and now - v > 300:
            context.bot_data.pop(k, None)

async def cmd_antiforward(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        state = get_channel_settings(chat_id)['settings'].get('anti_forward', False)
        await update.message.reply_text(f"Anti-forward şu an: {'AÇIK' if state else 'KAPALI'}\nKullanım: /antiforward on|off")
        return
    new_state = args[0].lower() == 'on'
    channel = get_channel_settings(chat_id)
    channel['settings']['anti_forward'] = new_state
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Anti-forward {'açıldı' if new_state else 'kapatıldı'}.")
    await send_log(chat_id, f"Anti-forward {'açıldı' if new_state else 'kapatıldı'} | {mention(update.effective_user)}", ParseMode.HTML)

async def cmd_antimedia(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        state = get_channel_settings(chat_id)['settings'].get('anti_media_flood', False)
        await update.message.reply_text(f"Anti-media şu an: {'AÇIK' if state else 'KAPALI'}\nKullanım: /antimedia on|off")
        return
    new_state = args[0].lower() == 'on'
    channel = get_channel_settings(chat_id)
    channel['settings']['anti_media_flood'] = new_state
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Anti-media flood {'açıldı' if new_state else 'kapatıldı'}.")
    await send_log(chat_id, f"Anti-media flood {'açıldı' if new_state else 'kapatıldı'} | {mention(update.effective_user)}", ParseMode.HTML)

async def cmd_antispam(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        channel = get_channel_settings(chat_id)
        state = channel['settings'].get('anti_spam_flood', False)
        limit = channel['settings'].get('flood_limit', 7)
        timeframe = channel['settings'].get('flood_timeframe', 5)
        await update.message.reply_text(
            f"Anti-spam flood şu an: {'AÇIK' if state else 'KAPALI'}\n"
            f"Limit: {limit} mesaj / {timeframe} saniye\n"
            f"Kullanım: /antispam on|off\n"
            f"Limit değiştirmek: /antispam on 10 5 (10 mesaj/5 saniye)"
        )
        return
    new_state = args[0].lower() == 'on'
    channel = get_channel_settings(chat_id)
    channel['settings']['anti_spam_flood'] = new_state
    if len(args) >= 3:
        try:
            channel['settings']['flood_limit'] = int(args[1])
            channel['settings']['flood_timeframe'] = int(args[2])
        except Exception as e:
            logger.debug(f"cmd_antispam: {e}")
    save_channel_settings(chat_id, channel)
    lim = channel['settings'].get('flood_limit', 7)
    tf = channel['settings'].get('flood_timeframe', 5)
    await update.message.reply_text(f"Anti-spam flood {'açıldı' if new_state else 'kapatıldı'}. (Limit: {lim} mesaj/{tf}sn)")
    await send_log(chat_id, f"Anti-spam {'açıldı' if new_state else 'kapatıldı'} | {mention(update.effective_user)}", ParseMode.HTML)

async def cmd_antilink(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        state = get_channel_settings(chat_id)['settings'].get('anti_link', False)
        await update.message.reply_text(f"Anti-link şu an: {'AÇIK' if state else 'KAPALI'}\nKullanım: /antilink on|off")
        return
    new_state = args[0].lower() == 'on'
    channel = get_channel_settings(chat_id)
    channel['settings']['anti_link'] = new_state
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Link engelleme {'açıldı' if new_state else 'kapatıldı'}. (Adminler muaf)")
    await send_log(chat_id, f"Anti-link {'açıldı' if new_state else 'kapatıldı'} | {mention(update.effective_user)}", ParseMode.HTML)

async def cmd_captcha(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        state = get_channel_settings(chat_id)['settings'].get('captcha_enabled', False)
        await update.message.reply_text(f"Captcha şu an: {'AÇIK' if state else 'KAPALI'}\nKullanım: /captcha on|off")
        return
    new_state = args[0].lower() == 'on'
    channel = get_channel_settings(chat_id)
    channel['settings']['captcha_enabled'] = new_state
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Captcha {'açıldı' if new_state else 'kapatıldı'}.")
    await send_log(chat_id, f"Captcha {'açıldı' if new_state else 'kapatıldı'} | {mention(update.effective_user)}", ParseMode.HTML)

async def add_admin(update: Update, context):
    """/addadmin — /admin ile aynı."""
    await _cmd_set_role(update, context, 'admin')

async def remove_admin(update: Update, context):
    """/remove @kişi — rütbeyi ve Telegram admin haklarını alır."""
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_roles'):
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı.")
        return
    err = await remove_rank(chat_id, update.effective_user.id, user_id)
    if err:
        await update.message.reply_text(err)
        return
    who = mention(member.user)
    await update.message.reply_text(f"✅ {who} artık yetkili değil.", parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"🗑 {who} rütbesi alındı | {mention(update.effective_user)}", ParseMode.HTML)

async def klasorcu(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, LVL_KURUCU):
        await deny(update, level=LVL_KURUCU)
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            )
        )
        who = mention(member.user)
        await update.message.reply_text(f"📂 {who} klasörcü yapıldı!", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"📂 {who} klasörcü atandı | {chat_id}", ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def ban(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_ban'):
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    if (why := hierarchy_block(chat_id, update.effective_user.id, user_id)):
        await update.message.reply_text(why)
        return

    if update.message.reply_to_message:
        reason = ' '.join(context.args) if context.args else "Sebep belirtilmedi"
    else:
        reason = ' '.join(context.args[1:]) if len(context.args) > 1 else "Sebep belirtilmedi"

    duration = None
    duration_str_label = None
    raw_args = list(context.args) if context.args else []
    dur_candidates = raw_args if update.message.reply_to_message else raw_args[1:]
    if dur_candidates and len(dur_candidates[0]) > 1 and dur_candidates[0][:-1].isdigit() and dur_candidates[0][-1] in ['m', 'h', 'd']:
        duration_args = dur_candidates[0]
        val = int(duration_args[:-1])
        unit = duration_args[-1]
        if unit == 'm': duration = val * 60; duration_str_label = f"{val} dakika"
        elif unit == 'h': duration = val * 3600; duration_str_label = f"{val} saat"
        elif unit == 'd': duration = val * 86400; duration_str_label = f"{val} gun"
    else:
        duration_args = None
    if duration and reason.startswith(duration_args):
        reason = reason[len(duration_args):].strip() or "Sebep belirtilmedi"

    try:
        channel = get_channel_settings(chat_id)
        if duration:
            until = int(time.time() + duration)
            await bot.ban_chat_member(chat_id, user_id, until_date=until)
            ban_type = f"geçici ban ({duration_str_label})"
        else:
            await bot.ban_chat_member(chat_id, user_id)
            ban_type = "kalıcı ban"

        username = member.user.username or member.user.first_name

        async with _db_lock:
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO ban_list (chat_id, user_id, username, reason, banned_at, banned_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (chat_id, user_id, username, reason, time.time(), update.effective_user.id))
                if duration:
                    conn.execute("""
                        INSERT OR REPLACE INTO temp_bans (chat_id, user_id, username, unban_at)
                        VALUES (?, ?, ?, ?)
                    """, (chat_id, user_id, username, int(time.time() + duration)))
                conn.commit()

        channel['stats']['bans'] = channel['stats'].get('bans', 0) + 1
        save_channel_settings(chat_id, channel)
        who = mention(member.user)
        reason_h = html.escape(reason)
        await update.message.reply_text(
            f"🚫 {who} {ban_type} aldı! Sebep: {reason_h}\n📨 İtiraz için bota özelden /itiraz yazabilir.",
            parse_mode=ParseMode.HTML, reply_markup=mod_markup(chat_id, user_id, 'ban'))
        await send_log(chat_id, f"🚫 {who} {ban_type} | Sebep: {reason_h} | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, ban_type, user_id, username, update.effective_user.id, update.effective_user.username or '', reason)
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def unban(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_ban'):
        return

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
        user_id = target.id
    elif context.args and (context.args[0].isdigit() or context.args[0].startswith('@')):
        ref = context.args[0]
        user_id, member = await resolve_user(chat_id, ref)
        if not member:
            await update.message.reply_text("Kullanıcı bulunamadı!")
            return
        target = member.user
    else:
        await update.message.reply_text("Kullanıcı belirt! Reply at veya ID/username ver.")
        return

    try:
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM ban_list WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                conn.commit()
        who = mention(target)
        await update.message.reply_text(f"✅ {who} unban edildi!", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"✅ {who} ban kaldırıldı | {chat_id}", ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def kick(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_kick'):
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    if (why := hierarchy_block(chat_id, update.effective_user.id, user_id)):
        await update.message.reply_text(why)
        return
    try:
        channel = get_channel_settings(chat_id)
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        channel['stats']['kicks'] = channel['stats'].get('kicks', 0) + 1
        save_channel_settings(chat_id, channel)
        username = member.user.username or member.user.first_name
        who = mention(member.user)
        await update.message.reply_text(f"👢 {who} kicklendi!", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"👢 {who} kicklendi | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, 'kick', user_id, username, update.effective_user.id, update.effective_user.username or '', '')
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def mute(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_mute'):
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    if (why := hierarchy_block(chat_id, update.effective_user.id, user_id)):
        await update.message.reply_text(why)
        return

    duration_str = None
    hours = 24
    duration_arg = None
    if update.message.reply_to_message:
        duration_arg = context.args[0] if context.args else None
    else:
        duration_arg = context.args[1] if len(context.args) > 1 else None

    if duration_arg:
        if duration_arg[:-1].isdigit() and duration_arg[-1] in ['m', 'h', 'd']:
            val = int(duration_arg[:-1])
            unit = duration_arg[-1]
            if unit == 'm':
                hours = val / 60
                duration_str = f"{val} dakika"
            elif unit == 'h':
                hours = val
                duration_str = f"{val} saat"
            elif unit == 'd':
                hours = val * 24
                duration_str = f"{val} gün"
        elif duration_arg.isdigit():
            hours = int(duration_arg)
            duration_str = f"{hours} saat"

    if not duration_str:
        duration_str = f"{hours} saat"
    if hours * 3600 > ADMIN_MAX_MUTE and not has_permission(chat_id, update.effective_user.id, LVL_UST):
        hours, duration_str = ADMIN_MAX_MUTE // 3600, "24 saat (Admin sınırı)"

    try:
        until_date = int(time.time() + hours * 3600)
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        username = member.user.username or member.user.first_name

        async with _db_lock:
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO mute_list (chat_id, user_id, username, until_date, muted_at, muted_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (chat_id, user_id, username, until_date, time.time(), update.effective_user.id))
                conn.commit()

        channel = get_channel_settings(chat_id)
        channel['stats']['mutes'] = channel['stats'].get('mutes', 0) + 1
        save_channel_settings(chat_id, channel)
        who = mention(member.user)
        await update.message.reply_text(f"🔇 {who} {duration_str} mute edildi!", parse_mode=ParseMode.HTML,
                                        reply_markup=mod_markup(chat_id, user_id, 'mute'))
        await send_log(chat_id, f"🔇 {who} {duration_str} mute | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, 'mute', user_id, username, update.effective_user.id, update.effective_user.username or '', duration_str)
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def unmute(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_mute'):
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=await _default_member_permissions(chat_id)
        )
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM mute_list WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                conn.commit()
        who = mention(member.user)
        await update.message.reply_text(f"{_emoji('unmute', True)} {who} unmute edildi!", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"{_emoji('unmute', True)} {who} unmute edildi | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, 'unmute', user_id, member.user.username or member.user.first_name or '',
                             update.effective_user.id, update.effective_user.username or '', 'manuel unmute')
    except Exception as e:
        logger.error(f"Unmute hatası ({chat_id}/{user_id}): {e}")
        await update.message.reply_text(f"Unmute başarısız: {friendly_error(e)}")

async def warn(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_warn'):
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    if (why := hierarchy_block(chat_id, update.effective_user.id, user_id)):
        await update.message.reply_text(why)
        return

    if update.message.reply_to_message:
        reason = ' '.join(context.args) if context.args else "Sebep belirtilmedi"
    else:
        reason = ' '.join(context.args[1:]) if len(context.args) > 1 else "Sebep belirtilmedi"

    username = member.user.username or member.user.first_name
    channel = get_channel_settings(chat_id)

    await apply_punishment(
        chat_id,
        user_id,
        username,
        reason,
        channel,
        user=member.user,
        thread=thread_kw(update.effective_message, chat_id),
        actor_id=update.effective_user.id,
        actor_username=update.effective_user.username or update.effective_user.first_name or 'yetkili'
    )

async def unwarn(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_unwarn'):
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    if (why := hierarchy_block(chat_id, update.effective_user.id, user_id)):
        await update.message.reply_text(why)
        return
    async with _db_lock:
        with get_db() as conn:
            row = conn.execute("SELECT warn_count FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)).fetchone()
            current = row['warn_count'] if row else 0
            new_count = max(0, current - 1)
            if new_count > 0:
                conn.execute("UPDATE warnings SET warn_count = ?, last_warn_at = ? WHERE chat_id = ? AND user_id = ?",
                             (new_count, time.time(), chat_id, user_id))
            else:
                conn.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            conn.commit()
    who = mention(member.user)
    await update.message.reply_text(f"{_emoji('success', True)} {who} uyarısı geri alındı ({new_count}/{get_channel_settings(chat_id)['settings'].get('warn_limit', 5)}).", parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"↩️ {who} uyarı geri alındı ({new_count}) | {chat_id}", ParseMode.HTML)
    await log_mod_action(chat_id, 'unwarn', user_id, member.user.username or member.user.first_name or '',
                         update.effective_user.id, update.effective_user.username or '', f"kalan:{new_count}")

async def warns(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    with get_db() as conn:
        row = conn.execute(
            "SELECT warn_count FROM warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()
    warn_count = row['warn_count'] if row else 0
    channel = get_channel_settings(chat_id)
    warn_limit = channel['settings'].get('warn_limit', 5) if channel else 5
    await update.message.reply_text(f"📊 {mention(member.user)} uyarı: {warn_count}/{warn_limit}",
                                    parse_mode=ParseMode.HTML)

async def pin(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_pin'):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Pin için bir mesaja reply at ve /pin yaz!")
        return
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=update.message.reply_to_message.message_id,
            disable_notification=False
        )
        await update.message.reply_text("📌 Mesaj sabitlendi!")
        await send_log(chat_id, f"📌 Mesaj sabitlendi | {chat_id}")
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def unpin(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_pin'):
        return
    try:
        if update.message.reply_to_message:
            await bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=update.message.reply_to_message.message_id
            )
        else:
            await bot.unpin_all_chat_messages(chat_id=chat_id)
        await update.message.reply_text("✅ Sabitleme kaldırıldı!")
        await send_log(chat_id, f"📍 Sabitleme kaldırıldı | {chat_id}")
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def slowmode(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_purge'):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /slowmode <saniye> (0 = kapat)\nÖrnek: /slowmode 30")
        return
    try:
        seconds = int(context.args[0])
        await bot.set_chat_slow_mode_delay(chat_id=chat_id, slow_mode_delay=seconds)
        if seconds == 0:
            await update.message.reply_text("⏩ Yavaş mod kapatıldı.")
        else:
            await update.message.reply_text(f"🐢 Yavaş mod ayarlandı: {seconds} saniye.")
        await send_log(chat_id, f"🐢 Slowmode {seconds}sn | {mention(update.effective_user)}", ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("Geçerli bir saniye değeri gir!")
    except Exception as e:
        await update.message.reply_text(friendly_error(e))

async def temizle(update: Update, context):
    """/temizle <sayı> veya /temizle all — toplu silme (deleteMessages) arka planda çalışır, bot beklemez."""
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    if not await require(update, chat_id, 'can_purge'):
        return
    if update.effective_chat.type == 'private':
        await update.message.reply_text("Bu komutu temizlenecek grubun içinde kullan.")
        return
    arg = context.args[0].lower() if context.args else ''
    if arg != 'all' and not arg.isdigit():
        await update.message.reply_text("Kullanim: /temizle <sayi> veya /temizle all")
        return

    count = 20000 if arg == 'all' else max(1, min(int(arg), 1000))
    cmd_msg_id = update.message.message_id
    ids = list(range(cmd_msg_id, max(0, cmd_msg_id - count - 1), -1))  # komut mesajı dahil
    label = "Son 20.000 mesaj" if arg == 'all' else f"Son {count} mesaj"
    context.application.create_task(
        _run_cleanup(chat_id, ids, label, mention(update.effective_user), thread_kw(update.message), context.job_queue),
        update=update)

async def _bulk_delete(chat_id: str, message_ids) -> int:
    """deleteMessages ile 100'erli toplu silme; bulunamayan mesajlar Telegram tarafından atlanır.
    Toplu istek hata verirse o parti tek tek denenir. Dönüş: işlenen parti sayısı."""
    ids = [m for m in message_ids if m and m > 0]
    batches = fails_in_row = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
            fails_in_row = 0
        except Exception as e:
            logger.debug(f"Toplu silme hatası ({chat_id}): {e}")
            results = await asyncio.gather(*[bot.delete_message(chat_id, m) for m in chunk], return_exceptions=True)
            fails_in_row = fails_in_row + 1 if all(isinstance(r, Exception) for r in results) else 0
            if fails_in_row >= 3:
                break
        batches += 1
    return batches

async def _run_cleanup(chat_id: str, ids: list, label: str, by: str, tkw: dict, job_queue):
    """Arka plan temizliği: büyük silmelerde önce durum mesajı, sonunda 5 sn görünen sonuç mesajı."""
    status = None
    try:
        if len(ids) > 1000:
            status = await bot.send_message(chat_id, "🧹 Mesajlar siliniyor, lütfen bekle...", **tkw)
        await _bulk_delete(chat_id, ids)
        text = f"🧹 {label} temizlendi."
        if status:
            await status.edit_text(text)
        else:
            status = await bot.send_message(chat_id, text, **tkw)
    except Exception as e:
        logger.error(f"Temizleme hatası ({chat_id}): {e}")
    if status and job_queue:
        job_queue.run_once(_delete_message_job, 5, data={'chat_id': chat_id, 'message_id': status.message_id})
    await send_log(chat_id, f"🧹 {html.escape(label)} temizlendi | {by}", ParseMode.HTML)

async def banlist(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, reason, banned_at FROM ban_list WHERE chat_id = ? ORDER BY banned_at DESC LIMIT 20",
            (chat_id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("📋 Ban listesi boş.")
        return
    msg = "🚫 <b>Ban Listesi</b> (son 20):\n<blockquote expandable>"
    for row in rows:
        who = mention_html(row['user_id'], row['username'] or str(row['user_id']))
        dt = datetime.fromtimestamp(row['banned_at'], TZ_TR).strftime('%d.%m.%Y')
        msg += f"• {who} — {html.escape(row['reason'] or '?')} ({dt})\n"
    await update.message.reply_text(msg.rstrip() + "</blockquote>", parse_mode=ParseMode.HTML)

async def mutelist(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    now = time.time()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, until_date, muted_at FROM mute_list WHERE chat_id = ? ORDER BY muted_at DESC LIMIT 20",
            (chat_id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("📋 Mute listesi boş.")
        return
    msg = "🔇 <b>Mute Listesi</b> (son 20):\n<blockquote expandable>"
    for row in rows:
        who = mention_html(row['user_id'], row['username'] or str(row['user_id']))
        if row['until_date'] and row['until_date'] > now:
            remaining = int((row['until_date'] - now) / 60)
            time_str = f"{remaining} dk kaldı"
        else:
            time_str = "süresi dolmuş"
        msg += f"• {who} — {time_str}\n"
    await update.message.reply_text(msg.rstrip() + "</blockquote>", parse_mode=ParseMode.HTML)

async def spam_koruma(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Kullanım: /spamkoruma on|off")
        return
    state = context.args[0].lower() == 'on'
    channel = get_channel_settings(chat_id)
    channel['settings']['spam_protection'] = state
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Spam koruma {'açıldı' if state else 'kapatıldı'}!")
    await send_log(chat_id, f"⚙️ Spam koruma {'açıldı' if state else 'kapatıldı'} | {mention(update.effective_user)}", ParseMode.HTML)

async def word_ban(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /wordban <kelime>")
        return
    word = ' '.join(context.args).strip()
    if word.lower().startswith('re:'):
        try:
            re.compile(word[3:])
        except re.error as e:
            await update.message.reply_text(f"Geçersiz regex: {e}")
            return
    else:
        word = word.lower()
    channel = get_channel_settings(chat_id)
    if word not in channel['settings']['banned_words']:
        channel['settings']['banned_words'].append(word)
        save_channel_settings(chat_id, channel)
        await update.message.reply_text(f"'{word}' yasaklı kelime listesine eklendi!")
    else:
        await update.message.reply_text(f"'{word}' zaten yasaklı!")

async def word_ban_on(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    channel = get_channel_settings(chat_id)
    channel['settings']['word_ban_enabled'] = True
    save_channel_settings(chat_id, channel)
    await update.message.reply_text("Kelime yasaklama sistemi açıldı!")

async def word_ban_off(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    channel = get_channel_settings(chat_id)
    channel['settings']['word_ban_enabled'] = False
    save_channel_settings(chat_id, channel)
    await update.message.reply_text("Kelime yasaklama sistemi kapatıldı!")

async def set_auto_accept(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    channel = get_channel_settings(chat_id)
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Kullanım: /setautoaccept on|off")
        return
    state = context.args[0].lower() == 'on'
    channel['settings']['auto_accept'] = state
    if state:
        channel['settings']['auto_reject'] = False
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Otomatik kabul {'açıldı' if state else 'kapatıldı'}!")

async def set_auto_reject(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    channel = get_channel_settings(chat_id)
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Kullanım: /setautoreject on|off")
        return
    state = context.args[0].lower() == 'on'
    channel['settings']['auto_reject'] = state
    if state:
        channel['settings']['auto_accept'] = False
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Otomatik red {'açıldı' if state else 'kapatıldı'}!")

async def set_auto_reject_bot(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    channel = get_channel_settings(chat_id)
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Kullanım: /setautorejectbot on|off")
        return
    state = context.args[0].lower() == 'on'
    channel['settings']['auto_reject_bot'] = state
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Bot/sahte hesap reddi {'açıldı' if state else 'kapatıldı'}!")

async def invite_stats(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    channel = get_channel_settings(chat_id)
    invites = channel.get('invites', {})
    if not invites:
        await update.message.reply_text("Henüz davet istatistiği yok!")
        return
    msg = "📈 Davet İstatistikleri:\n"
    for user_id_str, count in invites.items():
        try:
            user = await bot.get_chat_member(chat_id, int(user_id_str))
            msg += f"{mention(user.user)}: {count} üye\n"
        except Exception:
            continue
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def leaderboard(update: Update, context):
    await _send_leaderboard(update, context, 'toplam')

_raid_locked: set = set()

async def anti_raid_check(chat_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('anti_raid', False):
        return False

    now = time.time()
    limit = channel['settings'].get('raid_limit', 10)
    timeframe = channel['settings'].get('raid_timeframe', 30)

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO raid_joins (chat_id, user_id, timestamp) VALUES (?, ?, ?)",
                (chat_id, user_id, now)
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM raid_joins WHERE chat_id = ? AND timestamp > ?",
                (chat_id, now - timeframe)
            ).fetchone()[0]
            conn.commit()

    if count >= limit:
        if chat_id not in _raid_locked:
            await raid_lock(chat_id, RAID_LOCK_SECONDS, f"🚨 RAİD TESPİT EDİLDİ!\n{count} üye / {timeframe} saniye")
        return True
    return False

async def raid_lock(chat_id: str, seconds: int, reason: str) -> bool:
    """Grubu kilitler (kimse yazamaz); eski izinler saklanır, süre dolunca otomatik açılır."""
    if chat_id in _raid_locked:
        return False
    saved = None
    try:
        chat = await bot.get_chat(chat_id)
        saved = chat.permissions.to_dict() if chat.permissions else None
    except Exception as e:
        logger.debug(f"Kilit öncesi izinler alınamadı: {e}")
    try:
        await bot.set_chat_permissions(chat_id=chat_id, permissions=ChatPermissions.no_permissions())
    except Exception as e:
        logger.error(f"Raid kilitleme hata: {e}")
        return False
    _raid_locked.add(chat_id)
    # Kilit bilgisi DB'ye yazılır → bot yeniden başlasa da süre dolunca açılır
    ch = get_channel_settings(chat_id)
    if ch:
        ch['settings']['raid_lock'] = {'until': time.time() + seconds, 'saved_perms': saved}
        save_channel_settings(chat_id, ch)
    await send_log(chat_id, f"{reason}\nGrup {seconds // 60} dk kilitlendi. Açmak için: /antiraid_ac\nKanal: {chat_id}",
                   ParseMode.HTML)
    await notify_managers(chat_id, f"🔒 Grup {seconds // 60} dk kilitlendi: {chat_id}\n{reason}", parse_mode=ParseMode.HTML)
    return True

async def cmd_kilit(update: Update, context):
    """/kilit [dk] — acil durum: grubu kilitler (Üst Admin ve üstü). Açmak Yardımcı Kurucu ve üstünde."""
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.effective_message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_lock'):
        return
    minutes = int(context.args[0]) if context.args and context.args[0].isdigit() else 10
    minutes = max(1, min(minutes, 1440))
    if chat_id in _raid_locked:
        await update.effective_message.reply_text("Grup zaten kilitli.")
        return
    ok = await raid_lock(chat_id, minutes * 60, f"🚨 Acil kilit: {mention(update.effective_user)}")
    await update.effective_message.reply_text(
        f"🔒 Grup {minutes} dk kilitlendi. Kilidi Yardımcı Kurucu ve üstü /antiraid_ac ile açabilir."
        if ok else "Kilitlenemedi (botun yetkilerini kontrol et).")

RAID_LOCK_SECONDS = 600

async def raid_unlock(chat_id: str) -> bool:
    """Raid kilidini açar ve grubun ESKİ izinlerini geri yükler."""
    _raid_locked.discard(chat_id)
    ch = get_channel_settings(chat_id)
    lock = ch['settings'].pop('raid_lock', None) if ch else None
    perms = None
    if lock and lock.get('saved_perms'):
        try:
            perms = ChatPermissions.de_json(lock['saved_perms'], bot)
        except Exception:
            perms = None
    if perms is None:
        perms = ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_invite_users=True,
        )
    try:
        await bot.set_chat_permissions(chat_id=chat_id, permissions=perms)
    except Exception as e:
        logger.error(f"Raid kilit açma hata: {e}")
        return False
    if ch:
        save_channel_settings(chat_id, ch)
    return True

async def check_raid_locks(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM channels WHERE settings LIKE '%raid_lock%'").fetchall()
    for r in rows:
        ch = get_channel_settings(r['chat_id'])
        lock = ch['settings'].get('raid_lock') if ch else None
        if not lock:
            continue
        _raid_locked.add(r['chat_id'])
        if now >= lock.get('until', 0):
            if await raid_unlock(r['chat_id']):
                await send_log(r['chat_id'], f"✅ Raid kilidi otomatik açıldı | {r['chat_id']}")

async def cmd_antiraid(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        channel = get_channel_settings(chat_id)
        s = channel['settings']
        state = s.get('anti_raid', False)
        limit = s.get('raid_limit', 10)
        tf = s.get('raid_timeframe', 30)
        locked = "🔒 Şu an KİLİTLİ" if chat_id in _raid_locked else "🔓 Açık"
        await update.message.reply_text(
            f"🚨 Anti-Raid: {'AÇIK' if state else 'KAPALI'}\n"
            f"Limit: {limit} üye / {tf} saniye\n"
            f"Durum: {locked}\n\n"
            f"Kullanım: /antiraid on|off\n"
            f"Limit değiştir: /antiraid on 15 20 (15 üye/20sn)"
        )
        return
    new_state = args[0].lower() == 'on'
    channel = get_channel_settings(chat_id)
    channel['settings']['anti_raid'] = new_state
    if len(args) >= 3:
        try:
            channel['settings']['raid_limit'] = int(args[1])
            channel['settings']['raid_timeframe'] = int(args[2])
        except Exception as e:
            logger.debug(f"cmd_antiraid: {e}")
    save_channel_settings(chat_id, channel)
    lim = channel['settings'].get('raid_limit', 10)
    tf = channel['settings'].get('raid_timeframe', 30)
    await update.message.reply_text(
        f"🚨 Anti-raid {'açıldı' if new_state else 'kapatıldı'}.\n"
        f"Limit: {lim} üye / {tf} saniye"
    )
    await send_log(chat_id, f"🚨 Anti-raid {'açıldı' if new_state else 'kapatıldı'} | {mention(update.effective_user)}", ParseMode.HTML)

async def cmd_antiraid_ac(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    ch = get_channel_settings(chat_id)
    if chat_id not in _raid_locked and not (ch and ch['settings'].get('raid_lock')):
        await update.message.reply_text("Grup zaten kilitli değil.")
        return
    if await raid_unlock(chat_id):
        await update.message.reply_text("✅ Raid kilidi açıldı, grup eski izinlerine döndü.")
        await send_log(chat_id, f"✅ Raid kilidi manuel açıldı | {mention(update.effective_user)}", ParseMode.HTML)
    else:
        await update.message.reply_text("Hata: kilit açılamadı (botun yetkilerini kontrol et).")

async def profil(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    viewing_other = bool(context.args) or bool(update.message.reply_to_message)
    if viewing_other and not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    if not viewing_other:
        target_id = update.effective_user.id
        try:
            member = await bot.get_chat_member(chat_id, target_id)
        except Exception:
            await update.message.reply_text("Profil alinamadi!")
            return
    else:
        target_id, member = await resolve_user(
            chat_id,
            context.args[0] if context.args else None,
            update.message.reply_to_message.from_user if update.message.reply_to_message else None
        )
        if not member or not target_id:
            await update.message.reply_text("Kullanici bulunamadi!")
            return

    with get_db() as conn:
        warn_row = conn.execute(
            "SELECT warn_count, last_warn_at FROM warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, target_id)
        ).fetchone()

        ban_row = conn.execute(
            "SELECT reason, banned_at, username FROM ban_list WHERE chat_id = ? AND user_id = ?",
            (chat_id, target_id)
        ).fetchone()

        mute_row = conn.execute(
            "SELECT until_date, muted_at FROM mute_list WHERE chat_id = ? AND user_id = ?",
            (chat_id, target_id)
        ).fetchone()

        log_rows = conn.execute(
            "SELECT action, by_user_id, by_username, reason, timestamp FROM mod_log WHERE chat_id = ? AND target_user_id = ? ORDER BY timestamp DESC LIMIT 5",
            (chat_id, target_id)
        ).fetchall()

    warn_count = warn_row['warn_count'] if warn_row else 0
    last_warn = datetime.fromtimestamp(warn_row['last_warn_at'], TZ_TR).strftime('%d.%m.%Y %H:%M') if warn_row and warn_row['last_warn_at'] else "-"

    durum = "Normal"
    if ban_row:
        durum = "BANLANDI"
    elif mute_row and mute_row['until_date'] and mute_row['until_date'] > time.time():
        kalan = int((mute_row['until_date'] - time.time()) / 60)
        durum = f"Susturuldu ({kalan//60}s {kalan%60}dk kaldi)" if kalan >= 60 else f"Susturuldu ({kalan} dk kaldi)"

    warn_limit = get_channel_settings(chat_id)['settings'].get('warn_limit', 5)
    msg = (
        "👤 <b>Kullanici Profili</b>\n"
        f"Ad: {mention(member.user)}\n"
        f"ID: <code>{target_id}</code>\n"
        f"Durum: {durum}\n\n"
        f"Uyari: {warn_count}/{warn_limit}"
    )
    if warn_count > 0:
        msg += f" (Son: {last_warn})"

    if log_rows:
        msg += "\n\nSon Islemler:\n"
        for row in log_rows:
            tarih = datetime.fromtimestamp(row['timestamp'], TZ_TR).strftime('%d.%m %H:%M')
            if row['by_user_id'] and row['by_user_id'] != cur_bot_id():
                by = mention_html(row['by_user_id'], row['by_username'] or str(row['by_user_id']))
            else:
                by = "sistem"
            sebep = f" - {html.escape(row['reason'])}" if row['reason'] else ""
            msg += f"  {tarih} {html.escape(row['action'])} by {by}{sebep}\n"
    else:
        msg += "\n\nHic moderasyon islemi yok."

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def wordlist(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return

    channel = get_channel_settings(chat_id)
    words = channel['settings'].get('banned_words', [])
    enabled = channel['settings'].get('word_ban_enabled', False)

    if not words:
        await update.message.reply_text(
            f"Kelime yasaklama: {'ACIK' if enabled else 'KAPALI'}\n\n"
            "Hic yasakli kelime yok.\n"
            "Eklemek icin: /wordban <kelime>"
        )
        return

    kelimeler = "\n".join([f"  {i+1}. {w}" for i, w in enumerate(words)])

    keyboard = []
    for i, word in enumerate(words):
        keyboard.append([ibtn(f"Sil: {word}", f"wdel|{chat_id}|{i}", RED)])
    keyboard.append([ibtn("Tumunu Sil", f"wdel_all|{chat_id}", RED)])
    keyboard.append([InlineKeyboardButton("Kapat", callback_data=f"wdel_close|{chat_id}")])

    await update.message.reply_text(
        f"Kelime Yasaklama: {'ACIK' if enabled else 'KAPALI'}\n"
        f"Toplam: {len(words)} kelime\n\n"
        f"{kelimeler}\n\n"
        "Silmek icin asagidaki butonlari kullan:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def wordlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = data.split("|")[1]
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    await query.answer()

    if data.startswith("wdel_close|"):
        await query.edit_message_text("Kelime listesi kapatildi.")
        return

    if data.startswith("wdel_all|"):
        channel = get_channel_settings(chat_id)
        channel['settings']['banned_words'] = []
        save_channel_settings(chat_id, channel)
        await query.edit_message_text("Tum yasakli kelimeler silindi.")
        await send_log(chat_id, f"Tum yasakli kelimeler silindi | {mention(query.from_user)}", ParseMode.HTML)
        return

    if data.startswith("wdel|"):
        idx = int(data.split("|")[2])
        channel = get_channel_settings(chat_id)
        words = channel['settings'].get('banned_words', [])
        if 0 <= idx < len(words):
            silinen = words.pop(idx)
            channel['settings']['banned_words'] = words
            save_channel_settings(chat_id, channel)
            await send_log(chat_id, f"Yasakli kelime silindi: {html.escape(silinen)} | {mention(query.from_user)}", ParseMode.HTML)

        if not words:
            await query.edit_message_text("Tum kelimeler silindi.")
            return

        kelimeler = "\n".join([f"  {i+1}. {w}" for i, w in enumerate(words)])
        keyboard = []
        for i, word in enumerate(words):
            keyboard.append([ibtn(f"Sil: {word}", f"wdel|{chat_id}|{i}", RED)])
        keyboard.append([ibtn("Tumunu Sil", f"wdel_all|{chat_id}", RED)])
        keyboard.append([InlineKeyboardButton("Kapat", callback_data=f"wdel_close|{chat_id}")])

        await query.edit_message_text(
            f"Kelime Yasaklama: ACIK\n"
            f"Toplam: {len(words)} kelime\n\n"
            f"{kelimeler}\n\n"
            "Silmek icin asagidaki butonlari kullan:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def get_nightmod(chat_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM nightmod_settings WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row:
        return None
    return dict(row)

def save_nightmod(chat_id: str, data: dict):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO nightmod_settings
            (chat_id, enabled, start_hour, start_minute, end_hour, end_minute, restrictions, saved_permissions, is_active, configured)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id,
            data.get('enabled', 0),
            data.get('start_hour', 23),
            data.get('start_minute', 0),
            data.get('end_hour', 7),
            data.get('end_minute', 0),
            json.dumps(data.get('restrictions', {})),
            (data['saved_permissions'] if isinstance(data.get('saved_permissions'), str)
             else json.dumps(data['saved_permissions'])) if data.get('saved_permissions') else None,
            data.get('is_active', 0),
            data.get('configured', 0),
        ))
        conn.commit()

def _nightmod_keyboard(chat_id: str, restrictions: dict) -> InlineKeyboardMarkup:
    def btn(label, key):
        state = "ON" if restrictions.get(key, False) else "OFF"
        icon = "✅" if restrictions.get(key, False) else "❌"
        return ibtn(f"{icon} {label}", f"nm_toggle|{chat_id}|{key}", GREEN if restrictions.get(key, False) else RED)
    keyboard = [
        [btn("Mesaj Gonderme", "block_messages"),
         btn("Medya Gonderme", "block_media")],
        [btn("Ses/Video Not", "block_voice"),
         btn("Sticker/GIF", "block_sticker")],
        [btn("Link Gonderme", "block_links"),
         btn("Dosya Gonderme", "block_files")],
        [ibtn("Kaydet", f"nm_save|{chat_id}", GREEN),
         ibtn("Iptal", f"nm_cancel|{chat_id}", RED)]
    ]
    return InlineKeyboardMarkup(keyboard)

async def cmd_nightmod(update: Update, context):
    
    if update.effective_chat.type == 'private':
        chat_id = context.user_data.get('selected_channel')
    else:
        chat_id = str(update.effective_chat.id)

    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile kanali sec!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return

    args = context.args

    if args and args[0].lower() == 'ac':
        await nightmod_activate(chat_id)
        await update.message.reply_text("Night mode manuel olarak acildi.")
        return
    if args and args[0].lower() == 'kapat':
        await nightmod_deactivate(chat_id)
        await update.message.reply_text("Night mode manuel olarak kapatildi.")
        return

    nm = get_nightmod(chat_id)

    if args and ':' in args[0]:
        try:
            sh, sm = map(int, args[0].split(':'))
            eh, em = map(int, args[1].split(':')) if len(args) > 1 else (7, 0)
            if nm:
                nm_data = dict(nm)
                nm_data['start_hour'] = sh
                nm_data['start_minute'] = sm
                nm_data['end_hour'] = eh
                nm_data['end_minute'] = em
                nm_data['restrictions'] = json.loads(nm_data.get('restrictions', '{}')) if isinstance(nm_data.get('restrictions'), str) else nm_data.get('restrictions', {})
            else:
                nm_data = {
                    'enabled': 1, 'start_hour': sh, 'start_minute': sm,
                    'end_hour': eh, 'end_minute': em, 'restrictions': {},
                    'is_active': 0, 'configured': 1
                }
            save_nightmod(chat_id, nm_data)
            await update.message.reply_text(
                f"Night mode saati ayarlandi:\n"
                f"Baslangic: {sh:02d}:{sm:02d}\n"
                f"Bitis: {eh:02d}:{em:02d}\n\n"
                f"Kısıtlamaları ayarlamak için: /nightmod"
            )
            return
        except Exception:
            await update.message.reply_text("Format hatasi. Kullanim: /nightmod 23:00 07:00")
            return

    if not nm or not nm['configured']:
        if update.effective_chat.type != 'private':
            keyboard = [[InlineKeyboardButton(
                "Yapilandir (DM)",
                url=f"https://t.me/{context.bot.username}?start=nightmod_{chat_id}"
            )]]
            await update.message.reply_text(
                "Night mode ilk kez kullaniliyor! Yapilandirmak icin bota ozel mesaj gonder:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

    if nm:
        restrictions = json.loads(nm['restrictions']) if isinstance(nm['restrictions'], str) else nm['restrictions']
        sh, sm = nm['start_hour'], nm['start_minute']
        eh, em = nm['end_hour'], nm['end_minute']
        durum = "ACIK" if nm['enabled'] else "KAPALI"
        aktif = "Aktif" if nm['is_active'] else "Pasif"
    else:
        restrictions = {}
        sh, sm, eh, em = 23, 0, 7, 0
        durum = "KAPALI"
        aktif = "Pasif"

    keyboard = _nightmod_keyboard(chat_id, restrictions)
    await update.message.reply_text(
        f"Night Mode: {durum} ({aktif})\n"
        f"Saat: {sh:02d}:{sm:02d} - {eh:02d}:{em:02d} (UTC+3)\n\n"
        f"Kısıtlanacak izinleri sec, sonra Kaydet'e bas:",
        reply_markup=keyboard
    )

async def nightmod_activate(chat_id: str):
    """Gece modunu başlatır: mevcut izinleri tam olarak saklar, seçili kısıtlamaları ayrı ayrı uygular."""
    nm = get_nightmod(chat_id)
    if not nm:
        return
    try:
        chat = await bot.get_chat(chat_id)
        base = chat.permissions or await _default_member_permissions(chat_id)
        saved = base.to_dict()
        r = json.loads(nm['restrictions']) if isinstance(nm['restrictions'], str) else (nm['restrictions'] or {})

        def keep(attr: str, block_key: str) -> bool:
            return bool(getattr(base, attr, True)) and not r.get('block_messages') and not r.get(block_key)

        new_perms = ChatPermissions(
            can_send_messages=bool(base.can_send_messages) and not r.get('block_messages'),
            can_send_photos=keep('can_send_photos', 'block_media'),
            can_send_videos=keep('can_send_videos', 'block_media'),
            can_send_voice_notes=keep('can_send_voice_notes', 'block_voice'),
            can_send_video_notes=keep('can_send_video_notes', 'block_voice'),
            can_send_documents=keep('can_send_documents', 'block_files'),
            can_send_audios=keep('can_send_audios', 'block_files'),
            can_send_other_messages=keep('can_send_other_messages', 'block_sticker'),
            can_add_web_page_previews=keep('can_add_web_page_previews', 'block_links'),
            can_send_polls=keep('can_send_polls', 'block_messages'),
            can_invite_users=bool(base.can_invite_users),
            can_pin_messages=bool(base.can_pin_messages),
            can_change_info=bool(base.can_change_info),
            can_manage_topics=bool(base.can_manage_topics),
        )
        await bot.set_chat_permissions(chat_id, new_perms, use_independent_chat_permissions=True)

        nm_data = dict(nm)
        nm_data['is_active'] = 1
        nm_data['saved_permissions'] = saved
        nm_data['restrictions'] = r
        save_nightmod(chat_id, nm_data)
        await bot.send_message(
            chat_id,
            f"🌙 Gece modu başladı. Sabah {nm['end_hour']:02d}:{nm['end_minute']:02d}'e kadar kısıtlamalar aktif."
        )
        await send_log(chat_id, f"Night mode aktif oldu | {chat_id}")
    except Exception as e:
        logger.error(f"Night mode activate hata: {e}")

async def nightmod_deactivate(chat_id: str):
    """Gece modunu bitirir ve saklanan izinleri geri yükler."""
    nm = get_nightmod(chat_id)
    if not nm or not nm['is_active']:
        return
    try:
        saved_raw = nm['saved_permissions']
        if saved_raw:
            saved = json.loads(saved_raw) if isinstance(saved_raw, str) else saved_raw
            if isinstance(saved, str):  # eski sürümün çift kodladığı kayıt
                saved = json.loads(saved)
            if 'can_send_photos' in saved:
                await bot.set_chat_permissions(chat_id, ChatPermissions.de_json(saved, bot),
                                               use_independent_chat_permissions=True)
            else:  # eski biçim (4 alan)
                await bot.set_chat_permissions(chat_id, ChatPermissions(
                    can_send_messages=saved.get('can_send_messages', True),
                    can_send_other_messages=saved.get('can_send_other_messages', True),
                    can_add_web_page_previews=saved.get('can_add_web_page_previews', True),
                    can_invite_users=saved.get('can_invite_users', True),
                ))

        nm_data = dict(nm)
        nm_data['is_active'] = 0
        nm_data['saved_permissions'] = None
        nm_data['restrictions'] = json.loads(nm_data['restrictions']) if isinstance(nm_data['restrictions'], str) else nm_data['restrictions']
        save_nightmod(chat_id, nm_data)
        await bot.send_message(chat_id, "☀️ Gece modu sona erdi. Normal izinler geri yüklendi.")
        await send_log(chat_id, f"Night mode sona erdi | {chat_id}")
    except Exception as e:
        logger.error(f"Night mode deactivate hata: {e}")

async def check_nightmod(context: ContextTypes.DEFAULT_TYPE):
    
    from datetime import datetime, timezone, timedelta
    tz_tr = timezone(timedelta(hours=3))
    now = datetime.now(tz_tr)
    current_minutes = now.hour * 60 + now.minute

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM nightmod_settings WHERE enabled = 1").fetchall()

    for row in rows:
        chat_id = row['chat_id']
        start_total = row['start_hour'] * 60 + row['start_minute']
        end_total = row['end_hour'] * 60 + row['end_minute']
        is_active = row['is_active']

        if start_total > end_total:
            should_be_active = current_minutes >= start_total or current_minutes < end_total
        else:
            should_be_active = start_total <= current_minutes < end_total

        if should_be_active and not is_active:
            await nightmod_activate(chat_id)
        elif not should_be_active and is_active:
            await nightmod_deactivate(chat_id)

async def nightmod_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    query = update.callback_query
    data = query.data
    chat_id = data.split("|")[1]
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    await query.answer()

    if data.startswith("nm_cancel|"):
        await query.edit_message_text("Night mode yapilandirmasi iptal edildi.")
        return

    if data.startswith("nm_save|"):
        nm = get_nightmod(chat_id)
        if not nm:
            nm_data = {
                'enabled': 1, 'start_hour': 23, 'start_minute': 0,
                'end_hour': 7, 'end_minute': 0, 'restrictions': {},
                'is_active': 0, 'configured': 1
            }
        else:
            nm_data = dict(nm)
            nm_data['enabled'] = 1
            nm_data['configured'] = 1
            nm_data['restrictions'] = json.loads(nm_data['restrictions']) if isinstance(nm_data['restrictions'], str) else nm_data['restrictions']

        save_nightmod(chat_id, nm_data)
        restrictions = nm_data['restrictions']
        aktif_kisitlar = [k for k, v in restrictions.items() if v]
        isim_map = {
            'block_messages': 'Mesaj',
            'block_media': 'Medya',
            'block_voice': 'Ses/Video Not',
            'block_sticker': 'Sticker/GIF',
            'block_links': 'Link',
            'block_files': 'Dosya',
        }
        kisit_listesi = ", ".join([isim_map.get(k, k) for k in aktif_kisitlar]) or "Hic kisitlama secilmedi"
        await query.edit_message_text(
            f"Night mode kaydedildi!\n\n"
            f"Kisitlamalar: {kisit_listesi}\n"
            f"Saat ayarlamak icin: /nightmod 23:00 07:00"
        )
        await send_log(chat_id, f"Night mode yapilandirildi | {mention(query.from_user)}", ParseMode.HTML)
        return

    if data.startswith("nm_toggle|"):
        key = data.split("|")[2]
        nm = get_nightmod(chat_id)
        if nm:
            restrictions = json.loads(nm['restrictions']) if isinstance(nm['restrictions'], str) else nm['restrictions']
        else:
            restrictions = {}

        restrictions[key] = not restrictions.get(key, False)

        if nm:
            nm_data = dict(nm)
        else:
            nm_data = {
                'enabled': 0, 'start_hour': 23, 'start_minute': 0,
                'end_hour': 7, 'end_minute': 0, 'is_active': 0, 'configured': 0
            }
        nm_data['restrictions'] = restrictions
        save_nightmod(chat_id, nm_data)

        keyboard = _nightmod_keyboard(chat_id, restrictions)
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            logger.error(f"nightmod toggle hata: {e}")

async def check_expired_mutes(context: ContextTypes.DEFAULT_TYPE):
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM mute_list WHERE until_date > 0 AND until_date < ?", (time.time(),))
            conn.commit()

async def check_temp_bans(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT chat_id, user_id, username FROM temp_bans WHERE unban_at <= ?",
            (now,)
        ).fetchall()

    if not rows:
        return

    success = 0
    for row in rows:
        chat_id = row['chat_id']
        user_id = row['user_id']
        try:
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        except Exception as e:
            logger.error(f"[TEMP BAN] Unban başarısız ({chat_id}/{user_id}): {e}")
            await send_log(chat_id, f"{_emoji('error', True)} Geçici ban süresi doldu ama unban başarısız oldu: <code>{user_id}</code>", ParseMode.HTML)
            continue

        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM temp_bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                conn.execute("DELETE FROM ban_list WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                conn.commit()

        success += 1
        who = mention_html(user_id, row['username'] or str(user_id))
        await send_log(chat_id, f"{_emoji('clock', True)} Geçici ban sona erdi → {who} unban edildi", ParseMode.HTML)

    if success:
        logger.info(f"[TEMP BAN] {success}/{len(rows)} geçici ban Telegram üzerinde başarıyla kaldırıldı.")

# ═══════════════════════════ BUTONLU AYAR PANELİ ═══════════════════════════
# Callback biçimi: s|<chat_id>|<işlem>|<argümanlar...>
#   p <sayfa>              sayfa aç          t <anahtar> <sayfa>      aç/kapa
#   n <anahtar> u|d <sayfa> sayı artır/azalt a <koruma> <ceza>         koruma cezası
#   wa <ceza>              uyarı limiti cezası   i <tür>              metin girişi (ForceReply)
#   wd/ld <sıra> <sayfa>   kelime/link sil   nd <isim>                not sil
#   wc kelimeleri temizle · ne/nt/nh gece modu · lx log kaldır · rx kuralları sil · ru raid kilidi aç · x kapat

PROTECTIONS = {
    # anahtar: (etiket, ayar anahtarı, varsayılan ceza, açıklama)
    'link':    ("🔗 Link", 'anti_link', 'warn', "Link, gizli (yazıya gömülü) link ve link butonlu mesajlar silinir."),
    'word':    ("🔤 Yasaklı kelime", 'word_ban_enabled', 'warn', "Listedeki kelimeler; Türkçe karakter, büyük/küçük harf ve leetspeak dahil yakalanır."),
    'spam':    ("🔁 Tekrar spam", 'spam_protection', 'warn', "60 sn içindeki son 10 mesajın çoğu aynıysa spam sayılır."),
    'flood':   ("🌊 Flood", 'anti_spam_flood', 'mute', "Kısa sürede limitten fazla mesaj."),
    'forward': ("↪️ Forward", 'anti_forward', 'mute', "Başka sohbetten iletilen mesajlar."),
    'media':   ("🖼 Medya flood", 'anti_media_flood', 'mute', "Kısa sürede limitten fazla fotoğraf/video/sticker."),
    'badmedia': ("🔞 Uygunsuz medya", 'media_shield', 'ban',
                 "Porno/çıplaklık (yapay zeka, kuruluysa), engelli medya ve sticker paketleri, tehlikeli dosyalar (.apk, .exe…)."),
}
ACTION_LABELS = {'delete': 'Sil', 'warn': 'Uyar', 'mute': 'Sustur', 'kick': 'At', 'ban': 'Banla'}
WARN_ACTION_LABELS = {'ban': 'Banla', 'tempban': 'Geçici ban', 'kick': 'At', 'mute': 'Sustur'}
ESCALATING = ('flood', 'forward', 'media')  # susturma kademeli: 10 dk → 30 dk → 5 sa

# Sayısal ayarlar: (en az, en çok, adım veya hazır değerler, biçim; None = süre olarak göster)
NUMERIC = {
    'flood_limit':           (2, 30, 1, "{} mesaj"),
    'flood_timeframe':       (2, 60, 1, "{} sn"),
    'media_flood_limit':     (2, 30, 1, "{} medya"),
    'media_flood_timeframe': (2, 120, 2, "{} sn"),
    'raid_limit':            (3, 200, [3, 5, 8, 10, 15, 20, 30, 50, 100, 200], "{} üye"),
    'raid_timeframe':        (10, 600, [10, 20, 30, 60, 120, 300, 600], "{} sn"),
    'warn_limit':            (2, 20, 1, "{}"),
    'captcha_timeout':       (30, 600, 30, None),
    'newbie_minutes':        (0, 1440, [0, 5, 10, 15, 30, 60, 120, 360, 720, 1440], "{} dk"),
    'mute_minutes':          (1, 10080, [5, 10, 15, 30, 60, 120, 360, 720, 1440, 4320, 10080], "{} dk"),
    'warn_action_duration':  (3600, 30 * 86400, [3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 3 * 86400,
                                                 7 * 86400, 30 * 86400], None),
    'media_lock_minutes':    (5, 1440, [5, 10, 15, 30, 60, 120, 360, 720, 1440], "{} dk"),
    'edit_guard_minutes':    (1, 1440, [1, 2, 3, 5, 10, 15, 30, 60, 120, 360, 1440], "{} dk"),
    'welcome_autodel':       (0, 1440, [0, 1, 2, 5, 10, 30, 60, 180, 720, 1440], "{} dk"),
    'tag_size':              (1, 10, [1, 3, 5, 10], "{} kişi"),
    'vote_needed':           (2, 15, [3, 5, 7, 10, 15], "{} oy"),
    'vote_mute_minutes':     (10, 1440, [10, 30, 60, 180, 720, 1440], "{} dk"),
}
TOGGLE_KEYS = {v[1] for v in PROTECTIONS.values()} | {
    'anti_raid', 'captcha_enabled', 'join_captcha', 'auto_accept', 'auto_reject', 'auto_reject_bot',
    'restrict_no_username', 'welcome_enabled', 'nsfw_scan', 'file_block', 'media_autolock', 'edit_guard',
    'edit_notify', 'reports_enabled', 'recovery_autorestore', 'welcome_clean', 'welcome_batch', 'welcome_dm',
    'goodbye_enabled', 'fsub_enabled', 'tag_active_only', 'shared_blacklist', 'cas_enabled', 'name_track', 'vote_mute'}
NIGHT_RESTRICTIONS = [('block_messages', "Mesaj"), ('block_media', "Foto/Video"), ('block_voice', "Ses/Video not"),
                      ('block_sticker', "Sticker/GIF"), ('block_links', "Link önizleme"), ('block_files', "Dosya/Müzik")]
PAGE_SIZE = 8

RICH_INPUT_KINDS = {'welcome', 'goodbye', 'rules', 'sched'}  # medya da kabul edilir

INPUT_PROMPTS = {
    'welcome': ("✏️ Yeni hoş geldin mesajını yaz ya da fotoğraf/video/GIF gönder (açıklaması mesaj olur).\n"
                "Butonlar: her satıra  Etiket - https://link  (yan yana: && ile)\n"
                "Değişkenler: {kullanıcı} {ad} {username} {grup} {uye_sayisi} · Rastgele: mesajları %%% satırıyla ayır",
                "Hoş geldin {kullanıcı}!"),
    'goodbye': ("✏️ Veda mesajını yaz (medya ve butonlar da olur). Değişkenler: {ad} {kullanıcı} {grup}",
                "👋 {ad} aramızdan ayrıldı."),
    'rules': ("📜 Grup kurallarını yaz. Biçim (kalın, link) ve butonlar korunur.", "1) Saygılı ol  2) Reklam yok"),
    'fsub': ("📢 Zorunlu kanalı yaz: @kanal, t.me/kanal ya da -100… ID. Bot o kanalda yönetici olmalı.", "@kanal"),
    'sched': ("⏰ Önce süreyi, sonra mesajı yaz. Örn: 6sa Kuralları okumayı unutmayın!\n"
              "Süre: 30dk, 6sa, 1g · Medya için fotoğrafın açıklamasına yaz.", "6sa Mesaj"),
    'word': ("🔤 Yasaklanacak kelimeleri yaz (her satıra bir tane). Regex için başına re: koy.", "kelime"),
    'link': ("🔗 İzin verilecek alan adlarını yaz (boşlukla ayır). Örn: youtube.com t.me/kanalim", "youtube.com"),
    'note': ("📝 Notu şu biçimde yaz: isim içerik", "kurallar Grup kuralları..."),
    'log': ("🧾 Log kanalı/grubu ID'sini yaz (örn. -1001234567890). Bot orada mesaj atabilmeli.", "-1001234567890"),
    'recovery': ("🛟 Güvenilir kişinin kullanıcı ID'sini yaz (kişi bota /id yazarak öğrenebilir).", "123456789"),
}

def _can_edit_settings(chat_id: str, user_id: int) -> bool:
    return has_specific_permission(chat_id, user_id, 'can_manage_settings')

def _can_manage_log(chat_id: str, user_id: int, channel: dict) -> bool:
    return has_permission(chat_id, user_id, LVL_KURUCU)

def _step_value(key: str, cur: int, up: bool) -> int:
    lo, hi, step, _ = NUMERIC[key]
    if isinstance(step, list):
        if up:
            return next((v for v in step if v > cur), step[-1])
        return next((v for v in reversed(step) if v < cur), step[0])
    return max(lo, min(hi, cur + (step if up else -step)))

def _fmt_numeric(key: str, val: int) -> str:
    if key in ('newbie_minutes', 'welcome_autodel') and val == 0:
        return "Kapalı"
    fmt = NUMERIC[key][3]
    return human_duration(val) if fmt is None else fmt.format(val)

def _num_row(cid: str, key: str, s: dict, page: str, label: str) -> list:
    val = int(s.get(key, _default_channel_settings().get(key, 0)) or 0)
    return [ibtn("➖", f"s|{cid}|n|{key}|d|{page}"),
            ibtn(f"{label}: {_fmt_numeric(key, val)}", f"s|{cid}|p|{page}"),
            ibtn("➕", f"s|{cid}|n|{key}|u|{page}")]

def _back(cid: str, page: str = 'main') -> list:
    return [ibtn("⬅️ Geri", f"s|{cid}|p|{page}")]

def _paged(items: list, pg: int):
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    pg = max(0, min(pg, pages - 1))
    return pg, pages, items[pg * PAGE_SIZE:(pg + 1) * PAGE_SIZE]

def _nav_rows(cid: str, base: str, pg: int, pages: int) -> list:
    if pages <= 1:
        return []
    return [[ibtn("◀️", f"s|{cid}|p|{base}.{(pg - 1) % pages}"), ibtn(f"{pg + 1}/{pages}", f"s|{cid}|p|{base}.{pg}"),
             ibtn("▶️", f"s|{cid}|p|{base}.{(pg + 1) % pages}")]]

def _nm_data(chat_id: str) -> dict:
    nm = get_nightmod(chat_id)
    d = dict(nm) if nm else {'enabled': 0, 'start_hour': 23, 'start_minute': 0, 'end_hour': 7, 'end_minute': 0,
                             'is_active': 0, 'configured': 0, 'restrictions': {}, 'saved_permissions': None}
    if isinstance(d.get('restrictions'), str):
        d['restrictions'] = json.loads(d['restrictions'] or '{}')
    if isinstance(d.get('saved_permissions'), str):
        d['saved_permissions'] = json.loads(d['saved_permissions'])
    d['restrictions'] = d.get('restrictions') or {}
    return d

_title_cache: dict = {}

async def _chat_title(chat_id: str) -> str:
    cached = _title_cache.get(chat_id)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    try:
        title = (await bot.get_chat(chat_id)).title or chat_id
    except Exception as e:
        logger.debug(f"Sohbet adı alınamadı {chat_id}: {e}")
        title = chat_id
    _title_cache[chat_id] = (time.time(), title)
    return title

async def render_settings(cid: str, page: str = 'main'):
    """Panel sayfasının (metin, klavye) çiftini üretir."""
    channel = get_channel_settings(cid)
    s = channel['settings']
    base, _, sub = page.partition('.')
    title = html.escape(await _chat_title(cid))
    rows: list = []

    if base == 'prot':
        text = f"🛡 <b>Koruma</b> — {title}\n\nBir korumaya dokunarak aç/kapat, cezasını ve limitlerini ayarla."
        for key, (label, skey, default, _) in PROTECTIONS.items():
            on = s.get(skey, False)
            act = ACTION_LABELS.get(s.get(f'action_{key}') or default, '?')
            rows.append([ibtn(f"{'🟢' if on else '🔴'} {label} · {act}", f"s|{cid}|p|pr.{key}", GREEN if on else RED)])
        rows.append(_back(cid))

    elif base == 'pr' and sub in PROTECTIONS:
        label, skey, default, desc = PROTECTIONS[sub]
        act = s.get(f'action_{sub}') or default
        on = s.get(skey, False)
        text = (f"{label} <b>koruması</b> — {title}\n<i>{html.escape(desc)}</i>\n\n"
                f"Durum: <b>{'Açık' if on else 'Kapalı'}</b>\nCeza: <b>{ACTION_LABELS[act]}</b>")
        if act == 'warn':
            text += f" (limit {s.get('warn_limit', 5)} → {WARN_ACTION_LABELS.get(s.get('warn_action', 'ban'))})"
        if act == 'mute' and sub in ESCALATING:
            text += "\nSusturma kademeli: 10 dk → 30 dk → 5 saat"
        if sub == 'badmedia':
            text += _badmedia_text(s)
        page_id = f"pr.{sub}"
        rows.append([toggle_btn("Koruma " + ("açık" if on else "kapalı"), on, f"s|{cid}|t|{skey}|{page_id}")])
        rows.append([ibtn(f"• {lbl} •" if a == act else lbl, f"s|{cid}|a|{sub}|{a}", BLUE if a == act else None)
                     for a, lbl in ACTION_LABELS.items()])
        if sub == 'flood':
            rows += [_num_row(cid, 'flood_limit', s, page_id, "Limit"), _num_row(cid, 'flood_timeframe', s, page_id, "Süre")]
        elif sub == 'media':
            rows += [_num_row(cid, 'media_flood_limit', s, page_id, "Limit"),
                     _num_row(cid, 'media_flood_timeframe', s, page_id, "Süre")]
        if act == 'mute' and sub not in ESCALATING:
            rows.append(_num_row(cid, 'mute_minutes', s, page_id, "Susturma"))
        if sub == 'link':
            rows.append([ibtn("🔗 İzinli linkler ›", f"s|{cid}|p|links.0", BLUE)])
        elif sub == 'word':
            rows.append([ibtn("🔤 Kelime listesi ›", f"s|{cid}|p|words.0", BLUE)])
        elif sub == 'badmedia':
            rows += _badmedia_rows(cid, s, page_id)
        rows.append(_back(cid, 'prot'))

    elif base == 'join':
        text = (f"🚪 <b>Katılım</b> — {title}\n\n"
                "• <b>Captcha</b>: yeni üye grupta matematik sorusunu çözene kadar yazamaz.\n"
                "• <b>Özelden doğrulama</b>: katılım isteği gönderene bot özelden soru sorar; doğru cevaplayan "
                "otomatik kabul edilir. Grupta \"yeni üyeleri onayla\" açık olmalı ve botun davet yetkisi olmalı.")
        rows += [
            [toggle_btn("Captcha (grupta)", s.get('captcha_enabled'), f"s|{cid}|t|captcha_enabled|join")],
            [toggle_btn("Özelden doğrulama", s.get('join_captcha'), f"s|{cid}|t|join_captcha|join")],
            _num_row(cid, 'captcha_timeout', s, 'join', "Süre"),
            [toggle_btn("Oto kabul", s.get('auto_accept'), f"s|{cid}|t|auto_accept|join"),
             toggle_btn("Oto red", s.get('auto_reject'), f"s|{cid}|t|auto_reject|join")],
            [toggle_btn("Bot / kullanıcı adsız isteği reddet", s.get('auto_reject_bot'), f"s|{cid}|t|auto_reject_bot|join")],
            [toggle_btn("Kullanıcı adsız yeni üyeyi sustur", s.get('restrict_no_username'),
                        f"s|{cid}|t|restrict_no_username|join")],
            [ibtn("🚨 Anti-Raid ›", f"s|{cid}|p|raid", BLUE), ibtn("🆕 Yeni üye kısıtı ›", f"s|{cid}|p|newbie", BLUE)],
            _back(cid),
        ]

    elif base == 'raid':
        locked = cid in _raid_locked or bool(s.get('raid_lock'))
        text = (f"🚨 <b>Anti-Raid</b> — {title}\n\nBelirtilen sürede limitten fazla üye katılırsa grup "
                f"{RAID_LOCK_SECONDS // 60} dk kilitlenir, sonra eski izinler geri yüklenir.\n"
                f"Durum: <b>{'🔒 Kilitli' if locked else '🔓 Açık'}</b>")
        rows += [[toggle_btn("Anti-Raid", s.get('anti_raid'), f"s|{cid}|t|anti_raid|raid")],
                 _num_row(cid, 'raid_limit', s, 'raid', "Limit"), _num_row(cid, 'raid_timeframe', s, 'raid', "Süre")]
        if locked:
            rows.append([ibtn("🔓 Kilidi şimdi aç", f"s|{cid}|ru", GREEN)])
        rows.append(_back(cid, 'join'))

    elif base == 'newbie':
        text = (f"🆕 <b>Yeni üye kısıtı</b> — {title}\n\nYeni katılanlar belirtilen süre boyunca link, medya ve "
                "forward gönderemez (mesajı silinir, uyarı gösterilir).")
        rows += [_num_row(cid, 'newbie_minutes', s, 'newbie', "Süre"), _back(cid, 'join')]

    elif base in ('welcome', 'goodbye', 'sched'):
        text, rows = render_rich_page(cid, base, s, title)

    elif base in ('fsub', 'tag'):
        text, rows = render_fsub_tag_page(cid, base, s, title)

    elif base == 'comm':
        text, rows = render_comm_page(cid, s, title)

    elif base == 'warn':
        wa = s.get('warn_action', 'ban')
        text = (f"⚠️ <b>Uyarılar</b> — {title}\n\n\"Uyar\" cezalı korumalar ve /warn uyarı verir; limit dolunca "
                f"seçili ceza uygulanır.\nŞu an: <b>{s.get('warn_limit', 5)}</b> uyarı → <b>{WARN_ACTION_LABELS.get(wa, wa)}</b>")
        rows += [_num_row(cid, 'warn_limit', s, 'warn', "Uyarı limiti"),
                 [ibtn(f"• {lbl} •" if a == wa else lbl, f"s|{cid}|wa|{a}", BLUE if a == wa else None)
                  for a, lbl in WARN_ACTION_LABELS.items()]]
        if wa in ('tempban', 'mute'):
            rows.append(_num_row(cid, 'warn_action_duration', s, 'warn', "Ceza süresi"))
        rows += [_num_row(cid, 'mute_minutes', s, 'warn', "Koruma susturması"), _back(cid)]

    elif base == 'night':
        nm = _nm_data(cid)
        r = nm['restrictions']
        text = (f"🌙 <b>Gece Modu</b> — {title}\n\nDurum: <b>{'Açık' if nm['enabled'] else 'Kapalı'}</b>"
                f"{' (şu an aktif)' if nm['is_active'] else ''}\n"
                f"Saat: <b>{nm['start_hour']:02d}:{nm['start_minute']:02d} – {nm['end_hour']:02d}:{nm['end_minute']:02d}</b> (UTC+3)\n"
                "Seçili izinler bu saatlerde kapatılır, bitince eski izinler geri yüklenir.")
        rows += [[toggle_btn("Gece modu", nm['enabled'], f"s|{cid}|ne")],
                 [ibtn("➖", f"s|{cid}|nh|s|d"), ibtn(f"Başlangıç {nm['start_hour']:02d}:{nm['start_minute']:02d}", f"s|{cid}|p|night"),
                  ibtn("➕", f"s|{cid}|nh|s|u")],
                 [ibtn("➖", f"s|{cid}|nh|e|d"), ibtn(f"Bitiş {nm['end_hour']:02d}:{nm['end_minute']:02d}", f"s|{cid}|p|night"),
                  ibtn("➕", f"s|{cid}|nh|e|u")]]
        for i in range(0, len(NIGHT_RESTRICTIONS), 2):
            rows.append([ibtn(f"{'🚫' if r.get(k) else '✅'} {lbl}", f"s|{cid}|nt|{k}", RED if r.get(k) else GREEN)
                         for k, lbl in NIGHT_RESTRICTIONS[i:i + 2]])
        rows.append(_back(cid))

    elif base == 'links':
        wl = s.get('link_whitelist', [])
        pg, pages, part = _paged(wl, int(sub or 0) if (sub or '0').isdigit() else 0)
        text = (f"🔗 <b>İzinli linkler</b> — {title}\n\nBu alan adları link korumasından muaftır "
                f"(alt alan adları dahil). Toplam: {len(wl)}\nSilmek için dokun.")
        rows += [[ibtn(f"🗑 {d[:40]}", f"s|{cid}|ld|{pg * PAGE_SIZE + i}|{pg}", RED)] for i, d in enumerate(part)]
        rows += _nav_rows(cid, 'links', pg, pages)
        rows += [[ibtn("➕ Ekle", f"s|{cid}|i|link", GREEN)], _back(cid)]

    elif base == 'words' and sub == 'clear':
        text = f"🔤 {title}\n\n<b>Tüm yasaklı kelimeler silinsin mi?</b>"
        rows += [[ibtn("🗑 Evet, hepsini sil", f"s|{cid}|wc", RED), ibtn("↩️ Vazgeç", f"s|{cid}|p|words.0")]]

    elif base == 'words':
        words = s.get('banned_words', [])
        pg, pages, part = _paged(words, int(sub or 0) if (sub or '0').isdigit() else 0)
        text = (f"🔤 <b>Yasaklı kelimeler</b> — {title}\n\nFiltre: <b>{'Açık' if s.get('word_ban_enabled') else 'Kapalı'}</b> · "
                f"Toplam: {len(words)}\nSilmek için dokun.")
        rows.append([toggle_btn("Kelime filtresi", s.get('word_ban_enabled'), f"s|{cid}|t|word_ban_enabled|words.{pg}")])
        rows += [[ibtn(f"🗑 {w[:40]}", f"s|{cid}|wd|{pg * PAGE_SIZE + i}|{pg}", RED)] for i, w in enumerate(part)]
        rows += _nav_rows(cid, 'words', pg, pages)
        rows.append([ibtn("➕ Ekle", f"s|{cid}|i|word", GREEN)] +
                    ([ibtn("🧹 Tümünü sil", f"s|{cid}|p|words.clear", RED)] if words else []))
        rows.append(_back(cid))

    elif base == 'notes':
        with get_db() as conn:
            names = [r['name'] for r in conn.execute("SELECT name FROM notes WHERE chat_id = ? ORDER BY name", (cid,))]
        pg, pages, part = _paged(names, int(sub or 0) if (sub or '0').isdigit() else 0)
        text = (f"📝 <b>Notlar</b> — {title}\n\nGrupta <code>#isim</code> yazınca not gösterilir. Toplam: {len(names)}\n"
                "Silmek için dokun.")
        rows += [[ibtn(f"🗑 #{n}", f"s|{cid}|nd|{n}", RED)] for n in part]
        rows += _nav_rows(cid, 'notes', pg, pages)
        rows += [[ibtn("➕ Not ekle", f"s|{cid}|i|note", GREEN)], _back(cid)]

    elif base == 'filters':
        items = sorted(chat_filters(cid), key=lambda f: f['trigger_norm'])
        pg, pages, part = _paged(items, int(sub or 0) if (sub or '0').isdigit() else 0)
        text = (f"🧩 <b>Filtreler</b> — {title}\n\nGrupta tetikleyici yazılınca bot cevap verir. Toplam: {len(items)}\n"
                "Eklemek için grupta: <code>/filter</code> · Silmek için dokun.")
        rows += [[ibtn(f"🗑 {f['trigger_text'][:40]}{' 🖼' if f['file_id'] else ''}", f"s|{cid}|fd|{f['fid']}|{pg}", RED)]
                 for f in part]
        rows += _nav_rows(cid, 'filters', pg, pages)
        rows.append(_back(cid))

    elif base == 'log':
        log_id = channel.get('log_chat_id')
        text = (f"🧾 <b>Log kanalı</b> — {title}\n\nTüm moderasyon kayıtları buraya gönderilir.\n"
                f"Şu an: <code>{html.escape(str(log_id)) if log_id else 'ayarlanmamış'}</code>\n"
                "Sadece grup sahibi değiştirebilir.")
        rows.append([ibtn("✏️ Log kanalını ayarla", f"s|{cid}|i|log", BLUE)] +
                    ([ibtn("🗑 Kaldır", f"s|{cid}|lx", RED)] if log_id else []))
        rows.append(_back(cid))

    elif base in EXT_PAGES:
        text, rows = await _render_ext_page(cid, base, sub, channel, title)

    else:
        active = [lbl for key, (lbl, skey, _, _) in PROTECTIONS.items() if s.get(skey)]
        if s.get('anti_raid'):
            active.append("🚨 Raid")
        nm = get_nightmod(cid)
        text = (f"⚙️ <b>{html.escape(brand())} — Grup Ayarları</b>\n👥 <b>{title}</b>\n\n"
                f"🛡 Aktif korumalar: {', '.join(active) if active else 'yok'}\n"
                f"🚪 Captcha: {'✅' if s.get('captcha_enabled') else '❌'} · Özelden doğrulama: {'✅' if s.get('join_captcha') else '❌'}\n"
                f"⚠️ Uyarı: {s.get('warn_limit', 5)} → {WARN_ACTION_LABELS.get(s.get('warn_action', 'ban'))}\n"
                f"🌙 Gece modu: {'Açık' if nm and nm['enabled'] else 'Kapalı'}")
        rows = [[ibtn("🛡 Koruma", f"s|{cid}|p|prot", BLUE), ibtn("🚪 Katılım", f"s|{cid}|p|join", BLUE)],
                [ibtn("👋 Karşılama", f"s|{cid}|p|welcome", BLUE), ibtn("⚠️ Uyarılar", f"s|{cid}|p|warn", BLUE)],
                [ibtn("🌙 Gece Modu", f"s|{cid}|p|night", BLUE), ibtn("🔗 Linkler", f"s|{cid}|p|links.0", BLUE)],
                [ibtn("🔤 Kelimeler", f"s|{cid}|p|words.0", BLUE), ibtn("📝 Notlar", f"s|{cid}|p|notes.0", BLUE)],
                [ibtn("🧩 Filtreler", f"s|{cid}|p|filters.0", BLUE), ibtn("⏰ Zamanlı mesaj", f"s|{cid}|p|sched", BLUE)],
                [ibtn("📢 Kanal zorunluluğu", f"s|{cid}|p|fsub", BLUE), ibtn("🏷 Etiket", f"s|{cid}|p|tag", BLUE)],
                [ibtn("🧑‍⚖️ Topluluk koruması", f"s|{cid}|p|comm", BLUE)],
                [ibtn("✏️ Düzenleme & Rapor", f"s|{cid}|p|edit", BLUE), ibtn("🧾 Log Kanalı", f"s|{cid}|p|log", BLUE)],
                [ibtn("🌐 Grup Ağı", f"s|{cid}|p|net", BLUE), ibtn("🛟 Kurtarma", f"s|{cid}|p|rec", BLUE)],
                [ibtn("✖️ Kapat", f"s|{cid}|x", RED)]]
    return text, InlineKeyboardMarkup(rows)

async def send_settings_panel(message, chat_id: str, page: str = 'main'):
    """Ayar panelini yeni mesaj olarak gönderir (kanallar için kanal koruma paneli)."""
    channel = get_channel_settings(chat_id)
    if channel and channel.get('chat_type') == 'channel':
        await message.reply_text(f"Kanal Koruma Ayarlari\n{chat_id}",
                                 reply_markup=_kanal_settings_keyboard(chat_id, get_channel_cfg(chat_id)))
        return
    text, markup = await render_settings(chat_id, page)
    await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def _settings_change(cid: str, channel: dict, op: str, args: list, query, context):
    """Panelde değişiklik yapan işlemler. Dönüş: (açılacak sayfa, kısa bildirim) veya None (cevap verildi)."""
    s = channel['settings']
    uid = query.from_user.id
    by = mention(query.from_user)
    if op == 't' and args:
        key, page = args[0], (args[1] if len(args) > 1 else 'main')
        if key not in TOGGLE_KEYS:
            return page, "Geçersiz ayar"
        s[key] = not s.get(key, key == 'welcome_enabled')
        if key == 'auto_accept' and s[key]:
            s['auto_reject'] = False
        elif key == 'auto_reject' and s[key]:
            s['auto_accept'] = False
        save_channel_settings(cid, channel)
        await send_log(cid, f"⚙️ {html.escape(key)} → {'açıldı' if s[key] else 'kapatıldı'} | {by}", ParseMode.HTML)
        return page, "✅ Açıldı" if s[key] else "❌ Kapatıldı"
    if op == 'n' and len(args) >= 3 and args[0] in NUMERIC:
        key, up, page = args[0], args[1] == 'u', args[2]
        cur = int(s.get(key, _default_channel_settings().get(key, 0)) or 0)
        s[key] = _step_value(key, cur, up)
        save_channel_settings(cid, channel)
        return page, _fmt_numeric(key, s[key])
    if op == 'a' and len(args) == 2 and args[0] in PROTECTIONS and args[1] in ACTION_LABELS:
        s[f'action_{args[0]}'] = args[1]
        save_channel_settings(cid, channel)
        await send_log(cid, f"⚙️ {html.escape(args[0])} cezası → {ACTION_LABELS[args[1]]} | {by}", ParseMode.HTML)
        return f"pr.{args[0]}", f"Ceza: {ACTION_LABELS[args[1]]}"
    if op == 'wa' and args and args[0] in WARN_ACTION_LABELS:
        s['warn_action'] = args[0]
        save_channel_settings(cid, channel)
        return 'warn', f"Limit cezası: {WARN_ACTION_LABELS[args[0]]}"
    if op == 'i' and args and args[0] in INPUT_PROMPTS:
        if args[0] == 'log' and not _can_manage_log(cid, uid, channel):
            await query.answer("Log kanalını sadece grup sahibi değiştirebilir.", show_alert=True)
            return None
        await _ask_input(query, context, cid, args[0])
        return None
    if op in ('wd', 'ld') and len(args) == 2 and args[0].isdigit():
        list_key = 'banned_words' if op == 'wd' else 'link_whitelist'
        items = s.setdefault(list_key, [])
        idx = int(args[0])
        page = f"{'words' if op == 'wd' else 'links'}.{args[1]}"
        if idx >= len(items):
            return page, "Liste değişmiş, yenilendi"
        removed = items.pop(idx)
        save_channel_settings(cid, channel)
        await send_log(cid, f"🗑 {'Yasaklı kelime' if op == 'wd' else 'İzinli link'} silindi: {html.escape(removed)} | {by}",
                       ParseMode.HTML)
        return page, f"🗑 {removed[:40]} silindi"
    if op == 'wc':
        s['banned_words'] = []
        save_channel_settings(cid, channel)
        await send_log(cid, f"🧹 Tüm yasaklı kelimeler silindi | {by}", ParseMode.HTML)
        return 'words.0', "Tüm kelimeler silindi"
    if op == 'fd' and args and args[0].isdigit():
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM chat_filters WHERE chat_id = ? AND rowid = ?", (cid, int(args[0])))
                conn.commit()
        _filter_changed(cid)
        return f"filters.{args[1] if len(args) > 1 and args[1].isdigit() else 0}", "Filtre silindi"
    if op == 'nd' and args:
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM notes WHERE chat_id = ? AND name = ?", (cid, args[0]))
                conn.commit()
        return 'notes.0', f"#{args[0]} silindi"
    if op == 'ne':
        nm = _nm_data(cid)
        nm['enabled'] = 0 if nm['enabled'] else 1
        nm['configured'] = 1
        save_nightmod(cid, nm)
        if not nm['enabled'] and nm['is_active']:
            await nightmod_deactivate(cid)
        return 'night', "🌙 Gece modu " + ("açıldı" if nm['enabled'] else "kapatıldı")
    if op == 'nt' and args and args[0] in dict(NIGHT_RESTRICTIONS):
        nm = _nm_data(cid)
        nm['restrictions'][args[0]] = not nm['restrictions'].get(args[0], False)
        nm['configured'] = 1
        save_nightmod(cid, nm)
        return 'night', "Kaydedildi"
    if op == 'nh' and len(args) == 2:
        nm = _nm_data(cid)
        prefix = 'start' if args[0] == 's' else 'end'
        total = (nm[f'{prefix}_hour'] * 60 + nm[f'{prefix}_minute'] + (30 if args[1] == 'u' else -30)) % 1440
        nm[f'{prefix}_hour'], nm[f'{prefix}_minute'] = divmod(total, 60)
        nm['configured'] = 1
        save_nightmod(cid, nm)
        return 'night', f"{nm[f'{prefix}_hour']:02d}:{nm[f'{prefix}_minute']:02d}"
    if op == 'lx':
        if not _can_manage_log(cid, uid, channel):
            await query.answer("Log kanalını sadece grup sahibi değiştirebilir.", show_alert=True)
            return None
        channel['log_chat_id'] = None
        save_channel_settings(cid, channel)
        return 'log', "Log kanalı kaldırıldı"
    if op == 'rx':
        s['rules'] = ''
        s.pop('rules_rich', None)
        save_channel_settings(cid, channel)
        return 'welcome', "Kurallar silindi"
    if op == 'ru':
        ok = await raid_unlock(cid)
        if ok:
            await send_log(cid, f"✅ Raid kilidi panelden açıldı | {by}", ParseMode.HTML)
        return 'raid', "🔓 Kilit açıldı" if ok else "Kilit açılamadı (bot yetkisi?)"
    if op in EXT_OPS:
        return await _settings_change_ext(cid, channel, op, args, query, context)
    if op in RICH_OPS:
        return await rich_panel_op(cid, channel, op, args, query)
    return 'main', ''

async def settings_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('|')
    if len(parts) < 3:
        await query.answer()
        return
    cid, op, args = parts[1], parts[2], parts[3:]
    channel = get_channel_settings(cid)
    if not channel:
        await query.answer("Grup bulunamadı.", show_alert=True)
        return
    uid = query.from_user.id
    if not has_permission(cid, uid, 50):
        await query.answer("Yetkin yok!", show_alert=True)
        return
    if not await _is_real_chat_admin(cid, uid):
        await query.answer("Bu paneli kullanmak için grupta yönetici olmalısın.", show_alert=True)
        return
    if op != 'p' and not await _is_bot_admin(cid):
        await query.answer("Bot bu grupta yönetici değil veya yetkileri yetersiz.", show_alert=True)
        return
    if op == 'x':
        await query.answer()
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug(f"Panel kapatılamadı: {e}")
        return
    note = ''
    if op == 'p':
        page = args[0] if args else 'main'
    else:
        need = 'can_filters' if op == 'fd' else 'can_manage_settings'
        if not has_specific_permission(cid, uid, need):
            await deny(update, need)
            return
        result = await _settings_change(cid, channel, op, args, query, context)
        if result is None:
            return
        page, note = result
    await query.answer(note[:190])
    text, markup = await render_settings(cid, page)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
    except BadRequest as e:
        if 'not modified' not in str(e).lower():
            raise

async def _ask_input(query, context, cid: str, kind: str):
    """Panelden metin isteme: ForceReply ile cevap alanı açılır, cevap settings_input_handler'a düşer."""
    prompt_text, placeholder = INPUT_PROMPTS[kind]
    prompt = await query.message.reply_text(
        f"{mention(query.from_user)} {html.escape(prompt_text)}\n\n<i>Vazgeçmek için: iptal</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder[:64]))
    context.user_data['await_input'] = {
        'kind': kind, 'chat_id': cid, 'prompt_chat': prompt.chat_id, 'prompt_id': prompt.message_id,
        'panel_id': query.message.message_id, 'expires': time.time() + 300}
    await query.answer("Cevabını açılan mesaja yanıt olarak yaz.")

async def settings_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Panelin istediği metni (ForceReply cevabı) yakalar; diğer mesajlara dokunmaz."""
    st = context.user_data.get('await_input') if context.user_data is not None else None
    msg = update.message
    if not st or not msg or msg.chat_id != st['prompt_chat']:
        return
    if not msg.text and not (st['kind'] in RICH_INPUT_KINDS and (msg.caption or msg_media(msg))):
        return
    if msg.chat.type != 'private' and not (msg.reply_to_message and msg.reply_to_message.message_id == st['prompt_id']):
        return
    context.user_data.pop('await_input', None)
    if msg.text and msg.text in DM_MENU_TEXTS:
        return  # kullanıcı menüye bastı: giriş iptal, menü işleyicisi devam etsin
    try:
        await bot.delete_message(st['prompt_chat'], st['prompt_id'])
    except Exception as e:
        logger.debug(f"Giriş istemi silinemedi: {e}")
    if time.time() > st['expires']:
        await msg.reply_text("⏰ Süre doldu, panelden tekrar dene.")
        raise ApplicationHandlerStop
    if (msg.text or '').strip().lower() in ('iptal', 'vazgeç', 'vazgec'):
        await msg.reply_text("İptal edildi.")
        raise ApplicationHandlerStop
    cid = st['chat_id']
    channel = get_channel_settings(cid)
    if not channel or not _can_edit_settings(cid, update.effective_user.id):
        await msg.reply_text("Yetkin yok!")
        raise ApplicationHandlerStop
    note, page = await _apply_input(st['kind'], cid, channel, msg, update.effective_user)
    await msg.reply_text(note, parse_mode=ParseMode.HTML)
    try:
        text, markup = await render_settings(cid, page)
        await bot.edit_message_text(text, chat_id=st['prompt_chat'], message_id=st['panel_id'], reply_markup=markup,
                                    parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"Panel yenilenemedi: {e}")
    raise ApplicationHandlerStop

async def _apply_input(kind: str, cid: str, channel: dict, msg, user):
    """Metin girişini ayara uygular. Dönüş: (HTML bildirim, panel sayfası)"""
    s = channel['settings']
    text = (msg.text or msg.caption or '').strip()
    if kind in ('welcome', 'goodbye', 'rules'):
        rich = build_rich(msg_html(msg), msg_media(msg))
        if kind == 'rules' and not rich_plain(rich) and not rich.get('m'):
            return "❌ Kurallar boş olamaz.", 'welcome'
        store_rich(s, kind, rich)
        if kind == 'welcome':
            s['welcome_enabled'] = True
        elif kind == 'goodbye':
            s['goodbye_enabled'] = True
        save_channel_settings(cid, channel)
        label = {'welcome': "Hoş geldin mesajı", 'goodbye': "Veda mesajı", 'rules': "Kurallar"}[kind]
        await send_log(cid, f"✏️ {label} güncellendi | {mention(user)}", ParseMode.HTML)
        return f"✅ {label} güncellendi ({html.escape(rich_summary(rich))}). Panelden 👁 Önizle ile bakabilirsin.", \
            'goodbye' if kind == 'goodbye' else 'welcome'
    if kind == 'fsub':
        vals, err = await fsub_set_channel(cid, text)
        if err:
            return f"❌ {err}", 'fsub'
        s.update(vals)
        save_channel_settings(cid, channel)
        await send_log(cid, f"📢 Kanal zorunluluğu: {html.escape(vals['fsub_title'] or '')} | {mention(user)}", ParseMode.HTML)
        return f"✅ Kanal zorunluluğu açık: <b>{html.escape(vals['fsub_title'] or '')}</b>", 'fsub'
    if kind == 'sched':
        first, _, _ = text.partition(' ')
        interval = parse_interval(first)
        err = _sched_check(cid, interval)
        if err:
            return f"❌ {err}", 'sched'
        raw = msg.text or msg.caption or ''
        rich = build_rich(html_after(msg, raw.find(first) + len(first)), msg_media(msg))
        if not rich_plain(rich) and not rich.get('m'):
            return "❌ Süreden sonra mesajı yaz. Örn: <code>6sa Kuralları okuyun!</code>", 'sched'
        sid = sched_add(cid, rich, interval, user.id)
        return f"✅ Zamanlanmış mesaj #{sid}: her {human_duration(interval)} bir.", 'sched'
    if kind == 'word':
        added, bad = [], []
        for w in (line.strip() for line in text.split('\n')):
            if not w:
                continue
            if w.lower().startswith('re:'):
                try:
                    re.compile(w[3:])
                except re.error:
                    bad.append(w)
                    continue
            else:
                w = w.lower()
            if w not in s['banned_words']:
                s['banned_words'].append(w)
                added.append(w)
        if added:
            s['word_ban_enabled'] = True
        save_channel_settings(cid, channel)
        note = f"✅ {len(added)} kelime eklendi, kelime filtresi açık."
        if bad:
            note += f"\n❌ Geçersiz regex: {html.escape(', '.join(bad))}"
        return note, 'words.0'
    if kind == 'link':
        wl = s.setdefault('link_whitelist', [])
        added = []
        for item in re.split(r'[\s,]+', text.lower()):
            item = re.sub(r'^[a-z]+://', '', item)
            item = item[4:] if item.startswith('www.') else item
            if item and item not in wl:
                wl.append(item)
                added.append(item)
        save_channel_settings(cid, channel)
        return f"✅ {len(added)} alan adı eklendi.", 'links.0'
    if kind == 'note':
        name = text.split(maxsplit=1)[0].lower() if text else ''
        parts = msg.text_html.split(maxsplit=1)
        if not _NOTE_NAME.match(name) or len(parts) < 2:
            return "❌ Biçim: <code>isim içerik</code> (isim: harf, rakam, - veya _)", 'notes.0'
        async with _db_lock:
            with get_db() as conn:
                conn.execute("""INSERT OR REPLACE INTO notes (chat_id, name, content, file_id, file_type, created_by, created_at)
                                VALUES (?, ?, ?, NULL, NULL, ?, ?)""", (cid, name, parts[1], user.id, time.time()))
                conn.commit()
        return f"✅ Not kaydedildi: <code>#{html.escape(name)}</code>", 'notes.0'
    if kind == 'log':
        if not _can_manage_log(cid, user.id, channel):
            return "Log kanalını sadece grup sahibi değiştirebilir.", 'log'
        if not re.fullmatch(r'-?\d+', text):
            return "❌ Geçersiz ID. Örnek: <code>-1001234567890</code>", 'log'
        try:
            await bot.send_message(int(text), f"✅ ULUS log kanalı bağlandı: {html.escape(await _chat_title(cid))}",
                                   parse_mode=ParseMode.HTML)
        except Exception as e:
            return f"❌ Bu sohbete mesaj gönderemiyorum. {html.escape(friendly_error(e)[2:])}", 'log'
        channel['log_chat_id'] = text
        save_channel_settings(cid, channel)
        return "✅ Log kanalı ayarlandı.", 'log'
    if kind == 'recovery':
        return await _apply_recovery_input(cid, channel, msg, user)
    return "Bilinmeyen işlem.", 'main'

# ── Panel ekleri: uygunsuz medya, engelli medya listesi, düzenleme & rapor, grup ağı, kurtarma ──
EXT_PAGES = {'bm', 'edit', 'net', 'rec', 'snapc'}
EXT_OPS = {'mk', 'mo', 'bd', 'na', 'nr', 'nb', 'nc', 'ns', 'rd', 'sn', 'rg'}
RICH_OPS = {'pv', 'wr', 'wm', 'zt', 'zp', 'zc', 'zd', 'ts', 'ba'}

def _badmedia_rows(cid: str, s: dict, page_id: str) -> list:
    ai = "🤖 Yapay zeka taraması" + ("" if nsfw_available() else " (kurulu değil)")
    locked = bool(s.get('media_lock'))
    return [
        [toggle_btn(ai, s.get('nsfw_scan', True) and nsfw_available(), f"s|{cid}|t|nsfw_scan|{page_id}")],
        [toggle_btn("📦 Tehlikeli dosyalar (.apk .exe …)", s.get('file_block', True), f"s|{cid}|t|file_block|{page_id}")],
        [toggle_btn("🔒 Saldırıda otomatik medya kilidi", s.get('media_autolock', True),
                    f"s|{cid}|t|media_autolock|{page_id}")],
        _num_row(cid, 'media_lock_minutes', s, page_id, "Kilit süresi"),
        [ibtn("🚫 Engelli medya listesi ›", f"s|{cid}|p|bm.0", BLUE)],
        [ibtn("🔓 Medya kilidini aç", f"s|{cid}|mo", GREEN) if locked else ibtn("🔒 Medyayı şimdi kilitle", f"s|{cid}|mk", RED)],
    ]

def _badmedia_text(s: dict) -> str:
    ai = ("açık" if s.get('nsfw_scan', True) else "kapalı") if nsfw_available() else \
        "kurulu değil (sunucuda <code>pip install nudenet</code>)"
    lock = s.get('media_lock')
    lock_txt = f"\n🔒 Medya kilidi aktif: {datetime.fromtimestamp(lock['until'], TZ_TR):%H:%M}'e kadar" if lock else ""
    return (f"\nYapay zeka taraması: {ai}\nEngellemek için medyaya yanıt ver: <code>/medyaengel</code> · "
            f"sticker paketi: <code>/paketengel</code>{lock_txt}")

async def _render_ext_page(cid: str, base: str, sub: str, channel: dict, title: str):
    s = channel['settings']
    rows: list = []
    if base == 'bm':
        with get_db() as conn:
            items = conn.execute("SELECT rowid, uid, kind, note FROM blocked_media WHERE chat_id = ? ORDER BY added_at DESC",
                                 (cid,)).fetchall()
        pg, pages, part = _paged(list(items), int(sub or 0) if (sub or '0').isdigit() else 0)
        text = (f"🚫 <b>Engelli medya</b> — {title}\n\nAynısı gönderilince silinir. Toplam: {len(items)}\n"
                "Kaldırmak için dokun. Eklemek: medyaya yanıt verip <code>/medyaengel</code>")
        for r in part:
            label = f"📦 {r['uid'][4:]}" if r['kind'] == 'set' else f"🖼 {r['note'] or r['kind']}"
            rows.append([ibtn(f"🗑 {label[:40]}", f"s|{cid}|bd|{r['rowid']}|{pg}", RED)])
        rows += _nav_rows(cid, 'bm', pg, pages)
        rows.append(_back(cid, 'pr.badmedia'))
    elif base == 'edit':
        text = (f"✏️ <b>Düzenleme & 🚩 Rapor</b> — {title}\n\n"
                f"• <b>Geç düzenleme koruması</b>: gönderildikten {s.get('edit_guard_minutes', 5)} dk sonra düzenlenen mesaj "
                "silinir; eski ve yeni hâli grubun kurucusuna ve botu ekleyen kişiye özelden gönderilir "
                "(bot, onların özelden /start yazmış olmasını ister).\n"
                "• <b>Rapor</b>: üyeler bir mesaja yanıt verip <code>/report</code> veya <code>@admin</code> yazar; "
                "yetkililere butonlu bildirim gider.")
        rows += [[toggle_btn("Geç düzenleme koruması", s.get('edit_guard'), f"s|{cid}|t|edit_guard|edit")],
                 _num_row(cid, 'edit_guard_minutes', s, 'edit', "Süre"),
                 [toggle_btn("Kurucuya/ekleyene bildir", s.get('edit_notify', True), f"s|{cid}|t|edit_notify|edit")],
                 [toggle_btn("Rapor sistemi", s.get('reports_enabled', True), f"s|{cid}|t|reports_enabled|edit")],
                 _back(cid)]
    elif base == 'net':
        owner = network_of(cid)
        if owner:
            chats = network_chats(owner)
            names = "\n".join([f"• {html.escape(await _chat_title(c))}" for c in chats[:20]])
            text = (f"🌐 <b>Grup ağı</b> — {title}\n\nBu grup {mention_html(owner, str(owner))} ağında "
                    f"({len(chats)} grup):\n<blockquote expandable>{names}</blockquote>\n"
                    "Ban eşitleme açıkken bir grupta banlanan kişi ağdaki tüm gruplardan banlanır (kick hariç); "
                    "ban kaldırılınca her yerden kalkar.")
            rows += [[toggle_btn("Ban eşitleme", network_ban_sync(owner), f"s|{cid}|nb")],
                     [ibtn("📋 Kelime & link listelerini ağa kopyala", f"s|{cid}|nc", BLUE)],
                     [ibtn("⚙️ Koruma ayarlarını ağa kopyala", f"s|{cid}|ns", BLUE)],
                     [ibtn("➖ Bu grubu ağdan çıkar", f"s|{cid}|nr", RED)]]
        else:
            text = (f"🌐 <b>Grup ağı</b> — {title}\n\nBu grup bir ağda değil. Yönettiğin grupları kendi ağına eklersen "
                    "banlar tüm gruplara yayılır ve ayarlarını tek tıkla kopyalarsın. (Kurucu / yardımcı kurucu gerekir.)")
            rows.append([ibtn("➕ Bu grubu ağıma ekle", f"s|{cid}|na", GREEN)])
        rows.append(_back(cid))
    elif base == 'rec':
        ids = s.get('recovery_ids', [])
        with get_db() as conn:
            snap = conn.execute("SELECT id, admin_count, taken_at FROM admin_snapshots WHERE chat_id = ? "
                                "ORDER BY taken_at DESC LIMIT 1", (cid,)).fetchone()
        last = (f"{datetime.fromtimestamp(snap['taken_at'], TZ_TR):%d.%m %H:%M} ({snap['admin_count']} admin)"
                if snap else "henüz yok")
        text = (f"🛟 <b>Admin kurtarma</b> — {title}\n\nAdmin listesi 6 saatte bir kaydedilir. Biri kısa sürede 3+ adminin "
                "yetkisini alırsa yöneticilere ve güvenilir kişilere kurtarma butonlu uyarı gider. Güvenilir kişiler "
                "bota özelden <code>/kurtar</code> yazarak adminleri geri yükleyebilir.\n"
                f"Son kayıt: {last}\nGüvenilir kişiler ({len(ids)}/3) — kaldırmak için dokun:")
        rows += [[ibtn(f"🗑 {mention_name}", f"s|{cid}|rd|{i}", RED)]
                 for i, mention_name in enumerate(str(u) for u in ids)]
        if len(ids) < 3:
            rows.append([ibtn("➕ Güvenilir kişi ekle", f"s|{cid}|i|recovery", GREEN)])
        rows += [[toggle_btn("Otomatik geri yükle", s.get('recovery_autorestore'), f"s|{cid}|t|recovery_autorestore|rec")],
                 [ibtn("📸 Şimdi kaydet", f"s|{cid}|sn", BLUE)] + ([ibtn("♻️ Geri yükle ›", f"s|{cid}|p|snapc.{snap['id']}")]
                                                                   if snap else []),
                 _back(cid)]
    else:  # snapc.<kayıt>: geri yükleme onayı
        with get_db() as conn:
            snap = conn.execute("SELECT id, data, taken_at FROM admin_snapshots WHERE id = ? AND chat_id = ?",
                                (int(sub) if sub.isdigit() else 0, cid)).fetchone()
        if not snap:
            text = "Kayıt bulunamadı."
        else:
            names = ", ".join(html.escape(a['name']) for a in json.loads(snap['data']) if a['status'] != 'creator')
            text = (f"♻️ {datetime.fromtimestamp(snap['taken_at'], TZ_TR):%d.%m %H:%M} kaydındaki adminler geri yüklenecek:\n"
                    f"<blockquote expandable>{names or '—'}</blockquote>")
            rows.append([ibtn("✅ Evet, geri yükle", f"s|{cid}|rg|{snap['id']}", GREEN), ibtn("❌ Vazgeç", f"s|{cid}|p|rec", RED)])
        rows.append(_back(cid, 'rec'))
    return text, rows

async def _settings_change_ext(cid: str, channel: dict, op: str, args: list, query, context):
    s = channel['settings']
    uid = query.from_user.id
    by = mention(query.from_user)
    if op in ('mk', 'mo'):
        if op == 'mk':
            ok = await media_lock(cid, int(s.get('media_lock_minutes', 30) or 30), f"panel: {query.from_user.first_name}")
            return 'pr.badmedia', "🔒 Medya kilitlendi" if ok else "Kilitlenemedi (raid kilidi veya bot yetkisi)"
        return 'pr.badmedia', "🔓 Medya kilidi açıldı" if await media_unlock(cid) else "Kilit zaten açık"
    if op == 'bd' and args and args[0].isdigit():
        unblock_media(cid, int(args[0]))
        return f"bm.{args[1] if len(args) > 1 else 0}", "Engel kaldırıldı"
    if op in ('na', 'nr', 'nb', 'nc', 'ns'):
        owner = network_of(cid)
        if op == 'na':
            if not has_permission(cid, uid, LVL_KURUCU):
                await query.answer("Ağa eklemek için bu grubun kurucusu olmalısın.", show_alert=True)
                return None
            network_add(cid, uid)
            await send_log(cid, f"🌐 Grup {by} ağına eklendi", ParseMode.HTML)
            return 'net', "Ağa eklendi"
        if not owner:
            return 'net', "Bu grup bir ağda değil"
        if op == 'nr':
            if uid != owner and not has_permission(cid, uid, LVL_KURUCU):
                await query.answer("Yetkin yok!", show_alert=True)
                return None
            network_remove(cid)
            return 'net', "Ağdan çıkarıldı"
        if uid != owner:
            await query.answer("Bu işlemi sadece ağın sahibi yapabilir.", show_alert=True)
            return None
        if op == 'nb':
            with get_db() as conn:
                conn.execute("UPDATE networks SET ban_sync = 1 - ban_sync WHERE owner_id = ?", (owner,))
                conn.commit()
            return 'net', "Ban eşitleme " + ("açık" if network_ban_sync(owner) else "kapalı")
        n = network_copy(cid, owner, lists=(op == 'nc'))
        await send_log(cid, f"🌐 Ayarlar ağdaki {n} gruba kopyalandı | {by}", ParseMode.HTML)
        return 'net', f"{n} gruba kopyalandı"
    # kurtarma işlemleri: grup sahibi veya kurucu/yardımcı kurucu
    if not has_permission(cid, uid, LVL_KURUCU):
        await query.answer("Kurtarma ayarlarını sadece kurucu değiştirebilir.", show_alert=True)
        return None
    if op == 'rd' and args and args[0].isdigit():
        ids = s.setdefault('recovery_ids', [])
        if int(args[0]) < len(ids):
            ids.pop(int(args[0]))
            save_channel_settings(cid, channel)
        return 'rec', "Kaldırıldı"
    if op == 'sn':
        sid = await take_admin_snapshot(cid, "manuel")
        return 'rec', "📸 Kaydedildi" if sid else "Kaydedilemedi (bot admin mi?)"
    if op == 'rg' and args and args[0].isdigit():
        ok, failed = await restore_snapshot(cid, int(args[0]))
        return 'rec', f"♻️ {ok} admin geri yüklendi" + (f", {len(failed)} başarısız" if failed else "")
    return 'main', ''

async def _apply_recovery_input(cid: str, channel: dict, msg, user):
    if not has_permission(cid, user.id, LVL_KURUCU):
        return "Güvenilir kişiyi sadece kurucu ekleyebilir.", 'rec'
    ref = msg.text.strip().lstrip('@')
    target = int(ref) if ref.lstrip('-').isdigit() and valid_id(int(ref)) else None
    if target is None:
        with get_db() as conn:
            row = conn.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1", (ref,)).fetchone()
        target = row['user_id'] if row else None
    if not target or target < 0:
        return "❌ Kişi bulunamadı. Kullanıcı ID'sini yaz (kişi bota /id yazarak öğrenebilir).", 'rec'
    ids = channel['settings'].setdefault('recovery_ids', [])
    if target not in ids:
        if len(ids) >= 3:
            return "En fazla 3 güvenilir kişi eklenebilir.", 'rec'
        ids.append(target)
        save_channel_settings(cid, channel)
    return f"✅ {mention_html(target, str(target))} güvenilir kişi olarak eklendi. Bota özelden /start yazmış olmalı.", 'rec'



async def cmd_settings(update: Update, context):
    """/settings — butonlu ayar paneli (grupta o grup, özelde /kanal ile seçilen grup)."""
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.effective_message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.effective_message.reply_text("Yetkin yok!")
        return
    await send_settings_panel(update.effective_message, chat_id)



async def help_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help menüsündeki '⚙️ Ayarlar Paneli' butonu."""
    query = update.callback_query
    chat = query.message.chat if query.message else None
    if not chat or chat.type == 'private':
        chat_id = context.user_data.get('selected_channel')
    else:
        chat_id = str(chat.id)
    channel = get_channel_settings(chat_id) if chat_id else None
    if not channel:
        await query.answer("Önce /kanal ile bir grup seç!", show_alert=True)
        return
    if not has_permission(chat_id, query.from_user.id, 50):
        await query.answer("Yetkin yok!", show_alert=True)
        return
    await query.answer()
    await send_settings_panel(query.message, chat_id)

async def duyuru(update: Update, context):
    if not is_bot_owner(update.effective_user.id):
        await update.message.reply_text("Bu komut sadece botun sahibi tarafından kullanılabilir!")
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /duyuru <mesaj>")
        return
    parts = (update.message.text or '').split(maxsplit=1)
    duyuru_msg = parts[1] if len(parts) > 1 else ' '.join(context.args)  # satır sonları korunur
    sent_count = 0
    failed_count = 0
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM channels WHERE mybot(bot_id)").fetchall()
    for row in rows:
        try:
            await bot.send_message(row['chat_id'], duyuru_msg)
            sent_count += 1
        except Exception:
            failed_count += 1
    await update.message.reply_text(f"Duyuru gönderildi!\nBaşarılı: {sent_count}\nBaşarısız: {failed_count}")

def upsert_user(chat_id: str, user):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, chat_id, username, first_name, last_name, is_bot, joined_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                last_seen  = excluded.last_seen,
                left_at    = NULL
        """, (
            user.id, chat_id,
            user.username or '',
            user.first_name or '',
            getattr(user, 'last_name', '') or '',
            int(user.is_bot),
            time.time(), time.time()
        ))
        conn.commit()

async def new_member_handler(update: Update, context):
    if not update.message or not update.message.new_chat_members:
        return
    chat_id = str(update.message.chat_id)

    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            return

    channel = get_channel_settings(chat_id)
    if not channel:
        return

    adder = update.message.from_user
    adder_is_staff = bool(adder) and await is_staff_user(chat_id, adder.id, channel)
    to_welcome = []

    for user in update.message.new_chat_members:
        if not user.is_bot:
            upsert_user(chat_id, user)
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("INSERT OR REPLACE INTO newcomers (chat_id, user_id, joined_at) VALUES (?, ?, ?)",
                                 (chat_id, user.id, time.time()))
                    conn.commit()
        username = user.username or user.first_name

        if is_gbanned(user.id, chat_id) and user.id != FOUNDER_ID:
            try:
                await bot.ban_chat_member(chat_id, user.id)
                await send_log(chat_id, f"🌐 Global banlı kullanıcı katıldı ve banlandı: {mention(user)}", ParseMode.HTML)
            except Exception as e:
                logger.debug(f"Gban uygulanamadı: {e}")
            continue
        if not user.is_bot:
            log_member_event(chat_id, user.id, 'join')
            await track_name(chat_id, user, channel['settings'])
            if not (adder_is_staff and adder.id != user.id) and await blacklist_on_join(chat_id, user, channel):
                continue

        is_raider = await anti_raid_check(chat_id, user.id, context)
        if is_raider:
            try:
                await bot.restrict_chat_member(
                    chat_id, user.id,
                    permissions=ChatPermissions(can_send_messages=False)
                )
            except Exception as e:
                logger.debug(f"new_member_handler: {e}")
            continue

        added_by_staff = adder_is_staff and adder.id != user.id  # yetkilinin elle eklediği kişi doğrulanmış sayılır
        if channel['settings'].get('captcha_enabled', False) and not user.is_bot and not added_by_staff \
                and not _consume_join_pass(chat_id, user.id):
            await send_captcha(chat_id, user.id, username, context, user=user)
            continue

        channel['stats']['joins'] = channel['stats'].get('joins', 0) + 1
        save_channel_settings(chat_id, channel)

        suspicious = (user.is_bot and not adder_is_staff) or (
            not user.is_bot and not user.username and channel['settings'].get('restrict_no_username', False))
        if suspicious:
            try:
                await bot.restrict_chat_member(
                    chat_id, user.id,
                    permissions=ChatPermissions(can_send_messages=False)
                )
                await send_log(chat_id, f"🤖 Şüpheli hesap kısıtlandı: {mention(user)} → {chat_id}", ParseMode.HTML)
            except Exception as e:
                logger.debug(f"new_member_handler: {e}")
        else:
            if not user.is_bot:
                to_welcome.append(user)
            await send_log(chat_id, f"👋 {mention(user)} katıldı → {chat_id}", ParseMode.HTML)

    if to_welcome and channel['settings'].get('welcome_enabled', True):
        try:
            await queue_welcome(chat_id, to_welcome, update.message, context)
        except Exception as e:
            logger.debug(f"Hoş geldin gönderilemedi {chat_id}: {e}")
    await track_invite(update, context)

async def track_invite(update: Update, context):
    chat_id = str(update.message.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    inviter = update.message.from_user
    if not inviter:
        return
    inviter_id = inviter.id
    is_staff = inviter_id == channel['owner'] or has_permission(chat_id, inviter_id, 50)
    if is_staff:
        invites = channel.get('invites', {})
        str_inviter_id = str(inviter_id)
        invites[str_inviter_id] = invites.get(str_inviter_id, 0) + 1
        channel['invites'] = invites
        save_channel_settings(chat_id, channel)

def _get_msg_type(message) -> str:
    
    if message.sticker:
        return 'sticker'
    elif message.animation:
        return 'gif'
    elif message.photo:
        return 'photo'
    elif message.video or message.video_note:
        return 'video'
    elif message.voice:
        return 'voice'
    elif message.audio:
        return 'audio'
    elif message.document:
        return 'document'
    elif message.text:
        import unicodedata
        txt = message.text.strip()
        all_emoji = all(
            unicodedata.category(c) in ('So', 'Sm', 'Cs') or c in (' ', '‍', '️')
            for c in txt
        ) if txt else False
        return 'emoji' if all_emoji else 'text'
    return 'text'

async def track_message(update: Update, context):
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    user = update.effective_user
    if user.is_bot:
        return
    chat_id = str(update.effective_chat.id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return

    upsert_user(chat_id, user)
    await track_name(chat_id, user, channel['settings'])

    msg_type = _get_msg_type(msg)
    username = user.username or ''
    first_name = user.first_name or ''

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO message_stats (chat_id, user_id, username, first_name, msg_type, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, user.id, username, first_name, msg_type, time.time())
            )
            conn.commit()

TZ_OFFSET = 3 * 3600

def _now_utc3() -> datetime:
    
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)

def _utc3_day_range(d: datetime):
    
    day_start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    start_ts = (day_start - timedelta(hours=3)).replace(tzinfo=timezone.utc).timestamp()
    end_ts = (day_end - timedelta(hours=3)).replace(tzinfo=timezone.utc).timestamp()
    return start_ts, end_ts

def _get_period_start(period: str) -> float:
    start_ts, _ = _get_period_range(period)
    return start_ts

def _get_period_range(period: str):
    
    now3 = _now_utc3()
    if period == 'gunluk':
        start3 = now3.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'haftalik':
        start3 = now3 - timedelta(days=now3.weekday())
        start3 = start3.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'aylik':
        start3 = now3.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return 0.0, time.time() + 1
    start_utc = (start3 - timedelta(hours=3)).replace(tzinfo=timezone.utc).timestamp()  # sunucu saat diliminden bağımsız
    return start_utc, time.time() + 1

def _get_leaderboard(chat_id: str, period: str, limit: int = 15,
                     since_ts: float = None, until_ts: float = None):
    """
    Liderlik tablosunu getir.
    since_ts / until_ts verilirse onları kullan (otomatik duyuru için),
    yoksa period'dan hesapla.
    """
    if since_ts is None or until_ts is None:
        since_ts, until_ts = _get_period_range(period)

    with get_db() as conn:
        if since_ts > 0:
            rows = conn.execute("""
                SELECT ms.user_id,
                       COALESCE(u.username, ms.username, '') as username,
                       COALESCE(u.first_name, ms.first_name, '') as first_name,
                       COUNT(*) as cnt
                FROM message_stats ms
                LEFT JOIN users u ON u.user_id = ms.user_id AND u.chat_id = ms.chat_id
                WHERE ms.chat_id = ? AND ms.sent_at >= ? AND ms.sent_at < ?
                GROUP BY ms.user_id
                ORDER BY cnt DESC
                LIMIT ?
            """, (chat_id, since_ts, until_ts, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT ms.user_id,
                       COALESCE(u.username, ms.username, '') as username,
                       COALESCE(u.first_name, ms.first_name, '') as first_name,
                       COUNT(*) as cnt
                FROM message_stats ms
                LEFT JOIN users u ON u.user_id = ms.user_id AND u.chat_id = ms.chat_id
                WHERE ms.chat_id = ?
                GROUP BY ms.user_id
                ORDER BY cnt DESC
                LIMIT ?
            """, (chat_id, limit)).fetchall()
    return rows

def _get_user_count(chat_id: str, user_id: int, period: str,
                    since_ts: float = None, until_ts: float = None) -> int:
    if since_ts is None or until_ts is None:
        since_ts, until_ts = _get_period_range(period)
    with get_db() as conn:
        if since_ts > 0:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM message_stats WHERE chat_id = ? AND user_id = ? AND sent_at >= ? AND sent_at < ?",
                (chat_id, user_id, since_ts, until_ts)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM message_stats WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id)
            ).fetchone()
    return row['cnt'] if row else 0

def _format_name(row) -> str:
    
    name = (row['first_name'] or '').strip()
    if name:
        return name
    return f"ID:{row['user_id']}"

def _period_label(period: str) -> str:
    return {'gunluk': 'BUGÜN', 'haftalik': 'BU HAFTA', 'aylik': 'BU AY', 'toplam': 'TÜM ZAMANLAR'}[period]

def _build_leaderboard_text(chat_id: str, period: str, title: str,
                             footer: str,
                             since_ts: float = None, until_ts: float = None,
                             caller_id: int = None,
                             caller_name: str = None) -> str:
    """Sıralama metnini oluştur — hem komut hem otomatik duyuru için"""
    if since_ts is None or until_ts is None:
        since_ts, until_ts = _get_period_range(period)

    rows = _get_leaderboard(chat_id, period, limit=15,
                            since_ts=since_ts, until_ts=until_ts)
    if not rows:
        return ""

    with get_db() as conn:
        if since_ts > 0:
            total_active = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM message_stats WHERE chat_id = ? AND sent_at >= ? AND sent_at < ?",
                (chat_id, since_ts, until_ts)
            ).fetchone()[0]
            total_msgs = conn.execute(
                "SELECT COUNT(*) FROM message_stats WHERE chat_id = ? AND sent_at >= ? AND sent_at < ?",
                (chat_id, since_ts, until_ts)
            ).fetchone()[0]
        else:
            total_active = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM message_stats WHERE chat_id = ?",
                (chat_id,)
            ).fetchone()[0]
            total_msgs = conn.execute(
                "SELECT COUNT(*) FROM message_stats WHERE chat_id = ?",
                (chat_id,)
            ).fetchone()[0]

    lines = [title, "", "Kullanıcı → Mesaj"]
    for i, row in enumerate(rows, 1):
        name = _format_name(row)
        lines.append(f"{i}. {name} : {row['cnt']} ")

    lines.append("")
    lines.append(footer)
    lines.append(f"├ Toplam aktif kullanıcı: {total_active}")
    lines.append(f"└ Toplam mesaj: {total_msgs}")

    if caller_id is not None:
        cnt = _get_user_count(chat_id, caller_id, period,
                              since_ts=since_ts, until_ts=until_ts)
        name = caller_name or str(caller_id)
        lines.append(f"\nSenin {name} : {cnt}")

    return "\n".join(lines)

async def _send_leaderboard(update: Update, context, period: str):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Bu grup kayıtlı değil! Özelden kullanıyorsan önce /kanal ile grup seç.")
        return

    label_map = {
        'gunluk': ('Grubunuzda Günlük en çok aktif olan 15 kişi:', '📊 Bu Sıralama geçtiğimiz Güne aittir.'),
        'haftalik': ('Grubunuzda Haftalık en çok aktif olan 15 kişi:', '📊 Bu Sıralama geçtiğimiz Haftaya aittir.'),
        'aylik': ('Grubunuzda Aylık en çok aktif olan 15 kişi:', '📊 Bu Sıralama bu Aya aittir.'),
        'toplam': ('Grubunuzda Tüm Zamanların en çok aktif 15 kişisi:', '📊 Tüm zamanlar sıralaması.'),
    }
    title, footer = label_map.get(period, ('Sıralama', ''))

    caller_id = update.effective_user.id
    caller_name = update.effective_user.first_name or update.effective_user.username or str(caller_id)

    text = _build_leaderboard_text(
        chat_id, period, title, footer,
        caller_id=caller_id, caller_name=caller_name
    )
    if not text:
        await update.message.reply_text("Henüz mesaj istatistiği yok!")
        return

    await update.message.reply_text(text)

async def _auto_announce_daily(context):
    
    now3 = _now_utc3()
    yesterday3 = now3 - timedelta(days=1)
    since_ts, until_ts = _utc3_day_range(yesterday3)

    title = "Grubunuzda Günlük en çok aktif olan 15 kişi:"
    footer = "📊 Bu Sıralama geçtiğimiz Güne aittir."

    with get_db() as conn:
        chats = conn.execute(
            "SELECT chat_id FROM channels WHERE chat_type IN ('group', 'supergroup')"
        ).fetchall()

    for row in chats:
        chat_id = row['chat_id']
        try:
            text = _build_leaderboard_text(
                chat_id, 'gunluk', title, footer,
                since_ts=since_ts, until_ts=until_ts
            )
            if text:
                await context.bot.send_message(chat_id, text)
        except Exception as e:
            logger.warning(f"Günlük duyuru hatası {chat_id}: {e}")

async def _auto_announce_weekly(context):
    
    now3 = _now_utc3()
    this_monday3 = now3 - timedelta(days=now3.weekday())
    this_monday3 = this_monday3.replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday3 = this_monday3 - timedelta(days=7)

    since_ts = (last_monday3 - timedelta(hours=3)).replace(tzinfo=timezone.utc).timestamp()
    until_ts = (this_monday3 - timedelta(hours=3)).replace(tzinfo=timezone.utc).timestamp()

    title = "Grubunuzda Haftalık en çok aktif olan 15 kişi:"
    footer = "📊 Bu Sıralama geçtiğimiz Haftaya aittir."

    with get_db() as conn:
        chats = conn.execute(
            "SELECT chat_id FROM channels WHERE chat_type IN ('group', 'supergroup')"
        ).fetchall()

    for row in chats:
        chat_id = row['chat_id']
        try:
            text = _build_leaderboard_text(
                chat_id, 'haftalik', title, footer,
                since_ts=since_ts, until_ts=until_ts
            )
            if text:
                await context.bot.send_message(chat_id, text)
        except Exception as e:
            logger.warning(f"Haftalık duyuru hatası {chat_id}: {e}")

async def run_daily_scheduler(context):
    
    await _auto_announce_daily(context)
    now3 = _now_utc3()
    if now3.weekday() == 0:
        await _auto_announce_weekly(context)

async def cmd_gunluk(update: Update, context):
    await _send_leaderboard(update, context, 'gunluk')

async def cmd_haftalik(update: Update, context):
    await _send_leaderboard(update, context, 'haftalik')

async def cmd_aylik(update: Update, context):
    await _send_leaderboard(update, context, 'aylik')

async def cmd_toplam(update: Update, context):
    await _send_leaderboard(update, context, 'toplam')

async def cmd_top(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Bu grup kayıtlı değil! Özelden kullanıyorsan önce /kanal ile grup seç.")
        return
    keyboard = [
        [
            ibtn("📅 Günlük", f"top|{chat_id}|gunluk", BLUE),
            ibtn("📅 Haftalık", f"top|{chat_id}|haftalik", BLUE),
            ibtn("📅 Aylık", f"top|{chat_id}|aylik", BLUE),
        ],
        [ibtn("📊 Bütün zamanlarda", f"top|{chat_id}|toplam", BLUE)],
        [
            InlineKeyboardButton("📋 Detaylı bilgi", callback_data=f"topdetay|{chat_id}"),
            InlineKeyboardButton("🌐 Global", callback_data=f"topglobal|{update.effective_user.id}"),
        ],
    ]
    caller = update.effective_user.username or update.effective_user.first_name
    await update.message.reply_text(
        f"👥 Bulunduğunuz grup için sıralama türünü seçiniz.\n\n"
        f"Bu menü {caller} tarafından açıldı.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def top_callback(update: Update, context):
    
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("top|"):
        _, chat_id, period = data.split("|")
        rows = _get_leaderboard(chat_id, period)
        label = _period_label(period)
        if not rows:
            await query.message.edit_text("Henüz mesaj istatistiği yok!")
            return
        lines = [f"👥 Grubunuzdaki {label} en çok aktif olanlar:\n\nKullanıcı → Mesaj"]
        for i, row in enumerate(rows, 1):
            name = _format_name(row)
            lines.append(f"▫️{i}. {name} : {row['cnt']}")
        caller_id = query.from_user.id
        caller_cnt = _get_user_count(chat_id, caller_id, period)
        caller_name = query.from_user.username or query.from_user.first_name
        lines.append(f"\nSenin {caller_name} : {caller_cnt}")
        keyboard = [[InlineKeyboardButton("🔙 Geri", callback_data=f"topback|{chat_id}")]]
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("topback|"):
        chat_id = data.split("|")[1]
        keyboard = [
            [
                ibtn("📅 Günlük", f"top|{chat_id}|gunluk", BLUE),
                ibtn("📅 Haftalık", f"top|{chat_id}|haftalik", BLUE),
                ibtn("📅 Aylık", f"top|{chat_id}|aylik", BLUE),
            ],
            [ibtn("📊 Bütün zamanlarda", f"top|{chat_id}|toplam", BLUE)],
            [
                InlineKeyboardButton("📋 Detaylı bilgi", callback_data=f"topdetay|{chat_id}"),
                InlineKeyboardButton("🌐 Global", callback_data=f"topglobal|{query.from_user.id}"),
            ],
        ]
        caller = query.from_user.username or query.from_user.first_name
        await query.message.edit_text(
            f"👥 Bulunduğunuz grup için sıralama türünü seçiniz.\n\nBu menü {caller} tarafından açıldı.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("topdetay|"):
        chat_id = data.split("|")[1]
        with get_db() as conn:
            def active_users(since):
                if since > 0:
                    r = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats WHERE chat_id = ? AND sent_at >= ?", (chat_id, since)).fetchone()
                else:
                    r = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats WHERE chat_id = ?", (chat_id,)).fetchone()
                return r['c'] if r else 0

            def total_msgs(since):
                if since > 0:
                    r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id = ? AND sent_at >= ?", (chat_id, since)).fetchone()
                else:
                    r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id = ?", (chat_id,)).fetchone()
                return r['c'] if r else 0

            def type_count(mtype):
                r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id = ? AND msg_type = ?", (chat_id, mtype)).fetchone()
                return r['c'] if r else 0

            g = _get_period_start('gunluk')
            h = _get_period_start('haftalik')
            a = _get_period_start('aylik')

            text = (
                f"Bot grubunuzda yetkili olduğundan beri grubunuzun çeşitli etkileşimleri:\n\n"
                f"👥 Aktif kullanıcı:\n"
                f"┌📆 Günlük: {active_users(g)}\n"
                f"├📆 Haftalık: {active_users(h)}\n"
                f"├📆 Aylık: {active_users(a)}\n"
                f"└Total: {active_users(0)}\n\n"
                f"💬 Toplam mesaj:\n"
                f"┌📆 Günlük: {total_msgs(g)}\n"
                f"├📆 Haftalık: {total_msgs(h)}\n"
                f"├📆 Aylık: {total_msgs(a)}\n"
                f"└Total: {total_msgs(0)}\n\n"
                f"📊 Toplam çeşitli etkileşim:\n"
                f"┌🃏 Çıkartma: {type_count('sticker')}\n"
                f"├🀄️ Gif: {type_count('gif')}\n"
                f"├🙃 Emoji: {type_count('emoji')}\n"
                f"├📷 Fotoğraf: {type_count('photo')}\n"
                f"├🎥 Video: {type_count('video')}\n"
                f"├💾 Dosya: {type_count('document')}\n"
                f"├🎙 Ses kaydı: {type_count('voice')}\n"
                f"└📼 Müzik: {type_count('audio')}\n\n"
                f"Belirli bir kullanıcı için /info @kullanici veya mesaja reply vererek bilgi alabilirsiniz."
            )
        keyboard = [[InlineKeyboardButton("🔙 Geri", callback_data=f"topback|{chat_id}")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("topglobal|"):
        user_id = int(data.split("|")[1])
        with get_db() as conn:
            chat_rows = conn.execute("SELECT DISTINCT chat_id FROM message_stats WHERE user_id = ?", (user_id,)).fetchall()
            chat_count = len(chat_rows)

            def global_msgs(since):
                if since > 0:
                    r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE user_id = ? AND sent_at >= ?", (user_id, since)).fetchone()
                else:
                    r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE user_id = ?", (user_id,)).fetchone()
                return r['c'] if r else 0

            def global_type(mtype):
                r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE user_id = ? AND msg_type = ?", (user_id, mtype)).fetchone()
                return r['c'] if r else 0

            urow = conn.execute("SELECT username, first_name FROM users WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
            if not urow:
                urow = conn.execute("SELECT username, first_name FROM message_stats WHERE user_id = ? ORDER BY sent_at DESC LIMIT 1", (user_id,)).fetchone()

            g = _get_period_start('gunluk')
            h = _get_period_start('haftalik')
            a = _get_period_start('aylik')

            uname = (urow['username'] if urow and urow['username'] else '') or (urow['first_name'] if urow else str(user_id))
            at_uname = f"@{urow['username']}" if urow and urow['username'] else '-'
            fname = urow['first_name'] if urow else '-'

            text = (
                f"🆔 ID: {user_id}\n"
                f"👱 İsim: {fname}\n"
                f"🌐 Kullanıcı adı: {at_uname}\n"
                f"👥 Toplam Bulunduğun grup sayısı: {chat_count}\n\n"
                f"💬 Bulunduğun gruplarda toplam mesaj:\n"
                f"      ├📆 Günlük: {global_msgs(g)}\n"
                f"      ├📆 Haftalık: {global_msgs(h)}\n"
                f"      ├📆 Aylık: {global_msgs(a)}\n"
                f"      └Total: {global_msgs(0)}\n\n"
                f"🔍 Bulunduğun gruplarda toplam bilgi:\n"
                f"      ├🃏 Çıkartma: {global_type('sticker')}\n"
                f"      ├🀄️ Gif: {global_type('gif')}\n"
                f"      ├🙃 Emoji: {global_type('emoji')}\n"
                f"      ├📷 Fotoğraf: {global_type('photo')}\n"
                f"      ├🎥 Video: {global_type('video')}\n"
                f"      ├💾 Dosya: {global_type('document')}\n"
                f"      ├🎙 Ses kaydı: {global_type('voice')}\n"
                f"      └📼 Müzik: {global_type('audio')}\n"
            )
            keyboard = [[InlineKeyboardButton("🔙 Geri", callback_data=f"topback|{query.message.chat_id}")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_info(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Bu grup kayıtlı değil! Özelden kullanıyorsan önce /kanal ile grup seç.")
        return

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    elif context.args:
        ref = context.args[0].lstrip('@')
        target_id, member = await resolve_user(chat_id, ref)
        if not target_id:
            await update.message.reply_text("Kullanıcı bulunamadı!")
            return
        target = member.user
    else:
        target = update.effective_user
    user_id = target.id

    g = _get_period_start('gunluk')
    h = _get_period_start('haftalik')
    a = _get_period_start('aylik')
    base = "SELECT COUNT(*) AS c FROM message_stats WHERE chat_id = ? AND user_id = ?"
    with get_db() as conn:
        def count(extra: str = "", *params) -> int:
            return conn.execute(base + extra, (chat_id, user_id, *params)).fetchone()['c']
        msgs = {k: count(" AND sent_at >= ?", since) for k, since in (('g', g), ('h', h), ('a', a))}
        msgs['t'] = count()
        types = {t: count(" AND msg_type = ?", t)
                 for t in ('sticker', 'gif', 'emoji', 'photo', 'video', 'document', 'voice', 'audio')}
        rank_row = conn.execute("""
            SELECT COUNT(*) + 1 as rank FROM (
                SELECT user_id, COUNT(*) as cnt FROM message_stats WHERE chat_id = ? GROUP BY user_id
            ) WHERE cnt > (SELECT COUNT(*) FROM message_stats WHERE chat_id = ? AND user_id = ?)
        """, (chat_id, chat_id, user_id)).fetchone()
    rank = rank_row['rank'] if rank_row else '-'

    text = (
        f"📊 {mention(target)} istatistikleri:\n\n"
        f"💬 Mesaj sayısı:\n"
        f"┌📆 Günlük: {msgs['g']}\n"
        f"├📆 Haftalık: {msgs['h']}\n"
        f"├📆 Aylık: {msgs['a']}\n"
        f"└Total: {msgs['t']}\n\n"
        f"📊 Etkileşim detayı:\n"
        f"┌🃏 Çıkartma: {types['sticker']}\n"
        f"├🀄️ Gif: {types['gif']}\n"
        f"├🙃 Emoji: {types['emoji']}\n"
        f"├📷 Fotoğraf: {types['photo']}\n"
        f"├🎥 Video: {types['video']}\n"
        f"├💾 Dosya: {types['document']}\n"
        f"├🎙 Ses kaydı: {types['voice']}\n"
        f"└📼 Müzik: {types['audio']}\n\n"
        f"🏆 Genel sıralama: #{rank}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

def get_channel_cfg(chat_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM channel_settings WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row:
        return {
            'chat_id': chat_id,
            'admin_spam_enabled': 0, 'admin_spam_limit': 10, 'admin_spam_window': 300, 'admin_spam_action': 'demote',
            'admin_media_enabled': 0, 'admin_media_limit': 10, 'admin_media_window': 300, 'admin_media_action': 'demote_ban',
            'link_protection': 0, 'clone_protection': 0, 'bot_add_protection': 0,
            'bulk_ban_protection': 0, 'bulk_ban_limit': 5, 'bulk_ban_window': 300,
            'safe_admins': '[]', 'channel_title': None, 'channel_description': None,
            'lockdown_mode': 0, 'lockdown_saved_admins': '[]', 'weekly_log_day': 0
        }
    return dict(row)

def save_channel_cfg(chat_id: str, cfg: dict):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO channel_settings
            (chat_id, admin_spam_enabled, admin_spam_limit, admin_spam_window, admin_spam_action,
             admin_media_enabled, admin_media_limit, admin_media_window, admin_media_action,
             link_protection, clone_protection, bot_add_protection,
             bulk_ban_protection, bulk_ban_limit, bulk_ban_window,
             safe_admins, channel_title, channel_description,
             lockdown_mode, lockdown_saved_admins, weekly_log_day)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            chat_id,
            cfg.get('admin_spam_enabled', 0), cfg.get('admin_spam_limit', 10),
            cfg.get('admin_spam_window', 300), cfg.get('admin_spam_action', 'demote'),
            cfg.get('admin_media_enabled', 0), cfg.get('admin_media_limit', 10),
            cfg.get('admin_media_window', 300), cfg.get('admin_media_action', 'demote_ban'),
            cfg.get('link_protection', 0), cfg.get('clone_protection', 0),
            cfg.get('bot_add_protection', 0), cfg.get('bulk_ban_protection', 0),
            cfg.get('bulk_ban_limit', 5), cfg.get('bulk_ban_window', 300),
            cfg.get('safe_admins', '[]'), cfg.get('channel_title'),
            cfg.get('channel_description'), cfg.get('lockdown_mode', 0),
            cfg.get('lockdown_saved_admins', '[]'), cfg.get('weekly_log_day', 0)
        ))
        conn.commit()

async def log_channel_action(chat_id: str, action: str, user_id: int = 0, username: str = '', detail: str = ''):
    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO channel_log (chat_id, action, user_id, username, detail, timestamp) VALUES (?,?,?,?,?,?)",
                (chat_id, action, user_id, username, detail, time.time())
            )
            conn.commit()

def get_channel_managers(chat_id: str) -> list:
    channel = get_channel_settings(chat_id)
    managers = []
    if channel:
        managers.append(channel['owner'])
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id FROM roles WHERE chat_id = ? AND role IN ('kurucu','yardimci_kurucu')",
            (chat_id,)
        ).fetchall()
        for r in row:
            if r['user_id'] not in managers:
                managers.append(r['user_id'])
    return managers

async def notify_managers(chat_id: str, text: str, reply_markup=None, parse_mode: str | None = None):
    managers = get_channel_managers(chat_id)
    for uid in managers:
        try:
            await bot.send_message(uid, text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logger.debug(f"notify_managers: {e}")

async def save_admin_list(chat_id: str):
    try:
        admins = await bot.get_chat_administrators(chat_id)
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM saved_admins WHERE chat_id = ?", (chat_id,))
                for a in admins:
                    if a.user.is_bot:
                        continue
                    p = a
                    conn.execute("""
                        INSERT OR REPLACE INTO saved_admins
                        (chat_id, user_id, username, custom_title,
                         can_post_messages, can_edit_messages, can_delete_messages,
                         can_invite_users, can_restrict_members, can_promote_members,
                         can_manage_chat, saved_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        chat_id, a.user.id, a.user.username or '',
                        getattr(p, 'custom_title', '') or '',
                        int(getattr(p, 'can_post_messages', False) or False),
                        int(getattr(p, 'can_edit_messages', False) or False),
                        int(getattr(p, 'can_delete_messages', False) or False),
                        int(getattr(p, 'can_invite_users', False) or False),
                        int(getattr(p, 'can_restrict_members', False) or False),
                        int(getattr(p, 'can_promote_members', False) or False),
                        int(getattr(p, 'can_manage_chat', False) or False),
                        time.time()
                    ))
                conn.commit()
    except Exception as e:
        logger.error(f"save_admin_list hata: {e}")

async def enter_lockdown(chat_id: str, reason: str, spam_admin_id: int = 0):
    cfg = get_channel_cfg(chat_id)
    managers = get_channel_managers(chat_id)
    protected = set(managers + [cur_bot_id() or 0])
    if spam_admin_id:
        protected.discard(spam_admin_id)

    # Geri yükleme için mevcut admin yetkilerini sakla (zaten kilitliyse ilk kayıt korunur)
    if not cfg.get('lockdown_mode'):
        await save_admin_list(chat_id)

    try:
        admins = await bot.get_chat_administrators(chat_id)
        demoted = []
        for a in admins:
            if a.user.id in protected or a.user.is_bot:
                continue
            try:
                try:
                    await bot.promote_chat_member(
                        chat_id, a.user.id,
                        can_post_messages=False,
                        can_edit_messages=False,
                        can_delete_messages=False,
                        can_invite_users=False,
                        can_promote_members=False,
                        can_manage_chat=False,
                    )
                except Exception:
                    await bot.promote_chat_member(
                        chat_id, a.user.id,
                        can_delete_messages=False,
                        can_invite_users=False,
                        can_restrict_members=False,
                        can_promote_members=False,
                        can_manage_chat=False,
                    )
                demoted.append(a.user.id)
            except Exception as e:
                logger.debug(f"enter_lockdown: {e}")

        if cfg.get('lockdown_mode'):  # ikinci kilit: ilk turda yetkisi alınanlar da listede kalsın
            demoted = sorted(set(demoted) | set(json.loads(cfg.get('lockdown_saved_admins') or '[]')))
        cfg['lockdown_mode'] = 1
        cfg['lockdown_saved_admins'] = json.dumps(demoted)
        save_channel_cfg(chat_id, cfg)
        await log_channel_action(chat_id, 'lockdown', spam_admin_id, '', reason)

        keyboard = InlineKeyboardMarkup([
            [
                ibtn("Tum adminleri geri yukle", f"lockdown_restore|{chat_id}|all", GREEN),
                ibtn("Spam yapan haric geri yukle", f"lockdown_restore|{chat_id}|{spam_admin_id}", BLUE),
            ]
        ])
        suspect = f"Şüpheli: {mention_html(spam_admin_id, str(spam_admin_id))}\n" if spam_admin_id else ""
        await notify_managers(
            chat_id,
            f"🚨 <b>KANAL KORUMA MODU AKTIF</b>\n\n"
            f"Sebep: {html.escape(reason)}\n"
            f"{suspect}"
            f"Kanal: <code>{chat_id}</code>\n\n"
            f"Kurucu ve botu ekleyen admin haric tum admin yetkileri alindi.",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"enter_lockdown hata: {e}")

async def restore_admins(chat_id: str, exclude_user_id: int = 0):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM saved_admins WHERE chat_id = ?", (chat_id,)).fetchall()
    demoted = set(json.loads(get_channel_cfg(chat_id).get('lockdown_saved_admins') or '[]'))

    restored = 0
    for row in rows:
        if exclude_user_id and row['user_id'] == exclude_user_id:
            continue
        if row['user_id'] not in demoted:
            continue  # kilitte yetkisi alınmayan adminlere dokunma
        try:
            await bot.promote_chat_member(
                chat_id, row['user_id'],
                can_post_messages=bool(row['can_post_messages']),
                can_edit_messages=bool(row['can_edit_messages']),
                can_delete_messages=bool(row['can_delete_messages']),
                can_invite_users=bool(row['can_invite_users']),
                can_restrict_members=bool(row['can_restrict_members']),
                can_promote_members=bool(row['can_promote_members']),
                can_manage_chat=bool(row['can_manage_chat']),
            )
            restored += 1
        except Exception as e:
            logger.debug(f"restore_admins: {e}")

    cfg = get_channel_cfg(chat_id)
    cfg['lockdown_mode'] = 0
    cfg['lockdown_saved_admins'] = '[]'
    save_channel_cfg(chat_id, cfg)
    await log_channel_action(chat_id, 'lockdown_restored', 0, '', f'{restored} admin geri yuklendi')
    return restored

# ═══════════════════════════ GRUP AĞI (çoklu grup yönetimi) ═══════════════════════════
# Bir kurucu yönettiği grupları ağa bağlar: bir grupta ban diğerlerine yayılır (kick değil), ban kaldırma da.
NETWORK_COPY_EXCLUDE = {'welcome_msg', 'rules', 'spam_whitelist', 'banned_words', 'link_whitelist', 'raid_lock',
                        'media_lock', 'recovery_ids', 'welcome_rich', 'goodbye_rich', 'rules_rich', 'fsub_enabled',
                        'fsub_channel', 'fsub_title', 'fsub_link'}

def network_of(chat_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT owner_id FROM network_members WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return row['owner_id'] if row else None

def network_chats(owner_id: int) -> list:
    with get_db() as conn:
        return [r['chat_id'] for r in conn.execute("SELECT chat_id FROM network_members WHERE owner_id = ? ORDER BY added_at",
                                                   (owner_id,))]

def network_ban_sync(owner_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT ban_sync FROM networks WHERE owner_id = ?", (owner_id,)).fetchone()
    return bool(row['ban_sync']) if row else False

def network_add(chat_id: str, owner_id: int):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO networks (owner_id, ban_sync, created_at) VALUES (?, 1, ?)", (owner_id, time.time()))
        conn.execute("INSERT OR REPLACE INTO network_members (chat_id, owner_id, added_at) VALUES (?, ?, ?)",
                     (str(chat_id), owner_id, time.time()))
        conn.commit()

def network_remove(chat_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM network_members WHERE chat_id = ?", (str(chat_id),))
        conn.commit()

def network_copy(source: str, owner_id: int, lists: bool) -> int:
    """Kaynak grubun ayarlarını ağdaki diğer gruplara kopyalar. lists=True: kelime ve link listeleri (birleştirerek)."""
    src = get_channel_settings(source)['settings']
    copied = 0
    for cid in network_chats(owner_id):
        if cid == source:
            continue
        ch = get_channel_settings(cid)
        if not ch:
            continue
        if lists:
            for key in ('banned_words', 'link_whitelist'):
                ch['settings'][key] = list(dict.fromkeys(ch['settings'].get(key, []) + src.get(key, [])))
        else:
            for key in _default_channel_settings():
                if key not in NETWORK_COPY_EXCLUDE:
                    ch['settings'][key] = copy.deepcopy(src.get(key, _default_channel_settings()[key]))
        save_channel_settings(cid, ch)
        copied += 1
    return copied

async def network_ban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ağdaki bir grupta ban → 5 sn sonra hâlâ banlıysa (kick değilse) diğer gruplara yayılır; ban kaldırma da yayılır."""
    cm = update.chat_member
    if not cm:
        return
    chat_id = str(cm.chat.id)
    owner = network_of(chat_id)
    if not owner or not network_ban_sync(owner):
        return
    user = cm.new_chat_member.user
    old, new = cm.old_chat_member.status, cm.new_chat_member.status
    if user.id == cur_bot_id() or user.is_bot:
        return
    if new == 'kicked' and old != 'kicked':
        if context.job_queue:
            context.job_queue.run_once(_network_ban_job, 5, data={'chat_id': chat_id, 'owner': owner, 'user_id': user.id})
    elif old == 'kicked' and new != 'kicked':
        async with _db_lock:
            with get_db() as conn:
                n = conn.execute("DELETE FROM network_bans WHERE owner_id = ? AND user_id = ?", (owner, user.id)).rowcount
                conn.commit()
        if n:
            for cid in network_chats(owner):
                if cid != chat_id:
                    try:
                        await bot.unban_chat_member(cid, user.id, only_if_banned=True)
                    except Exception as e:
                        logger.debug(f"Ağ ban kaldırma ({cid}): {e}")
            await send_log(chat_id, f"🌐 Ağ banı kaldırıldı: {mention(user)} (ağdaki tüm gruplarda)", ParseMode.HTML)

async def _network_ban_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM network_bans WHERE owner_id = ? AND user_id = ?", (d['owner'], d['user_id'])).fetchone():
            return  # zaten ağ genelinde banlı (yayılan banın kendi bildirimi)
    try:
        member = await bot.get_chat_member(d['chat_id'], d['user_id'])
    except Exception as e:
        logger.debug(f"Ağ banı kontrolü yapılamadı: {e}")
        return
    if member.status != 'kicked':
        return  # kick (ban + unban) — yayılmaz
    until_ts = member.until_date.timestamp() if getattr(member, 'until_date', None) else 0
    until = int(until_ts) if until_ts > time.time() + 60 else None
    async with _db_lock:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO network_bans (owner_id, user_id, source_chat, until_date, banned_at) "
                         "VALUES (?, ?, ?, ?, ?)", (d['owner'], d['user_id'], d['chat_id'], until, time.time()))
            conn.commit()
    ok = 0
    for cid in network_chats(d['owner']):
        if cid == d['chat_id']:
            continue
        try:
            await bot.ban_chat_member(cid, d['user_id'], until_date=until)
            ok += 1
        except Exception as e:
            logger.debug(f"Ağ banı uygulanamadı ({cid}): {e}")
    await send_log(d['chat_id'], f"🌐 Ağ banı: {mention(member.user)} ağdaki {ok} gruba daha yayıldı", ParseMode.HTML)

# ═══════════════════════════ YEDEK ADMİN KURTARMA ═══════════════════════════
# Admin listesinin düzenli anlık görüntüsü; toplu yetki alma sezilince uyarı; güvenilir kişiler /kurtar ile geri yükler.
ADMIN_RIGHT_FIELDS = ('can_manage_chat', 'can_delete_messages', 'can_manage_video_chats', 'can_restrict_members',
                      'can_promote_members', 'can_change_info', 'can_invite_users', 'can_post_stories',
                      'can_edit_stories', 'can_delete_stories', 'can_post_messages', 'can_edit_messages',
                      'can_pin_messages', 'can_manage_topics')
SNAPSHOT_KEEP = 10
_demotions: dict = {}      # (chat_id, by_id) → [(zaman, user_id)]
_demotion_alerted: dict = {}

async def take_admin_snapshot(chat_id: str, reason: str = "periyodik"):
    """Güncel admin listesini (yetkileriyle) kaydeder; öncekiyle aynıysa yeni kayıt açmaz. Dönüş: kayıt no veya None."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.debug(f"Admin anlık görüntüsü alınamadı ({chat_id}): {e}")
        return None
    data = sorted(({'user_id': a.user.id, 'name': a.user.first_name or str(a.user.id), 'status': a.status,
                    'title': getattr(a, 'custom_title', None) or '',
                    'rights': {f: bool(getattr(a, f, False)) for f in ADMIN_RIGHT_FIELDS}}
                   for a in admins if not a.user.is_bot), key=lambda x: x['user_id'])
    payload = json.dumps(data, ensure_ascii=False)
    with get_db() as conn:
        last = conn.execute("SELECT id, data FROM admin_snapshots WHERE chat_id = ? ORDER BY taken_at DESC LIMIT 1",
                            (chat_id,)).fetchone()
        if last and last['data'] == payload:
            return last['id']
        sid = conn.execute("INSERT INTO admin_snapshots (chat_id, data, admin_count, taken_at, reason) VALUES (?, ?, ?, ?, ?)",
                           (chat_id, payload, len(data), time.time(), reason)).lastrowid
        conn.execute("DELETE FROM admin_snapshots WHERE chat_id = ? AND id NOT IN "
                     "(SELECT id FROM admin_snapshots WHERE chat_id = ? ORDER BY taken_at DESC LIMIT ?)",
                     (chat_id, chat_id, SNAPSHOT_KEEP))
        conn.commit()
    return sid

async def admin_snapshot_job(context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        chats = [r['chat_id'] for r in conn.execute("SELECT chat_id FROM channels")]
    for cid in chats:
        await take_admin_snapshot(cid)

def _can_recover(chat_id: str, user_id: int) -> bool:
    channel = get_channel_settings(chat_id)
    if not channel:
        return False
    return (user_id in channel['settings'].get('recovery_ids', []) or user_id == channel.get('owner')
            or has_permission(chat_id, user_id, 90))

async def restore_snapshot(chat_id: str, snap_id: int):
    """Kayıttaki adminleri yetkileriyle geri atar (kurucu hariç). Bot sadece kendinde olan yetkileri verebilir.
    Dönüş: (başarılı, başarısız isimler)"""
    with get_db() as conn:
        row = conn.execute("SELECT data FROM admin_snapshots WHERE id = ? AND chat_id = ?", (snap_id, chat_id)).fetchone()
    if not row:
        return 0, ["kayıt bulunamadı"]
    try:
        me = await bot.get_chat_member(chat_id, bot_id_for(chat_id))
    except Exception as e:
        return 0, [f"bot bilgisi alınamadı: {e}"]
    ok, failed = 0, []
    for adm in json.loads(row['data']):
        if adm['status'] == 'creator':
            continue
        rights = {k: v and bool(getattr(me, k, False)) for k, v in adm['rights'].items()}
        try:
            await bot.promote_chat_member(chat_id, adm['user_id'], **rights)
            if adm.get('title'):
                try:
                    await bot.set_chat_administrator_custom_title(chat_id, adm['user_id'], adm['title'])
                except Exception as e:
                    logger.debug(f"Admin etiketi geri yüklenemedi: {e}")
            ok += 1
        except Exception as e:
            logger.debug(f"Admin geri yüklenemedi ({chat_id}/{adm['user_id']}): {e}")
            failed.append(adm['name'])
    invalidate_admin_cache(chat_id)
    await send_log(chat_id, f"♻️ Admin kurtarma: {ok} admin geri yüklendi"
                            + (f", başarısız: {html.escape(', '.join(failed))}" if failed else ""), ParseMode.HTML)
    return ok, failed

async def demotion_watch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aynı kişi 10 dk içinde 3+ admini düşürürse: kurtarma butonlu uyarı; ayar açıksa otomatik geri yükleme."""
    cm = update.chat_member
    if not cm or not cm.from_user:
        return
    old, new = cm.old_chat_member, cm.new_chat_member
    if old.status != 'administrator' or new.status == 'administrator' or new.user.is_bot or cm.from_user.id == cur_bot_id():
        return
    chat_id, by = str(cm.chat.id), cm.from_user
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    now = time.time()
    key = (chat_id, by.id)
    hits = [(t, u) for t, u in _demotions.get(key, []) if now - t < 600] + [(now, new.user.id)]
    _demotions[key] = hits
    if len(hits) < 3 or now - _demotion_alerted.get(chat_id, 0) < 1800:
        return
    _demotion_alerted[chat_id] = now
    first = min(t for t, _ in hits)
    with get_db() as conn:
        snap = conn.execute("SELECT id, taken_at FROM admin_snapshots WHERE chat_id = ? AND taken_at < ? "
                            "ORDER BY taken_at DESC LIMIT 1", (chat_id, first)).fetchone()
    title = html.escape(await _chat_title(chat_id))
    text = (f"🚨 <b>Toplu yetki alma</b>\nGrup/kanal: <b>{title}</b>\n{mention(by)} (<code>{by.id}</code>) "
            f"10 dk içinde {len(hits)} adminin yetkisini aldı.")
    markup = None
    if snap:
        when = datetime.fromtimestamp(snap['taken_at'], TZ_TR).strftime('%d.%m %H:%M')
        text += f"\nSon sağlam kayıt: {when}"
        markup = InlineKeyboardMarkup([[ibtn("♻️ Adminleri geri yükle", f"rc|{snap['id']}", GREEN)]])
        if channel['settings'].get('recovery_autorestore'):
            ok, failed = await restore_snapshot(chat_id, snap['id'])
            text += f"\n♻️ Otomatik geri yüklendi: {ok} admin" + (f" (başarısız: {len(failed)})" if failed else "")
            markup = None
    else:
        text += "\nKullanılabilir admin kaydı yok."
    targets = list(dict.fromkeys(get_channel_managers(chat_id) + channel['settings'].get('recovery_ids', [])))
    for uid in targets:
        if uid == by.id:
            continue
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug(f"Kurtarma uyarısı gönderilemedi ({uid}): {e}")
    await log_channel_action(chat_id, 'mass_demote', by.id, by.username or '', f"{len(hits)} admin")

async def cmd_kurtar(update: Update, context):
    """/kurtar (özelden) — güvenilir kişi veya kurucu: admin kaydını seçip geri yükler."""
    uid = update.effective_user.id
    with get_db() as conn:
        chats = [r['chat_id'] for r in conn.execute("SELECT chat_id FROM channels")]
    mine = [c for c in chats if _can_recover(c, uid)]
    if not mine:
        await update.effective_message.reply_text("Kurtarma yetkin olan bir grup/kanal yok. "
                                                  "Grup sahibi seni panelden 'güvenilir kişi' olarak eklemeli.")
        return
    rows = [[ibtn(f"🛟 {(await _chat_title(c))[:40]}", f"rk|{c}", BLUE)] for c in mine[:20]]
    await update.effective_message.reply_text("Hangi grubun/kanalın adminlerini geri yüklemek istiyorsun?",
                                              reply_markup=InlineKeyboardMarkup(rows))

async def recovery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """rk|<chat_id> kayıtları listele · rs|<kayıt> onay sor · rc|<kayıt> geri yükle"""
    query = update.callback_query
    kind, _, arg = query.data.partition('|')
    uid = query.from_user.id
    if kind == 'rk':
        chat_id = arg
        if not _can_recover(chat_id, uid):
            await query.answer("Yetkin yok!", show_alert=True)
            return
        await query.answer()
        with get_db() as conn:
            snaps = conn.execute("SELECT id, admin_count, taken_at, reason FROM admin_snapshots WHERE chat_id = ? "
                                 "ORDER BY taken_at DESC LIMIT 6", (chat_id,)).fetchall()
        if not snaps:
            await query.edit_message_text("Bu grup için henüz admin kaydı yok (kayıt 6 saatte bir alınır).")
            return
        rows = [[ibtn(f"{datetime.fromtimestamp(s['taken_at'], TZ_TR):%d.%m %H:%M} · {s['admin_count']} admin",
                      f"rs|{s['id']}", BLUE)] for s in snaps]
        await query.edit_message_text(f"<b>{html.escape(await _chat_title(chat_id))}</b> — geri yüklenecek kaydı seç:",
                                      parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        return
    try:
        snap_id = int(arg)
    except ValueError:
        await query.answer("Hata.", show_alert=True)
        return
    with get_db() as conn:
        snap = conn.execute("SELECT chat_id, data, taken_at FROM admin_snapshots WHERE id = ?", (snap_id,)).fetchone()
    if not snap or not _can_recover(snap['chat_id'], uid):
        await query.answer("Yetkin yok veya kayıt yok.", show_alert=True)
        return
    if kind == 'rs':
        await query.answer()
        names = ", ".join(html.escape(a['name']) for a in json.loads(snap['data']) if a['status'] != 'creator')
        await query.edit_message_text(
            f"♻️ {datetime.fromtimestamp(snap['taken_at'], TZ_TR):%d.%m %H:%M} kaydındaki adminler geri yüklenecek:\n"
            f"<blockquote expandable>{names or '—'}</blockquote>\nOnaylıyor musun?", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[ibtn("✅ Evet, geri yükle", f"rc|{snap_id}", GREEN),
                                                ibtn("❌ Vazgeç", f"rk|{snap['chat_id']}", RED)]]))
        return
    await query.answer("Geri yükleniyor...")
    ok, failed = await restore_snapshot(snap['chat_id'], snap_id)
    await query.edit_message_text(f"♻️ {ok} admin geri yüklendi."
                                  + (f"\n❌ Başarısız: {html.escape(', '.join(failed))}\n(Bot, kendisinde olmayan yetkiyi "
                                     "veremez ve başkasının atadığı adminleri değiştiremez.)" if failed else ""),
                                  parse_mode=ParseMode.HTML)

async def lockdown_restore_callback(update: Update, context):
    query = update.callback_query
    parts = query.data.split("|")
    chat_id = parts[1]
    mode = parts[2]

    managers = get_channel_managers(chat_id)
    if query.from_user.id not in managers:
        await query.answer("Yetkisiz!", show_alert=True)
        return
    await query.answer()

    exclude = 0
    if mode != 'all':
        try:
            exclude = int(mode)
        except Exception:
            exclude = 0

    restored = await restore_admins(chat_id, exclude_user_id=exclude)
    await query.message.edit_text(
        query.message.text + f"\n\nAdmin listesi geri yuklendi ({restored} admin)."
    )

async def check_admin_spam(chat_id: str, user_id: int, is_media: bool, msg_id: int = 0) -> bool:
    cfg = get_channel_cfg(chat_id)
    key = 'admin_media_enabled' if is_media else 'admin_spam_enabled'
    if not cfg.get(key, 0):
        return False

    limit = cfg.get('admin_media_limit' if is_media else 'admin_spam_limit', 10)
    window = cfg.get('admin_media_window' if is_media else 'admin_spam_window', 300)
    action = cfg.get('admin_media_action' if is_media else 'admin_spam_action', 'demote')
    msg_type = 'media' if is_media else 'text'
    now = time.time()
    since = now - window

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO admin_flood (chat_id, user_id, msg_type, msg_id, sent_at) VALUES (?,?,?,?,?)",
                (chat_id, user_id, msg_type, msg_id, now)
            )
            count_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM admin_flood WHERE chat_id=? AND user_id=? AND msg_type=? AND sent_at>=?",
                (chat_id, user_id, msg_type, since)
            ).fetchone()
            conn.commit()

    count = count_row['cnt'] if count_row else 0
    if count < limit:
        return False

    async with _db_lock:
        with get_db() as conn:
            msg_ids = conn.execute(
                "SELECT msg_id FROM admin_flood WHERE chat_id=? AND user_id=? AND msg_type=? AND sent_at>=? AND msg_id>0",
                (chat_id, user_id, msg_type, since)
            ).fetchall()
            conn.execute(
                "DELETE FROM admin_flood WHERE chat_id=? AND user_id=? AND msg_type=?",
                (chat_id, user_id, msg_type)
            )
            conn.commit()

    await _bulk_delete(chat_id, [row['msg_id'] for row in msg_ids])

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        username = member.user.username or member.user.first_name
    except Exception:
        username = str(user_id)

    label = 'medya flood' if is_media else 'spam'
    await log_channel_action(chat_id, f'admin_{label}', user_id, username, f'{count} mesaj / {window}sn')

    if action in ('demote', 'demote_ban'):
        try:
            await bot.promote_chat_member(
                chat_id, user_id,
                can_post_messages=False, can_edit_messages=False,
                can_delete_messages=False, can_invite_users=False,
                can_promote_members=False, can_manage_chat=False,
            )
        except Exception:
            try:
                await bot.promote_chat_member(
                    chat_id, user_id,
                    can_delete_messages=False, can_invite_users=False,
                    can_restrict_members=False, can_promote_members=False,
                    can_manage_chat=False,
                )
            except Exception as e:
                logger.debug(f"check_admin_spam: {e}")

    if action == 'demote_ban':
        try:
            await bot.ban_chat_member(chat_id, user_id)
        except Exception as e:
            logger.debug(f"check_admin_spam: {e}")

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO admin_blacklist (chat_id, user_id, username, reason, added_at) VALUES (?,?,?,?,?)",
                (chat_id, user_id, username, label, now)
            )
            conn.commit()

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO spam_incidents (chat_id, user_id, incident_at) VALUES (?,?,?)",
                (chat_id, user_id, now)
            )
            recent = conn.execute(
                "SELECT COUNT(DISTINCT user_id) as c FROM spam_incidents WHERE chat_id=? AND incident_at>=? AND user_id!=?",
                (chat_id, now - 1800, user_id)
            ).fetchone()
            conn.commit()

    if recent and recent['c'] >= 1:
        await enter_lockdown(chat_id, "30dk icinde 2+ admin spam yapti", spam_admin_id=user_id)
    else:
        await notify_managers(
            chat_id,
            f"⚠️ <b>Admin Spam Tespit Edildi!</b>\n\n"
            f"Admin: {mention_html(user_id, username)} (<code>{user_id}</code>)\n"
            f"Islem: {'Ban + Yetki alindi' if action == 'demote_ban' else 'Yetkisi alindi'}\n"
            f"Kanal: <code>{chat_id}</code>\n"
            "Kanal koruma modu: Aktif degil",
            parse_mode=ParseMode.HTML
        )
    return True

async def handle_channel_post_protection(update: Update, context):
    msg = update.channel_post
    if not msg:
        return

    chat_id = str(msg.chat_id)
    cfg = get_channel_cfg(chat_id)

    if msg.sender_chat:
        return

    if not msg.from_user:
        return

    user_id = msg.from_user.id
    managers = get_channel_managers(chat_id)
    if user_id in managers:
        return

    is_media = bool(msg.photo or msg.video or msg.document or msg.audio or msg.voice or msg.sticker or msg.animation)

    if cfg.get('admin_spam_enabled') or cfg.get('admin_media_enabled'):
        triggered = await check_admin_spam(chat_id, user_id, is_media, msg_id=msg.message_id)
        if triggered:
            return

    if is_media and cfg.get('admin_media_enabled'):
        pass

    if cfg.get('link_protection'):
        safe_admins = json.loads(cfg.get('safe_admins', '[]'))
        if user_id not in safe_admins:
            text = msg.text or msg.caption or ''
            if re.search(r'https?://|t\.me/', text):
                try:
                    await msg.delete()
                except Exception as e:
                    logger.debug(f"handle_channel_post_protection: {e}")
                await log_channel_action(chat_id, 'link_deleted', user_id, '', text[:100])
                return

async def handle_chat_member_protection(update: Update, context):
    member_update = update.chat_member
    if not member_update:
        return

    chat_id = str(member_update.chat.id)
    cfg = get_channel_cfg(chat_id)
    if not cfg:
        return

    old = member_update.old_chat_member
    new = member_update.new_chat_member
    by_user = member_update.from_user
    if not by_user:
        return

    managers = get_channel_managers(chat_id)
    by_name = by_user.username or by_user.first_name

    if new.status == 'administrator' and old.status != 'administrator':
        if not new.user.is_bot and by_user.id != cur_bot_id():
            await auto_assign_role(chat_id, new.user.id, new)
        if new.user.is_bot and cfg.get('bot_add_protection'):
            try:
                await bot.ban_chat_member(chat_id, new.user.id)
                await bot.unban_chat_member(chat_id, new.user.id)
            except Exception as e:
                logger.debug(f"handle_chat_member_protection: {e}")
            await notify_managers(chat_id, f"🤖 Bot ekleme engellendi\nBot: {mention(new.user)}\nEkleyen: {mention(by_user)}",
                                  parse_mode=ParseMode.HTML)
            await log_channel_action(chat_id, 'bot_add_blocked', new.user.id, new.user.username or '', f'Ekleyen: {by_name}')
            return

        if not new.user.is_bot and by_user.id not in managers and by_user.id != cur_bot_id():
            bot_given = get_bot_given_admins(chat_id)
            if new.user.id not in bot_given:
                await notify_managers(
                    chat_id,
                    f"⚠️ Yetkisiz admin ekleme tespit edildi!\n"
                    f"Ekleyen: {mention(by_user)} (<code>{by_user.id}</code>)\n"
                    f"Eklenen: {mention(new.user)} (<code>{new.user.id}</code>)\n"
                    f"Kanal: <code>{chat_id}</code>",
                    parse_mode=ParseMode.HTML
                )
                await log_channel_action(chat_id, 'unauthorized_promote', new.user.id, new.user.username or '', "Ekleyen: " + by_name)

    if cfg.get('bulk_ban_protection') and new.status == 'kicked' and old.status not in ('kicked', 'left'):
        window = cfg.get('bulk_ban_window', 300)
        limit = cfg.get('bulk_ban_limit', 5)
        now = time.time()
        async with _db_lock:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO admin_flood (chat_id, user_id, msg_type, sent_at) VALUES (?,?,?,?)",
                    (chat_id, by_user.id, 'ban', now)
                )
                cnt = conn.execute(
                    "SELECT COUNT(*) as c FROM admin_flood WHERE chat_id=? AND user_id=? AND msg_type='ban' AND sent_at>=?",
                    (chat_id, by_user.id, now - window)
                ).fetchone()
                conn.commit()

        if cnt and cnt['c'] >= limit:
            await enter_lockdown(chat_id, f"Toplu ban tespiti: {by_name} ({by_user.id}) {cnt['c']} ban / {window}sn", spam_admin_id=by_user.id)

async def handle_clone_protection(update: Update, context):
    if not update.my_chat_member:
        return

    chat = update.my_chat_member.chat
    if chat.type != 'channel':
        return

    chat_id = str(chat.id)
    cfg = get_channel_cfg(chat_id)
    if not cfg.get('clone_protection'):
        return

    saved_title = cfg.get('channel_title')
    saved_desc = cfg.get('channel_description')

    try:
        current = await bot.get_chat(chat_id)
        changed = []

        if saved_title and current.title != saved_title:
            changed.append(f"Baslik: '{current.title}' → '{saved_title}'")
            try:
                await bot.set_chat_title(chat_id, saved_title)
            except Exception as e:
                logger.debug(f"handle_clone_protection: {e}")

        if saved_desc and (current.description or '') != saved_desc:
            changed.append(f"Aciklama degistirildi")
            try:
                await bot.set_chat_description(chat_id, saved_desc)
            except Exception as e:
                logger.debug(f"handle_clone_protection: {e}")

        if changed:
            await notify_managers(
                chat_id,
                f"Kanal klonlama koruması devreye girdi!\n"
                f"Degisiklikler geri alindi:\n" + "\n".join(changed)
            )
            await log_channel_action(chat_id, 'clone_protection', 0, '', "; ".join(changed))
    except Exception as e:
        logger.error(f"clone_protection hata: {e}")

async def weekly_log_cleanup(context):
    now = time.time()
    week_ago = now - (7 * 86400)
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM channel_log WHERE timestamp < ?", (week_ago,))
            conn.execute("DELETE FROM mod_log WHERE timestamp < ?", (now - MOD_LOG_RETENTION_DAYS * 86400,))
            conn.execute("DELETE FROM member_events WHERE at < ?", (now - 60 * 86400,))
            conn.execute("DELETE FROM admin_flood WHERE sent_at < ?", (now - 3600,))
            conn.commit()

    with get_db() as conn:
        chats = conn.execute("SELECT DISTINCT chat_id FROM channel_log WHERE timestamp >= ?", (week_ago,)).fetchall()

    for row in chats:
        chat_id = row['chat_id']
        with get_db() as conn:
            logs = conn.execute(
                "SELECT action, user_id, username, detail, timestamp FROM channel_log WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp DESC LIMIT 30",
                (chat_id, week_ago)
            ).fetchall()
        if not logs:
            continue
        lines = []
        for l in logs:
            dt = datetime.fromtimestamp(l['timestamp'], TZ_TR).strftime('%d.%m %H:%M')
            who = mention_html(l['user_id'], l['username'] or str(l['user_id'])) if l['user_id'] else "-"
            lines.append(f"{dt} | {html.escape(l['action'])} | {who} | {html.escape((l['detail'] or '')[:50])}")
        report = (f"📋 <b>Haftalik Kanal Log Raporu</b>\n<code>{chat_id}</code>\n"
                  f"<blockquote expandable>{chr(10).join(lines)}</blockquote>")
        await notify_managers(chat_id, report, parse_mode=ParseMode.HTML)

def _kanal_settings_keyboard(chat_id: str, cfg: dict) -> InlineKeyboardMarkup:
    def tog(val): return "✅" if val else "❌"
    action_spam = "Ban+Yetki Al" if cfg.get('admin_spam_action') == 'demote_ban' else "Sadece Yetki Al"
    action_media = "Ban+Yetki Al" if cfg.get('admin_media_action') == 'demote_ban' else "Sadece Yetki Al"
    keyboard = [
        [ibtn(f"{tog(cfg.get('admin_spam_enabled'))} Admin Spam Koruma", f"kcfg|{chat_id}|admin_spam_enabled", GREEN if cfg.get('admin_spam_enabled') else RED)],
        [InlineKeyboardButton(f"Spam Aksiyon: {action_spam}", callback_data=f"kcfg|{chat_id}|admin_spam_action")],
        [ibtn(f"{tog(cfg.get('admin_media_enabled'))} Admin Medya Flood", f"kcfg|{chat_id}|admin_media_enabled", GREEN if cfg.get('admin_media_enabled') else RED)],
        [InlineKeyboardButton(f"Medya Aksiyon: {action_media}", callback_data=f"kcfg|{chat_id}|admin_media_action")],
        [ibtn(f"{tog(cfg.get('link_protection'))} Link Koruması", f"kcfg|{chat_id}|link_protection", GREEN if cfg.get('link_protection') else RED)],
        [ibtn(f"{tog(cfg.get('clone_protection'))} Klonlama Koruması", f"kcfg|{chat_id}|clone_protection", GREEN if cfg.get('clone_protection') else RED)],
        [ibtn(f"{tog(cfg.get('bot_add_protection'))} Bot Ekleme Koruması", f"kcfg|{chat_id}|bot_add_protection", GREEN if cfg.get('bot_add_protection') else RED)],
        [ibtn(f"{tog(cfg.get('bulk_ban_protection'))} Toplu Ban Koruması", f"kcfg|{chat_id}|bulk_ban_protection", GREEN if cfg.get('bulk_ban_protection') else RED)],
        [InlineKeyboardButton("👥 Güvenli Adminler", callback_data=f"kcfg|{chat_id}|safe_admins_panel")],
        [InlineKeyboardButton("💾 Kanal Başlık/Açıklama Kaydet", callback_data=f"kcfg|{chat_id}|save_info")],
        [ibtn("🔙 Kapat", f"kcfg|{chat_id}|close", RED)],
    ]
    return InlineKeyboardMarkup(keyboard)

async def cmd_kanal_settings(update: Update, context):
    if update.effective_chat.type == 'private':
        chat_id = context.user_data.get('selected_channel')
    else:
        chat_id = str(update.effective_chat.id)

    if not chat_id:
        await update.message.reply_text("Once /kanal ile sec!")
        return

    channel = get_channel_settings(chat_id)
    if not channel:
        await update.message.reply_text("Kanal bulunamadi!")
        return

    managers = get_channel_managers(chat_id)
    if update.effective_user.id not in managers:
        await update.message.reply_text("Yetkin yok!")
        return

    try:
        chat_info = await bot.get_chat(chat_id)
        chat_type = chat_info.type
    except Exception:
        chat_type = 'unknown'

    if chat_type == 'channel':
        cfg = get_channel_cfg(chat_id)
        keyboard = _kanal_settings_keyboard(chat_id, cfg)
        await update.message.reply_text(f"Kanal Koruma Ayarlari\n{chat_id}", reply_markup=keyboard)
    else:
        await update.message.reply_text("Bu komut kanal ayarlari icin. Grup ayarlari icin /settings kullan.")

async def kanal_cfg_callback(update: Update, context):
    query = update.callback_query
    parts = query.data.split("|")
    chat_id = parts[1]
    key = parts[2]

    managers = get_channel_managers(chat_id)
    if query.from_user.id not in managers:
        await query.answer("Yetkisiz!", show_alert=True)
        return
    if key != 'save_info':  # save_info kendi uyarısıyla yanıtlar (bir sorgu tek kez yanıtlanabilir)
        await query.answer()

    cfg = get_channel_cfg(chat_id)

    if key == 'close':
        await query.message.delete()
        return

    elif key == 'save_info':
        try:
            chat_info = await bot.get_chat(chat_id)
            cfg['channel_title'] = chat_info.title or ''
            cfg['channel_description'] = chat_info.description or ''
            save_channel_cfg(chat_id, cfg)
            await query.answer(f"Kaydedildi: {chat_info.title}", show_alert=True)
        except Exception as e:
            await query.answer(friendly_error(e), show_alert=True)
        return

    elif key == 'safe_admins_panel':
        try:
            admins = await bot.get_chat_administrators(chat_id)
            safe = json.loads(cfg.get('safe_admins', '[]'))
            buttons = []
            for a in admins:
                if a.user.is_bot or a.user.id in managers:
                    continue
                is_safe = a.user.id in safe
                name = f"@{a.user.username}" if a.user.username else (a.user.first_name or str(a.user.id))
                buttons.append([ibtn(f"{'✅' if is_safe else '❌'} {name}", f"kcfg|{chat_id}|safe_toggle|{a.user.id}", GREEN if is_safe else RED)])
            buttons.append([InlineKeyboardButton("🔙 Geri", callback_data=f"kcfg|{chat_id}|back")])
            await query.message.edit_text("Guvenli admin listesi:\n(Link atmasina izin verilenler)", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await query.message.edit_text(friendly_error(e))
        return

    elif key == 'back':
        cfg = get_channel_cfg(chat_id)
        await query.message.edit_text(f"Kanal Koruma Ayarlari\n{chat_id}", reply_markup=_kanal_settings_keyboard(chat_id, cfg))
        return

    elif key.startswith('safe_toggle'):
        uid = int(parts[3])
        safe = json.loads(cfg.get('safe_admins', '[]'))
        if uid in safe:
            safe.remove(uid)
        else:
            safe.append(uid)
        cfg['safe_admins'] = json.dumps(safe)
        save_channel_cfg(chat_id, cfg)
        try:
            admins = await bot.get_chat_administrators(chat_id)
            buttons = []
            for a in admins:
                if a.user.is_bot or a.user.id in managers:
                    continue
                is_safe = a.user.id in safe
                name = f"@{a.user.username}" if a.user.username else (a.user.first_name or str(a.user.id))
                buttons.append([ibtn(f"{'✅' if is_safe else '❌'} {name}", f"kcfg|{chat_id}|safe_toggle|{a.user.id}", GREEN if is_safe else RED)])
            buttons.append([InlineKeyboardButton("🔙 Geri", callback_data=f"kcfg|{chat_id}|back")])
            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logger.debug(f"kanal_cfg_callback: {e}")
        return

    elif key in ('admin_spam_action', 'admin_media_action'):
        cur = cfg.get(key, 'demote')
        cfg[key] = 'demote_ban' if cur == 'demote' else 'demote'
        save_channel_cfg(chat_id, cfg)

    elif key in ('admin_spam_enabled', 'admin_media_enabled', 'link_protection',
                 'clone_protection', 'bot_add_protection', 'bulk_ban_protection'):
        cfg[key] = 0 if cfg.get(key) else 1
        save_channel_cfg(chat_id, cfg)

    cfg = get_channel_cfg(chat_id)
    await query.message.edit_reply_markup(reply_markup=_kanal_settings_keyboard(chat_id, cfg))

async def kanal_temizle(update: Update, context):
    """Kanalda /temizle gönderisi: son 50.000 gönderi aralığı toplu silinir."""
    msg = update.channel_post
    if not msg:
        return
    chat_id = str(msg.chat_id)
    if not get_channel_settings(chat_id):
        return
    ids = list(range(msg.message_id, max(0, msg.message_id - 50001), -1))  # komut gönderisi dahil
    context.application.create_task(
        _run_cleanup(chat_id, ids, "Kanal gönderileri", "kanal yöneticisi", {}, context.job_queue), update=update)

async def istekonayla(update: Update, context):
    if update.channel_post:
        chat_id = str(update.channel_post.chat_id)
        reply_func = update.channel_post.reply_text
    else:
        chat_id = _get_effective_chat_id(update, context)
        reply_func = update.message.reply_text

    if not chat_id or not get_channel_settings(chat_id):
        await reply_func("Once /kanal ile sec!")
        return

    if not update.channel_post:
        if not await require(update, chat_id, 'can_requests'):
            return

    limit = None
    if context.args and context.args[0].isdigit():
        limit = int(context.args[0])

    await reply_func("İstekler onaylanıyor, lütfen bekle...")

    ub = await get_userbot()
    pending_ids = []
    if ub:
        try:
            from telethon.tl.functions.messages import GetChatInviteImportersRequest
            entity = await ub.get_entity(int(chat_id))
            result = await ub(GetChatInviteImportersRequest(
                peer=entity, requested=True, offset_date=0, offset_user=InputPeerUser(0, 0), limit=limit or 200))
            pending_ids = [u.user_id for u in result.importers]
        except Exception as e:
            logger.warning(f"Telethon istekonayla hata: {e}")
    if not pending_ids:
        # Telethon yoksa: botun gördüğü ve kaydettiği bekleyen istekler (Bot API'de "hepsini onayla" metodu yok)
        with get_db() as conn:
            pending_ids = [r['user_id'] for r in conn.execute(
                "SELECT user_id FROM join_requests WHERE chat_id = ? AND status = 'pending' ORDER BY requested_at",
                (chat_id,))]
    if limit:
        pending_ids = pending_ids[:limit]
    if not pending_ids:
        await reply_func("Bekleyen katılım isteği bulunamadı.")
        return

    approved, failed = 0, 0
    done = []
    for uid in pending_ids:
        try:
            await bot.approve_chat_join_request(chat_id, uid)
            approved += 1
            done.append(uid)
        except TelegramError as e:
            failed += 1
            if 'hide_requester_missing' in str(e).lower() or 'user_already_participant' in str(e).lower():
                done.append(uid)  # istek artık yok: listeden düş
        await asyncio.sleep(0.05)

    async with _db_lock:
        with get_db() as conn:
            conn.executemany("UPDATE join_requests SET status = 'approved' WHERE chat_id = ? AND user_id = ?",
                             [(chat_id, u) for u in done])
            conn.commit()

    msg = f"✅ {approved} istek onaylandı."
    if failed:
        msg += f" ({failed} istek onaylanamadı; süresi dolmuş veya geri çekilmiş olabilir)"
    await reply_func(msg)
    await send_log(chat_id, f"İstek onaylama: {approved} onaylandı | {chat_id}")

_blocked_cache: set | None = None
_gban_cache: set | None = None

def _load_block_caches():
    global _blocked_cache, _gban_cache
    with get_db() as conn:
        _blocked_cache = {r['entity_id'] for r in conn.execute("SELECT entity_id FROM blocked_entities").fetchall()}
        _gban_cache = {r['user_id'] for r in conn.execute("SELECT user_id FROM global_bans").fetchall()}

def invalidate_block_caches():
    global _blocked_cache, _gban_cache
    _blocked_cache = None
    _gban_cache = None

def is_blocked(entity_id) -> bool:
    if _blocked_cache is None:
        _load_block_caches()
    return str(entity_id) in _blocked_cache

def is_gbanned(user_id, chat_id=None) -> bool:
    """Global ban (bot sahibinin, tüm botlarda) ya da chat_id verilirse o sohbeti yöneten klonun ban listesi."""
    if _gban_cache is None:
        _load_block_caches()
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid in _gban_cache:
        return True
    if chat_id is not None:
        bid = chat_bot_id(chat_id)
        if bid and bid != BOT_ID:
            with get_db() as conn:
                return conn.execute("SELECT 1 FROM clone_bans WHERE bot_id = ? AND user_id = ?", (bid, uid)).fetchone() is not None
    return False

async def blocklist_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tüm güncellemelerden önce çalışır: engellenen kullanıcı/sohbetleri ve global banlıları durdurur."""
    user = update.effective_user
    chat = update.effective_chat
    if user and user.id == FOUNDER_ID:
        return
    if chat and chat.type != 'private' and is_blocked(chat.id):
        if update.my_chat_member is None:
            try:
                await context.bot.leave_chat(chat.id)
            except Exception:
                pass
        raise ApplicationHandlerStop
    if user and is_blocked(user.id):
        if update.callback_query:
            try:
                await update.callback_query.answer("Bu botu kullanman engellendi.", show_alert=True)
            except Exception:
                pass
        raise ApplicationHandlerStop
    if user and chat and chat.type in ('group', 'supergroup') and is_gbanned(user.id, chat.id) and update.effective_message:
        if get_channel_settings(str(chat.id)):
            try:
                await context.bot.ban_chat_member(chat.id, user.id)
                await update.effective_message.delete()
            except Exception as e:
                logger.debug(f"Gban guard: {e}")
        raise ApplicationHandlerStop

def get_bot_given_admins(chat_id: str) -> set:
    with get_db() as conn:
        rows = conn.execute("SELECT user_id FROM bot_given_admins WHERE chat_id = ?", (chat_id,)).fetchall()
    return {r['user_id'] for r in rows}

async def auto_assign_role(chat_id: str, user_id: int, member):
    """Telegram'dan elle admin yapılan kişiye haklarına göre rütbe verir (bot rütbesi varsa dokunmaz)."""
    if getattr(member, 'can_promote_members', False):
        role = 'yardimci_kurucu'
    elif getattr(member, 'can_restrict_members', False):
        role = 'basadmin'
    else:
        role = 'admin'
    async with _db_lock:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO roles (chat_id, user_id, role) VALUES (?,?,?)", (chat_id, user_id, role))
            conn.commit()
    return role

async def cmd_uvye_etiketi(update: Update, context):
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    user_id = update.effective_user.id
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    if not await require(update, chat_id, 'can_manage_roles'):
        return

    target_id = None
    tag = None

    if msg.reply_to_message:
        target_id = msg.reply_to_message.from_user.id
        tag = ' '.join(context.args) if context.args else None
    elif context.args and len(context.args) >= 2:
        ref = context.args[0].lstrip('@')
        tag = ' '.join(context.args[1:])
        if ref.isdigit() and valid_id(int(ref)):
            target_id = int(ref)
        else:
            tid, _ = await resolve_user(chat_id, ref)
            target_id = tid
    else:
        await msg.reply_text("Kullanim: /uyeetiketi @kullanici <etiket> veya reply + /uyeetiketi <etiket>")
        return

    if not target_id or not tag:
        await msg.reply_text("Kullanici ve etiket gerekli!")
        return

    if len(tag) > 32:
        await msg.reply_text("Etiket max 32 karakter olabilir!")
        return

    if target_id != user_id and not can_act_on(chat_id, user_id, target_id):
        await msg.reply_text("⛔ Kendi rütbendeki veya üstündeki birine etiket veremezsin.")
        return

    try:
        target_member = await context.bot.get_chat_member(chat_id, target_id)
        who = mention(target_member.user)
        if target_member.status not in ('administrator', 'creator'):
            await msg.reply_text(f"{who} admin degil! Sadece adminlere etiket verilebilir.", parse_mode=ParseMode.HTML)
            return
        await context.bot.set_chat_administrator_custom_title(chat_id, target_id, tag)
        async with _db_lock:
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO member_tags (chat_id, user_id, tag, set_by, set_at) VALUES (?,?,?,?,?)",
                    (chat_id, target_id, tag, user_id, time.time())
                )
                conn.commit()
        await msg.reply_text(f"🏷 Etiket verildi: {who} → {html.escape(tag)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.reply_text(friendly_error(e))

async def _cmd_set_role(update, context, role: str):
    """/admin, /basadmin, /yardimcikurucu @kişi [etiket] — rütbe verir (hiyerarşi kurallarıyla)."""
    msg = update.channel_post or update.message
    if not msg:
        return
    chat_id = str(msg.chat_id) if update.channel_post else _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    caller_id = update.effective_user.id if update.effective_user and not update.channel_post else None
    if caller_id is None and not update.channel_post:
        return
    if caller_id is not None and not await require(update, chat_id, 'can_manage_roles'):
        return
    if caller_id is None and role == 'yardimci_kurucu':
        await msg.reply_text("Yardımcı kurucuyu sadece kurucu verebilir.")
        return

    parts = (msg.text or '').strip().split()
    args = parts[1:] if len(parts) > 1 else (context.args or [])
    if msg.reply_to_message and msg.reply_to_message.from_user:
        user_id = msg.reply_to_message.from_user.id
        tag = ' '.join(args) if args else None
    elif args:
        user_ref = args[0].strip().lstrip('@')
        tag = ' '.join(args[1:]) if len(args) > 1 else None
        if user_ref.isdigit() and valid_id(int(user_ref)):
            user_id = int(user_ref)
        else:
            user_id, _ = await resolve_user(chat_id, user_ref)
            if not user_id:
                await msg.reply_text("Kullanıcı bulunamadı! ID, @kullanıcıadı veya yanıt ile kullan.")
                return
    else:
        await msg.reply_text(f"Kullanım: /{ {'admin': 'admin', 'basadmin': 'basadmin'}.get(role, 'yardimcikurucu')} @kullanıcı [etiket]")
        return

    promote_chat_id = chat_id
    if update.channel_post:
        try:
            linked = getattr(await bot.get_chat(chat_id), 'linked_chat_id', None)
            if linked:
                promote_chat_id = str(linked)
        except TelegramError as e:
            logger.debug(f"_cmd_set_role: {e}")

    err = await set_rank(promote_chat_id, caller_id, user_id, role)
    if err:
        await msg.reply_text(err)
        return
    if tag:
        try:
            await bot.set_chat_administrator_custom_title(promote_chat_id, user_id, tag[:16])
        except TelegramError as e:
            logger.debug(f"_cmd_set_role etiket: {e}")
    try:
        who = mention((await bot.get_chat_member(promote_chat_id, user_id)).user)
    except TelegramError:
        who = mention_html(user_id, str(user_id))
    await msg.reply_text(f"✅ {who} → <b>{ROLE_NAMES[role]}</b>" + (f"\nEtiket: {html.escape(tag)}" if tag else ""),
                         reply_markup=rank_markup(promote_chat_id, user_id), parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"{ROLE_NAMES[role]} yapıldı: {who}" + (f" [{html.escape(tag)}]" if tag else ""), ParseMode.HTML)

async def cmd_admin_with_title(update: Update, context):
    await _cmd_set_role(update, context, 'admin')

async def cmd_basadmin(update: Update, context):
    await _cmd_set_role(update, context, 'basadmin')

async def cmd_yardimci_kurucu(update: Update, context):
    await _cmd_set_role(update, context, 'yardimci_kurucu')

async def cmd_panel(update: Update, context):
    if not is_bot_owner(update.effective_user.id):
        return
    if update.effective_chat.type != 'private':
        await update.message.reply_text("Bu komut sadece DM'de calisir!")
        return

    with get_db() as conn:
        total_groups = conn.execute("SELECT COUNT(*) as c FROM channels WHERE mybot(bot_id) AND chat_type IN ('group','supergroup')").fetchone()['c']
        total_channels = conn.execute("SELECT COUNT(*) as c FROM channels WHERE mybot(bot_id) AND chat_type='channel'").fetchone()['c']
        total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats WHERE chat_id IN (SELECT chat_id FROM channels WHERE mybot(bot_id))").fetchone()['c']

    keyboard = InlineKeyboardMarkup([
        [
            ibtn("📢 Kanallar", "panel|channels", BLUE),
            ibtn("👥 Gruplar", "panel|groups", BLUE),
        ],
        [
            ibtn("🚫 Engelliler", "panel|blocked", BLUE),
            ibtn("📊 İstatistikler", "panel|stats", BLUE),
        ],
    ])
    await update.message.reply_text(
        f"🤖 {brand()} Security Bot Paneli\n\n"
        f"📊 İstatistikler:\n"
        f"├ Toplam Grup: {total_groups}\n"
        f"├ Toplam Kanal: {total_channels}\n"
        f"└ Toplam Kullanici: {total_users}",
        reply_markup=keyboard
    )

async def panel_callback(update: Update, context):
    query = update.callback_query
    if not is_bot_owner(query.from_user.id):
        await query.answer("Yetkisiz!", show_alert=True)
        return
    await query.answer()

    data = query.data
    parts = data.split("|")
    action = parts[1] if len(parts) > 1 else ''

    if action == 'channels':
        with get_db() as conn:
            rows = conn.execute("SELECT chat_id, settings FROM channels WHERE mybot(bot_id) AND chat_type='channel'").fetchall()
        if not rows:
            await query.message.edit_text("Kayitli kanal yok.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="panel|back")]]))
            return
        buttons = []
        for row in rows:
            cid = row['chat_id']
            try:
                chat = await context.bot.get_chat(cid)
                name = chat.title or cid
                username = chat.username
                link = f"https://t.me/{username}" if username else None
            except Exception:
                name = cid
                link = None
            row_btns = []
            if link:
                row_btns.append(InlineKeyboardButton(f"📢 {name}", url=link))
            else:
                row_btns.append(InlineKeyboardButton(f"📢 {name}", callback_data=f"panel|chatinfo|{cid}"))
            row_btns.append(InlineKeyboardButton("🛠 Bilgi", callback_data=f"panel|chatinfo|{cid}"))
            buttons.append(row_btns)
        buttons.append([InlineKeyboardButton("🔙 Geri", callback_data="panel|back")])
        await query.message.edit_text("📢 Kanallar:", reply_markup=InlineKeyboardMarkup(buttons))

    elif action == 'groups':
        with get_db() as conn:
            rows = conn.execute("SELECT chat_id FROM channels WHERE mybot(bot_id) AND chat_type IN ('group','supergroup')").fetchall()
        if not rows:
            await query.message.edit_text("Kayitli grup yok.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="panel|back")]]))
            return
        buttons = []
        for row in rows:
            cid = row['chat_id']
            try:
                chat = await context.bot.get_chat(cid)
                name = chat.title or cid
                username = chat.username
                link = f"https://t.me/{username}" if username else None
            except Exception:
                name = cid
                link = None
            row_btns = []
            if link:
                row_btns.append(InlineKeyboardButton(f"👥 {name}", url=link))
            else:
                row_btns.append(InlineKeyboardButton(f"👥 {name}", callback_data=f"panel|chatinfo|{cid}"))
            row_btns.append(InlineKeyboardButton("🛠 Bilgi", callback_data=f"panel|chatinfo|{cid}"))
            buttons.append(row_btns)
        buttons.append([InlineKeyboardButton("🔙 Geri", callback_data="panel|back")])
        await query.message.edit_text("👥 Gruplar:", reply_markup=InlineKeyboardMarkup(buttons))

    elif action == 'chatinfo':
        cid = parts[2]
        try:
            chat = await context.bot.get_chat(cid)
            admins = await context.bot.get_chat_administrators(cid)
            cfg = get_channel_cfg(cid)
            member_count = chat.get_member_count() if hasattr(chat, 'get_member_count') else '?'
            try:
                member_count = await context.bot.get_chat_member_count(cid)
            except Exception:
                member_count = '?'

            prot_lines = []
            prot_lines.append(f"Admin Spam: {'✅' if cfg.get('admin_spam_enabled') else '❌'}")
            prot_lines.append(f"Link: {'✅' if cfg.get('link_protection') else '❌'}")
            prot_lines.append(f"Klonlama: {'✅' if cfg.get('clone_protection') else '❌'}")

            text = (
                f"{'📢' if chat.type == 'channel' else '👥'} {chat.title}\n\n"
                f"🆔 ID: {cid}\n"
                f"👥 Uye: {member_count}\n"
                f"👤 Admin sayisi: {len(admins)}\n"
                f"🛡 Koruma: {' | '.join(prot_lines)}"
            )
        except Exception as e:
            text = f"Bilgi alinamiyor: {e}"

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="panel|back")]])
        await query.message.edit_text(text, reply_markup=keyboard)

    elif action == 'blocked' and query.from_user.id != FOUNDER_ID:
        await query.message.edit_text("Engelli listesi sadece ana bot sahibine açıktır.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="panel|back")]]))

    elif action == 'blocked':
        with get_db() as conn:
            rows = conn.execute("SELECT entity_id, entity_type, reason FROM blocked_entities LIMIT 20").fetchall()
        if not rows:
            await query.message.edit_text("Engellenmiş kimse yok.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="panel|back")]]))
            return
        lines = ["🚫 Engellenenler:\n"]
        for r in rows:
            lines.append(f"• {r['entity_type']}: {r['entity_id']} — {r['reason'] or '-'}")
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="panel|back")]]))

    elif action == 'stats':
        with get_db() as conn:
            total_groups = conn.execute("SELECT COUNT(*) as c FROM channels WHERE mybot(bot_id) AND chat_type IN ('group','supergroup')").fetchone()['c']
            total_channels = conn.execute("SELECT COUNT(*) as c FROM channels WHERE mybot(bot_id) AND chat_type='channel'").fetchone()['c']
            total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats WHERE chat_id IN (SELECT chat_id FROM channels WHERE mybot(bot_id))").fetchone()['c']
            total_msgs = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id IN (SELECT chat_id FROM channels WHERE mybot(bot_id))").fetchone()['c']
            total_bans = conn.execute("SELECT COUNT(*) as c FROM ban_list WHERE chat_id IN (SELECT chat_id FROM channels WHERE mybot(bot_id))").fetchone()['c']
        text = (
            f"📊 Bot İstatistikleri\n\n"
            f"├ Toplam Grup: {total_groups}\n"
            f"├ Toplam Kanal: {total_channels}\n"
            f"├ Toplam Kullanici: {total_users}\n"
            f"├ Toplam Mesaj Kaydi: {total_msgs}\n"
            f"└ Toplam Ban: {total_bans}"
        )
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="panel|back")]]))

    elif action == 'back':
        with get_db() as conn:
            total_groups = conn.execute("SELECT COUNT(*) as c FROM channels WHERE mybot(bot_id) AND chat_type IN ('group','supergroup')").fetchone()['c']
            total_channels = conn.execute("SELECT COUNT(*) as c FROM channels WHERE mybot(bot_id) AND chat_type='channel'").fetchone()['c']
            total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats WHERE chat_id IN (SELECT chat_id FROM channels WHERE mybot(bot_id))").fetchone()['c']
        keyboard = InlineKeyboardMarkup([
            [
                ibtn("📢 Kanallar", "panel|channels", BLUE),
                ibtn("👥 Gruplar", "panel|groups", BLUE),
            ],
            [
                ibtn("🚫 Engelliler", "panel|blocked", BLUE),
                ibtn("📊 İstatistikler", "panel|stats", BLUE),
            ],
        ])
        await query.message.edit_text(
            f"🤖 {brand()} Security Bot Paneli\n\n"
            f"📊 İstatistikler:\n"
            f"├ Toplam Grup: {total_groups}\n"
            f"├ Toplam Kanal: {total_channels}\n"
            f"└ Toplam Kullanici: {total_users}",
            reply_markup=keyboard
        )

async def cmd_engelle(update: Update, context):
    if update.effective_user.id != FOUNDER_ID:
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /engelle <id> [sebep]")
        return
    entity_id = context.args[0]
    reason = ' '.join(context.args[1:]) if len(context.args) > 1 else ''
    try:
        test = int(entity_id)
        etype = 'chat' if test < 0 else 'user'
    except Exception:
        await update.message.reply_text("Gecersiz ID!")
        return

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO blocked_entities (entity_id, entity_type, reason, blocked_at) VALUES (?,?,?,?)",
                (entity_id, etype, reason, time.time())
            )
            conn.commit()
    invalidate_block_caches()
    await update.message.reply_text(f"✅ {entity_id} engellendi.")

async def cmd_engel_kaldir(update: Update, context):
    if update.effective_user.id != FOUNDER_ID:
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /engelkaldir <id>")
        return
    entity_id = context.args[0]
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM blocked_entities WHERE entity_id = ?", (entity_id,))
            conn.commit()
    invalidate_block_caches()
    await update.message.reply_text(f"✅ {entity_id} engeli kaldirildi.")

async def cmd_setwarnlimit(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    channel = get_channel_settings(chat_id)
    current = channel['settings'].get('warn_limit', 5)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Mevcut uyari limiti: {current}\nKullanim: /setwarnlimit <2-20>")
        return
    limit = int(context.args[0])
    if not 2 <= limit <= 20:
        await update.message.reply_text("Limit 2-20 arasinda olmali!")
        return
    channel['settings']['warn_limit'] = limit
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Uyari limiti {limit} olarak ayarlandi.")

async def cmd_whitelist(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    channel = get_channel_settings(chat_id)
    whitelist = channel['settings'].get('spam_whitelist', [])
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        if not whitelist:
            await update.message.reply_text("Spam muaf listesi bos.\n/whitelist @kullanici ile ekle.")
        else:
            lines = ["Spam Muaf Listesi:"]
            for uid in whitelist:
                try:
                    m = await bot.get_chat_member(chat_id, uid)
                    name = mention(m.user)
                except Exception:
                    name = mention_html(uid, f"ID:{uid}")
                lines.append(f" - {name}")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return
    who = mention(member.user)
    if user_id in whitelist:
        whitelist.remove(user_id)
        channel['settings']['spam_whitelist'] = whitelist
        save_channel_settings(chat_id, channel)
        await update.message.reply_text(f"{who} muaf listeden cikarildi.", parse_mode=ParseMode.HTML)
    else:
        whitelist.append(user_id)
        channel['settings']['spam_whitelist'] = whitelist
        save_channel_settings(chat_id, channel)
        await update.message.reply_text(f"{who} spam muaf listesine eklendi.", parse_mode=ParseMode.HTML)

async def cmd_grupbilgi(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    channel = get_channel_settings(chat_id)
    try:
        chat_info = await bot.get_chat(chat_id)
        title = chat_info.title or chat_id
        username_link = f"@{chat_info.username}" if getattr(chat_info, 'username', None) else "Ozel grup"
        member_count = await bot.get_chat_member_count(chat_id)
    except Exception as e:
        await update.message.reply_text(friendly_error(e))
        return
    with get_db() as conn:
        total_msgs = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id=?", (chat_id,)).fetchone()['c']
        total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats WHERE chat_id=?", (chat_id,)).fetchone()['c']
        total_bans = conn.execute("SELECT COUNT(*) as c FROM ban_list WHERE chat_id=?", (chat_id,)).fetchone()['c']
        total_warns = conn.execute("SELECT SUM(warn_count) as c FROM warnings WHERE chat_id=?", (chat_id,)).fetchone()['c'] or 0
        admin_count = conn.execute("SELECT COUNT(*) as c FROM roles WHERE chat_id=?", (chat_id,)).fetchone()['c']
    settings = channel['settings']
    warn_limit = settings.get('warn_limit', 5)
    active = []
    if settings.get('anti_spam_flood'): active.append("Anti-Flood")
    if settings.get('anti_link'): active.append("Anti-Link")
    if settings.get('anti_media_flood'): active.append("Anti-Media")
    if settings.get('captcha_enabled'): active.append("Captcha")
    if settings.get('anti_raid'): active.append("Anti-Raid")
    if settings.get('word_ban_enabled'): active.append("Kelime Filtresi")
    active_str = ", ".join(active) if active else "Yok"
    text = (
        f"📊 {title}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🔗 {username_link}\n"
        f"🆔 {chat_id}\n"
        f"👥 Uye: {member_count:,}\n"
        f"👮 Admin: {admin_count}\n\n"
        f"📈 Istatistikler\n"
        f"├ Toplam mesaj: {total_msgs:,}\n"
        f"├ Aktif kullanici: {total_users:,}\n"
        f"├ Toplam ban: {total_bans:,}\n"
        f"├ Toplam uyari: {total_warns:,}\n"
        f"└ Uyari limiti: {warn_limit}\n\n"
        f"Aktif Koruma: {active_str}\n"
        f"━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text)

async def cmd_yazitura(update: Update, context):
    import random
    result = random.choice(["Yazı! 🪙", "Tura! 🪙"])
    await update.message.reply_text(result)

async def cmd_zar(update: Update, context):
    import random
    result = random.randint(1, 6)
    faces = {1:"⚀",2:"⚁",3:"⚂",4:"⚃",5:"⚄",6:"⚅"}
    await update.message.reply_text(f"{faces[result]} Zar: {result}")

async def help_command(update: Update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    if chat.type == 'private':
        if current_clone() and is_bot_owner(user.id):
            await update.message.reply_text(parse_mode=ParseMode.HTML, text=fold(
                f"🤖 {_help_title()} — Bot Sahibi Komutlari\n\n"
                "/panel — Botunun gruplari ve istatistikleri\n"
                "/duyuru <mesaj> — Botunun tum gruplarina duyuru\n"
                "/gban <id|@kullanici> [sebep] — Botunun tum gruplarinda banla\n"
                "/ungban <id|@kullanici> — Bani kaldir\n"
                "/gbanlist — Ban listesi\n"
                "/kanal — Grup sec\n"
            ) + brand_footer())
        elif user.id == FOUNDER_ID:
            await update.message.reply_text(parse_mode=ParseMode.HTML, text=fold(
                "🤖 Bot Sahibi Komutlari\n\n"
                "/klonlar — Klon botlari yonet (durdur/baslat/sil)\n"
                "/klon — Kendi klon botun\n"
                "/panel — Yonetim paneli\n"
                "/engelle <id> [sebep] — Engelle\n"
                "/engelkaldir <id> — Engel kaldir\n"
                "/gban <id|@kullanici> [sebep] — Tum gruplarda banla\n"
                "/ungban <id|@kullanici> — Global bani kaldir\n"
                "/gbanlist — Global ban listesi\n"
                "/yedek — Veritabani yedegi al\n"
                "/gmedyaengel — Yanitlanan medyayi tum gruplarda engelle\n"
                "/kurtar — Admin kurtarma\n"
                "/duyuru <mesaj> — Tum gruplara duyuru\n"
                "/kanal — Kanal sec\n"
                "/kanalsettings — Kanal ayarlari\n"
            ))
        else:
            await update.message.reply_text(parse_mode=ParseMode.HTML, text=fold(
                f"{_help_title()}\n\n"
                + ("" if current_clone() else "/klon — Kendi adinla klon bot ac\n") +
                "/start — Baslat\n"
                "/menu — Alt menuyu goster\n"
                "/settings — Secili grubun butonlu ayar paneli\n"
                "/help — Yardim\n"
                "/id — ID goster\n"
                "/kanal — Kanal baglantisi\n"
                "/itiraz <aciklama> — Ban itirazi gonder\n"
                "/kurtar — Admin kurtarma (guvenilir kisiler icin)\n"
            ) + brand_footer())
        return

    chat_id = str(chat.id)
    is_admin = has_permission(chat_id, user.id, min_level=50)

    if not is_admin:
        try:
            m = await bot.get_chat_member(chat_id, user.id)
            if m.status in ('administrator', 'creator'):
                is_admin = True
        except Exception as e:
            logger.debug(f"help_command: {e}")

    user_section = (
        "👤 Kullanici Komutlari\n\n"
        "📊 Istatistik & Profil\n"
        "/profil — Kendi profilini goruntule\n"
        "/gunluk — Gunluk mesaj siralaması\n"
        "/haftalik — Haftalik siralama\n"
        "/aylik — Aylik siralama\n"
        "/toplam — Tum zamanlar siralaması\n"
        "/top — Butonlu siralama menusu\n"
        "/info @kullanici — Kullanici istatistikleri\n"
        "/grupbilgi — Grup hakkinda bilgi\n"
        "/rules — Grup kurallarini gor\n"
        "/afk [sebep] — AFK ol (etiketleyene bilgi verilir, yazınca kalkar)\n"
        "/oylama — Yanıtladığın kişi için susturma oylaması başlat\n"
        "/etiketme — /etiket listesinden çık (tekrar yazınca geri gir)\n\n"
        "📝 Notlar\n"
        "/notlar — Kayitli notlar\n"
        "#not — Notu getir (ornek: #kurallar)\n"
        "/not <isim> — Notu getir\n\n"
        "🎰 Eglence\n"
        "/yazitura — Yazi tura at\n"
        "/zar — Zar at\n\n"
        "ℹ️ Diger\n"
        "/help — Bu menu\n"
        "/id — ID goster\n"
        "/itiraz <aciklama> — Ban itirazi (bota ozelden)\n"
        "/report [sebep] veya @admin — Yanitladigin mesaji yetkililere bildir\n"
    )

    if not is_admin:
        await update.message.reply_text(fold(user_section) + brand_footer(), parse_mode=ParseMode.HTML)
        return

    level = user_level(chat_id, user.id)
    role = role_of_level(level)
    admin_section = (
        f"👮 Yetkili Komutlari — senin rütben: {ROLE_NAMES[role] if role else 'Telegram admini'}\n"
        "Rütbe sırası: 👑 Kurucu > 🔱 Yardımcı Kurucu > ⭐ Üst Admin > 🛡 Admin\n\n"
        "🛡 Admin ve üstü\n"
        "/warn @kullanici [sebep] — Uyari ver\n"
        "/mute @kullanici [sure] — Sustur (Admin en fazla 24 saat)\n"
        "/unmute @kullanici — Susturmayi kaldir\n"
        "/kick @kullanici — Gruptan at\n"
        "/warns · /banlist · /mutelist — Listeler\n"
        "/cekilis · /cekilis_bitir — Cekilis\n"
        "/filter — Otomatik yanit (or. selam → Aleykum selam; medya ve buton olur)\n"
        "/filters · /stop <kelime> · /stopall — Filtre listesi / sil\n"
        "/etiket <mesaj> · /etiketdur — Üyeleri gruplar hâlinde etiketle / durdur\n"
        "/sicil @kullanici — Botun tüm gruplarındaki ceza geçmişi ve eski isimleri (özelden gelir)\n"
        "/cekilis 1g 3 Ödül | kanal=@kanal mesaj=20 gun=7 — Şartlı, süreli çekiliş\n"
        "/yetkim — Rütben ve yetkilerin\n\n"
        "⭐ Üst Admin ve üstü\n"
        "/ban @kullanici [sure] [sebep] · /unban — Ban\n"
        "/unwarn @kullanici — 1 uyari geri al\n"
        "/temizle <sayi/all> · /slowmode <sn> — Toplu silme, yavas mod\n"
        "/pin · /unpin — Sabitleme\n"
        "/kilit [dk] · /medyakilit [dk] — Acil durum kilidi\n"
        "/istekonayla — Katilim isteklerini onayla\n"
        "/setrules · /setwelcome · /setgoodbye · /save · /notsil — Kurallar, karşılama, veda, notlar\n"
        "  (medya, butonlar, biçim ve rastgele mesaj desteklenir; /setwelcome yazınca yardımı çıkar)\n"
        "/welcome · /goodbye · /resetwelcome — Önizle / varsayılana dön\n"
        "/zamanla 6sa <mesaj> · /zamanlar — Zamanlanmış (tekrarlı) mesajlar\n\n"
        "🔱 Yardımcı Kurucu ve üstü\n"
        "/settings — Butonlu ayar paneli (tum koruma ayarlari)\n"
        "/kurulum — Hizli kurulum (paket + hos geldin + korumalar)\n"
        "/kanalzorunlu @kanal — Yazmak için kanala katılma zorunluluğu (kapat: /kanalzorunlu kapat)\n"
        "/antispam · /antilink · /antiforward · /antimedia · /antiraid · /captcha on/off\n"
        "/antiraid_ac · /medyaac — Kilitleri ac\n"
        "/nightmod · /wordban · /wordlist · /whitelist · /linkizin · /yeniuye\n"
        "/setwarnlimit · /setwarnaction · /captchasure\n"
        "/medyaengel · /paketengel — Medya/paket engeli\n"
        "/admin (/addadmin) · /basadmin @kullanici [etiket] — Rütbe ver\n"
        "/remove @kullanici — Rütbeyi al\n"
        "/yetkiler — Kişiye özel yetki paneli (bota özelden)\n"
        "/uyeetiketi @kullanici <etiket> — Admin etiketi\n"
        "/reload — Admin listesini Telegram'dan yenile (elle/başka botla verilen yetkiler)\n\n"
        "👑 Sadece Kurucu\n"
        "/yardimcikurucu @kullanici — Yardimci kurucu yap\n"
        "/setlog — Log kanali · Grup agi · Yedek admin kurtarma\n\n"
        "📊 Bilgi\n"
        "/stats · /grupbilgi · /leaderboard · /staff\n\n"
        "Sure formati: 30m, 2h, 7d"
    )

    keyboard = InlineKeyboardMarkup([
        [ibtn("⚙️ Ayarlar Paneli", "help_settings", BLUE)]
    ])
    await update.message.reply_text(
        fold(admin_section) + "\n\n" + fold(user_section) + brand_footer(),
        reply_markup=keyboard, parse_mode=ParseMode.HTML
    )

# ─────────────────────────── ÖZEL MENÜ, GRUP SEÇME, KOMUT MENÜSÜ ───────────────────────────
MENU_SETTINGS, MENU_GROUPS, MENU_STATS, MENU_HELP = "⚙️ Ayarlar", "🛡 Gruplarım", "📊 İstatistik", "❓ Yardım"
MENU_CLONE = "🤖 Klon bot"
DM_MENU_TEXTS = {MENU_SETTINGS, MENU_GROUPS, MENU_STATS, MENU_HELP, MENU_CLONE}
REQ_PICK_GROUP, REQ_PICK_CHANNEL = 1, 2
ADMIN_RIGHTS_GROUP = "change_info+delete_messages+restrict_members+invite_users+pin_messages+promote_members+manage_video_chats"
ADMIN_RIGHTS_CHANNEL = "change_info+post_messages+edit_messages+delete_messages+invite_users+promote_members"

def dm_menu_keyboard() -> ReplyKeyboardMarkup:
    """Özel sohbette klavyenin yerinde duran kalıcı menü; alttaki butonlar Telegram'ın sohbet seçicisini açar."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(MENU_SETTINGS, style=BLUE), KeyboardButton(MENU_GROUPS, style=BLUE)],
         [KeyboardButton(MENU_STATS), KeyboardButton(MENU_HELP)],
         [KeyboardButton("🔗 Grup seç", request_chat=KeyboardButtonRequestChat(
              request_id=REQ_PICK_GROUP, chat_is_channel=False, bot_is_member=True, request_title=True)),
          KeyboardButton("📢 Kanal seç", request_chat=KeyboardButtonRequestChat(
              request_id=REQ_PICK_CHANNEL, chat_is_channel=True, bot_is_member=True, request_title=True))]]
        + ([] if current_clone() else [[KeyboardButton(MENU_CLONE)]]),
        resize_keyboard=True, is_persistent=True, input_field_placeholder="Menüden seç veya komut yaz…")

def add_to_chat_markup(bot_username: str) -> InlineKeyboardMarkup:
    """Botu gerekli admin yetkileri önceden işaretli şekilde gruba/kanala ekleyen linkler."""
    return InlineKeyboardMarkup([[
        ibtn("➕ Gruba ekle", url=f"https://t.me/{bot_username}?startgroup=ulus&admin={ADMIN_RIGHTS_GROUP}", style=GREEN),
        ibtn("📢 Kanala ekle", url=f"https://t.me/{bot_username}?startchannel&admin={ADMIN_RIGHTS_CHANNEL}", style=BLUE),
    ]])

async def cmd_menu(update: Update, context):
    await update.message.reply_text("Menü aşağıda 👇", reply_markup=dm_menu_keyboard())

async def dm_menu_handler(update: Update, context):
    """Özel menü butonları (düz metin olarak gelir)."""
    text = update.message.text
    if text == MENU_HELP:
        await help_command(update, context)
        return
    if text == MENU_GROUPS:
        await kanal(update, context)
        return
    if text == MENU_CLONE:
        if not current_clone():
            await cmd_klon(update, context)
        return
    chat_id = context.user_data.get('selected_channel')
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce bir grup seç: 🛡 Gruplarım veya 🔗 Grup seç butonunu kullan.")
        return
    if text == MENU_SETTINGS:
        await cmd_settings(update, context)
    else:
        await stats(update, context)

async def chat_shared_handler(update: Update, context):
    """Menüdeki 'Grup seç' / 'Kanal seç' ile Telegram'ın sohbet seçicisinden gelen seçim."""
    shared = update.message.chat_shared
    chat_id = str(shared.chat_id)
    user_id = update.effective_user.id
    channel = get_channel_settings(chat_id) or (await ensure_registered(chat_id) and get_channel_settings(chat_id))
    if not channel:
        await update.message.reply_text(
            "Bot bu sohbette yönetici değil. Botu oraya yönetici olarak ekle ve tekrar seç.",
            reply_markup=add_to_chat_markup(context.bot.username))
        return
    if not has_permission(chat_id, user_id, 50) and not await repair_rank(chat_id, user_id):
        await update.message.reply_text("Bu sohbette yetkin yok.")
        return
    context.user_data['selected_channel'] = chat_id
    await update.message.reply_text(f"✅ Seçildi: <b>{html.escape(shared.title or chat_id)}</b>",
                                    parse_mode=ParseMode.HTML)
    await send_settings_panel(update.message, chat_id)

GROUP_USER_COMMANDS = [
    ("help", "Yardım menüsü"), ("rules", "Grup kuralları"), ("notlar", "Kayıtlı notlar"),
    ("profil", "Profilin ve uyarıların"), ("top", "Aktiflik sıralaması"), ("info", "Kullanıcı istatistikleri"),
    ("grupbilgi", "Grup bilgisi"), ("id", "ID göster"), ("zar", "Zar at"), ("yazitura", "Yazı tura at"),
    ("report", "Yanıtladığın mesajı yetkililere bildir"), ("afk", "AFK ol"), ("etiketme", "Etiket listesinden çık"),
    ("oylama", "Susturma oylaması (mesaja yanıt)"),
]
GROUP_ADMIN_COMMANDS = [
    ("settings", "Butonlu ayar paneli"), ("reload", "Admin listesini yenile"), ("warn", "Uyarı ver"), ("unwarn", "1 uyarı geri al"), ("warns", "Uyarıları gör"),
    ("mute", "Sustur"), ("unmute", "Susturmayı kaldır"), ("ban", "Banla"), ("unban", "Banı kaldır"),
    ("kick", "Gruptan at"), ("temizle", "Mesajları toplu sil"), ("pin", "Mesajı sabitle"), ("unpin", "Sabitlemeyi kaldır"),
    ("slowmode", "Yavaş mod"), ("banlist", "Ban listesi"), ("mutelist", "Mute listesi"), ("save", "Not kaydet"),
    ("notsil", "Not sil"), ("kurulum", "Hızlı kurulum"), ("filter", "Otomatik yanıt ekle"), ("filters", "Filtre listesi"), ("stop", "Filtre sil"),
    ("setwelcome", "Hoş geldin mesajı (medya/buton)"), ("setgoodbye", "Veda mesajı"), ("setrules", "Kuralları yaz"),
    ("zamanla", "Zamanlanmış mesaj ekle"), ("zamanlar", "Zamanlanmış mesajlar"), ("etiket", "Üyeleri etiketle"),
    ("etiketdur", "Etiketlemeyi durdur"), ("kanalzorunlu", "Kanal zorunluluğu"), ("sicil", "Kullanıcı sicili"),
    ("cekilis", "Çekiliş başlat"), ("cekilis_bitir", "Çekilişi bitir"),
    ("kilit", "Acil durum: grubu kilitle"), ("antiraid_ac", "Grup kilidini aç"), ("yetkim", "Rütben ve yetkilerin"), ("staff", "Yetkili listesi"), ("stats", "Grup istatistikleri"),
    ("medyaengel", "Yanıtlanan medyayı engelle"), ("paketengel", "Sticker paketini engelle"),
    ("medyakilit", "Medya gönderimini kilitle"), ("medyaac", "Medya kilidini aç"),
]
PRIVATE_COMMANDS = [
    ("start", "Başlat ve menü"), ("menu", "Menüyü göster"), ("kanal", "Grup/kanal seç"),
    ("settings", "Seçili grubun ayarları"), ("itiraz", "Ban itirazı gönder"), ("help", "Yardım"), ("id", "ID göster"),
    ("kurtar", "Admin kurtarma (güvenilir kişiler)"), ("kurulum", "Seçili grup için hızlı kurulum"),
]
CLONE_OWNER_COMMANDS = [
    ("panel", "Botunun grupları ve istatistikleri"), ("duyuru", "Tüm gruplarına duyuru"),
    ("gban", "Botunun gruplarında banla"), ("ungban", "Banı kaldır"), ("gbanlist", "Ban listesi"),
]
FOUNDER_COMMANDS = [("klonlar", "Klon botları yönet"), 
    ("panel", "Yönetim paneli"), ("gban", "Global ban"), ("ungban", "Global banı kaldır"),
    ("gbanlist", "Global ban listesi"), ("engelle", "Kullanıcı/sohbet engelle"), ("engelkaldir", "Engeli kaldır"),
    ("duyuru", "Tüm gruplara duyuru"), ("yedek", "Veritabanı yedeği"),
    ("gmedyaengel", "Medyayı tüm gruplarda engelle"),
]

async def setup_bot_profile(b):
    """'/' menüsünde herkes kendi rolüne uygun komutları görür; bot profil açıklaması ayarlanır."""
    def cmds(pairs):
        return [BotCommand(c, d) for c, d in pairs]
    try:
        await b.set_my_commands(cmds(GROUP_USER_COMMANDS), scope=BotCommandScopeAllGroupChats())
        await b.set_my_commands(cmds(GROUP_ADMIN_COMMANDS + GROUP_USER_COMMANDS),
                                scope=BotCommandScopeAllChatAdministrators())
        await b.set_my_commands(cmds(PRIVATE_COMMANDS), scope=BotCommandScopeAllPrivateChats())
        clone = current_clone()
        owner = bot_owner_id()
        if owner:
            extra = CLONE_OWNER_COMMANDS if clone else FOUNDER_COMMANDS
            await b.set_my_commands(cmds(PRIVATE_COMMANDS + extra), scope=BotCommandScopeChat(owner))
        name = brand()
        footer = f"\n\n⚡ Main bot: @{MAIN_BOT.username}" if clone and MAIN_BOT else ""
        await b.set_my_short_description(f"{name} — grup ve kanal koruma botu: spam, link, flood, raid ve captcha."[:120])
        await b.set_my_description(
            (f"🛡 {name} Security Bot\n\nGrubunu ve kanalını spam, link, flood, raid ve sahte hesaplara karşı korur. "
             "Tüm ayarlar butonlu panelden yapılır.\n\nBaşlamak için /start" + footer)[:512])
    except Exception as e:
        logger.warning(f"Komut menüsü / bot açıklaması ayarlanamadı: {e}")

# ─────────────────────────── KATILIM İSTEĞİ: ÖZELDEN DOĞRULAMA ───────────────────────────

def _captcha_options(correct: int) -> list:
    options = {correct}
    while len(options) < 4:
        cand = correct + random.randint(-6, 6)
        if cand >= 0:
            options.add(cand)
    options = list(options)
    random.shuffle(options)
    return options

async def send_join_captcha(request, channel: dict) -> bool:
    """Katılım isteği gönderene özelden captcha atar. Gönderilemezse False döner (istek admin onayına kalır)."""
    chat_id = str(request.chat.id)
    user = request.from_user
    timeout = int(channel['settings'].get('captcha_timeout', 120) or 120)
    question, answer = generate_captcha()
    kb = InlineKeyboardMarkup([[ibtn(str(o), f"jcap|{chat_id}|{o}", BLUE) for o in _captcha_options(int(answer))]])
    try:
        sent = await bot.send_message(
            request.user_chat_id,
            f"👋 Merhaba {mention(user)}!\n\n<b>{html.escape(request.chat.title or chat_id)}</b> grubuna katılma isteğin "
            f"alındı. Onaylanman için soruyu cevapla:\n\n🧮 <b>{question} = ?</b>\n\n⏰ Süre: {human_duration(timeout)}",
            parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        logger.info(f"Özelden doğrulama gönderilemedi ({chat_id}/{user.id}): {e}")
        return False
    async with _db_lock:
        with get_db() as conn:
            conn.execute("""INSERT OR REPLACE INTO join_captcha (chat_id, user_id, answer, message_id, created_at, passed)
                            VALUES (?, ?, ?, ?, ?, 0)""", (chat_id, user.id, answer, sent.message_id, time.time()))
            conn.commit()
    await send_log(chat_id, f"🔐 Özelden doğrulama gönderildi: {mention(user)} | {chat_id}", ParseMode.HTML)
    return True

async def join_captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """jcap|<chat_id>|<seçenek> — özelden doğrulama cevabı."""
    query = update.callback_query
    try:
        _, chat_id, chosen = query.data.split('|')
    except ValueError:
        await query.answer("Hata.", show_alert=True)
        return
    user = query.from_user
    with get_db() as conn:
        row = conn.execute("SELECT answer FROM join_captcha WHERE chat_id = ? AND user_id = ? AND passed = 0",
                           (chat_id, user.id)).fetchone()
    if not row:
        await query.answer("Bu doğrulamanın süresi dolmuş.", show_alert=True)
        return
    title = html.escape(await _chat_title(chat_id))
    if chosen == str(row['answer']):
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
        except Exception as e:
            logger.info(f"Katılım isteği onaylanamadı ({chat_id}/{user.id}): {e}")
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("DELETE FROM join_captcha WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
                    conn.commit()
            await query.answer("İstek artık geçerli değil (zaten işlenmiş olabilir).", show_alert=True)
            return
        async with _db_lock:
            with get_db() as conn:
                conn.execute("UPDATE join_captcha SET passed = 1, created_at = ? WHERE chat_id = ? AND user_id = ?",
                             (time.time(), chat_id, user.id))
                conn.commit()
        await query.answer("✅ Doğru!")
        await query.edit_message_text(f"✅ Doğrulandı! <b>{title}</b> grubuna kabul edildin.", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"✅ Özelden doğrulandı ve kabul edildi: {mention(user)} | {chat_id}", ParseMode.HTML)
    else:
        try:
            await bot.decline_chat_join_request(chat_id, user.id)
        except Exception as e:
            logger.debug(f"Katılım isteği reddedilemedi: {e}")
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM join_captcha WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
                conn.commit()
        await query.answer("❌ Yanlış cevap!", show_alert=True)
        await query.edit_message_text(f"❌ Yanlış cevap. <b>{title}</b> katılım isteğin reddedildi; "
                                      "tekrar istek gönderebilirsin.", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"❌ Özelden doğrulama başarısız, reddedildi: {mention(user)} | {chat_id}", ParseMode.HTML)

async def check_join_captcha_timeouts(context: ContextTypes.DEFAULT_TYPE):
    """Süresi dolan özelden doğrulamaları reddeder; eski 'geçti' kayıtlarını temizler."""
    now = time.time()
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id, user_id, message_id, created_at FROM join_captcha WHERE passed = 0").fetchall()
        conn.execute("DELETE FROM join_captcha WHERE passed = 1 AND created_at < ?", (now - 86400,))
        conn.commit()
    for r in rows:
        channel = get_channel_settings(r['chat_id'])
        timeout = int(channel['settings'].get('captcha_timeout', 120) or 120) if channel else 120
        if now - r['created_at'] < timeout:
            continue
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM join_captcha WHERE chat_id = ? AND user_id = ?", (r['chat_id'], r['user_id']))
                conn.commit()
        try:
            await bot.decline_chat_join_request(r['chat_id'], r['user_id'])
        except Exception as e:
            logger.debug(f"Zaman aşımı reddi: {e}")
        try:
            await bot.edit_message_text("⏰ Süre doldu, katılım isteğin reddedildi. Tekrar istek gönderebilirsin.",
                                        chat_id=r['user_id'], message_id=r['message_id'])
        except Exception as e:
            logger.debug(f"Doğrulama mesajı güncellenemedi: {e}")
        await send_log(r['chat_id'], f"⏰ Özelden doğrulama zaman aşımı → ID:{r['user_id']} reddedildi | {r['chat_id']}")

def _consume_join_pass(chat_id: str, user_id: int) -> bool:
    """Özelden doğrulamayı geçen kullanıcı grupta tekrar captcha çözmesin."""
    with get_db() as conn:
        n = conn.execute("DELETE FROM join_captcha WHERE chat_id = ? AND user_id = ? AND passed = 1",
                         (chat_id, user_id)).rowcount
        conn.commit()
    return n > 0

_flood_data: dict = {}

async def kanal_admin_spam_check(update: Update, context):
    
    msg = update.channel_post
    if not msg or not msg.sender_chat:
        return

    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return

    now = time.time()
    key = f"ch_flood_{chat_id}"
    if key not in _flood_data:
        _flood_data[key] = []
    _flood_data[key] = [t for t in _flood_data[key] if now - t < 10]
    _flood_data[key].append(now)

    if len(_flood_data[key]) > 10:
        await send_log(chat_id, f"⚠️ Kanal flood algılandı! 10 saniyede {len(_flood_data[key])} gönderi")
        _flood_data[key] = []

async def handle_join_request(update: Update, context):
    request = update.chat_join_request
    chat_id = str(request.chat.id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    user = request.from_user
    username = user.username or user.first_name
    user_id = user.id
    channel['stats']['requests'] = channel['stats'].get('requests', 0) + 1
    save_channel_settings(chat_id, channel)
    settings = channel['settings']

    if is_gbanned(user_id, chat_id):
        try:
            await bot.decline_chat_join_request(chat_id, user_id)
        except Exception as e:
            logger.debug(f"Gban katılım reddi: {e}")
        await send_log(chat_id, f"🌐 Global banlı katılım isteği reddedildi: {mention(user)}", ParseMode.HTML)
        return
    if settings.get('auto_reject_bot', False) and (user.is_bot or not user.username):
        try:
            await bot.decline_chat_join_request(chat_id, user_id)
            await send_log(chat_id, f"❌ Bot/sahte reddedildi: {mention(user)} → {chat_id}", ParseMode.HTML)
        except Exception as e:
            logger.debug(f"handle_join_request: {e}")
    elif settings.get('join_captcha', False) and await send_join_captcha(request, channel):
        pass  # doğru cevapta join_captcha_callback onaylar; gönderilemezse aşağıdaki kurallar geçerli
    elif settings.get('auto_accept', False):
        try:
            await bot.approve_chat_join_request(chat_id, user_id)
            channel['stats']['joins'] = channel['stats'].get('joins', 0) + 1
            save_channel_settings(chat_id, channel)
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("INSERT OR REPLACE INTO join_requests (chat_id, user_id, username, status, requested_at) VALUES (?, ?, ?, 'approved', ?)",
                                 (chat_id, user_id, username, time.time()))
                    conn.commit()
            await send_log(chat_id, f"✅ Otomatik kabul: {mention(user)} → {chat_id}", ParseMode.HTML)
        except Exception as e:
            logger.debug(f"handle_join_request: {e}")
    elif settings.get('auto_reject', False):
        try:
            await bot.decline_chat_join_request(chat_id, user_id)
            await send_log(chat_id, f"❌ Otomatik red: {mention(user)} → {chat_id}", ParseMode.HTML)
        except Exception as e:
            logger.debug(f"handle_join_request: {e}")
    else:
        async with _db_lock:
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO join_requests (chat_id, user_id, username, status, requested_at)
                    VALUES (?, ?, ?, 'pending', ?)
                """, (chat_id, user_id, username, time.time()))
                conn.commit()
        await send_log(chat_id, f"📩 Yeni istek: {mention(user)} → {chat_id}", ParseMode.HTML)

# ═══════════════════════════ RÜTBE VERME VE KİŞİYE ÖZEL YETKİ PANELİ ═══════════════════════════
PERM_KEYS = list(PERMS)
TG_KEYS = list(TG_RIGHTS)

def rank_error(chat_id: str, caller_id: int, target_id: int, role: str | None = None) -> str | None:
    """Rütbe verme/alma/düzenleme kuralları. Hata yoksa None."""
    if target_id in (cur_bot_id(), caller_id):
        return "Bu kişiye işlem yapamazsın."
    if not has_specific_permission(chat_id, caller_id, 'can_manage_roles'):
        return f"⛔ Rütbe yönetimi için {ROLE_NAMES['yardimci_kurucu']} ve üstü gerekli."
    if not can_act_on(chat_id, caller_id, target_id):
        return "⛔ Kendi rütbendeki veya üstündeki birini düzenleyemezsin."
    if role and ROLE_LEVELS[role] >= user_level(chat_id, caller_id):
        return f"⛔ {ROLE_NAMES[role]} rütbesini sadece üst rütbe verebilir."
    return None

async def _bot_rights(chat_id: str) -> set:
    try:
        me = await bot.get_chat_member(chat_id, bot_id_for(chat_id))
    except TelegramError:
        return set()
    return {k for k in TG_KEYS if getattr(me, k, False)}

async def apply_tg_rights(chat_id: str, user_id: int, level: int) -> None:
    """Rütbenin Telegram haklarını verir; kişiye özel kapatılanlar ve botta olmayanlar verilmez."""
    off = {p[3:] for p in perm_overrides(chat_id, user_id) if p.startswith('tg:')}
    have = await _bot_rights(chat_id)
    rights = {k: v and k in have for k, v in tg_rights_for(level, off).items()}
    rights['can_manage_chat'] = level > 0
    await bot.promote_chat_member(chat_id=chat_id, user_id=user_id, **rights)

async def set_rank(chat_id: str, caller_id: int | None, target_id: int, role: str) -> str | None:
    """Rütbe verir. caller_id None ise (kanal gönderisi) kontrol yapılmaz. Hata metni ya da None döner."""
    if not valid_id(target_id):
        return "Kullanıcı bulunamadı."
    if caller_id is not None:
        err = rank_error(chat_id, caller_id, target_id, role)
        if err:
            return err
    try:
        await apply_tg_rights(chat_id, target_id, ROLE_LEVELS[role])
    except TelegramError as e:
        err = str(e).lower()
        if 'not enough rights' in err or 'chat_admin_required' in err or 'right_forbidden' in err:
            return "❌ Botun admin atama yetkisi yok!"
        if 'user not found' in err or 'participant' in err:
            return "❌ Kullanıcı grupta bulunamadı!"
        return friendly_error(e)
    async with _db_lock:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO roles (chat_id, user_id, role) VALUES (?, ?, ?)", (chat_id, target_id, role))
            conn.execute("INSERT OR REPLACE INTO bot_given_admins (chat_id, user_id, given_by, given_at) VALUES (?,?,?,?)",
                         (chat_id, target_id, caller_id or 0, time.time()))
            conn.commit()
    invalidate_admin_cache(chat_id)
    return None

async def remove_rank(chat_id: str, caller_id: int, target_id: int) -> str | None:
    err = rank_error(chat_id, caller_id, target_id)
    if err:
        return err
    try:
        await bot.promote_chat_member(chat_id=chat_id, user_id=target_id, **{k: False for k in TG_KEYS},
                                      can_manage_chat=False)
    except TelegramError as e:
        logger.debug(f"remove_rank: {e}")
    async with _db_lock:
        with get_db() as conn:
            for tbl in ('roles', 'bot_given_admins', 'user_perm_off'):
                conn.execute(f"DELETE FROM {tbl} WHERE chat_id = ? AND user_id = ?", (chat_id, target_id))
            conn.commit()
    invalidate_admin_cache(chat_id)
    return None

def rank_markup(chat_id: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[ibtn("⚙️ Yetkiler", f"permtab|{chat_id}|{user_id}|bot", BLUE),
                                  ibtn("❌ Rütbeyi al", f"removeadmin|{chat_id}|{user_id}", RED)]])

async def render_perm_panel(chat_id: str, caller_id: int, target_id: int, section: str = 'bot'):
    """Kişiye özel yetki paneli: (metin, klavye). Yetki yoksa (hata metni, None)."""
    err = rank_error(chat_id, caller_id, target_id)
    if err:
        return err, None
    level = user_level(chat_id, target_id)
    role = role_of_level(level)
    if not role:
        return "Bu kişinin rütbesi yok. Önce /admin veya /basadmin ile rütbe ver.", None
    try:
        member = await bot.get_chat_member(chat_id, target_id)
        who = mention(member.user)
    except TelegramError:
        member, who = None, mention_html(target_id, str(target_id))
    off = perm_overrides(chat_id, target_id)
    rows = []
    if section == 'tg':
        have = await _bot_rights(chat_id)
        for i, k in enumerate(TG_KEYS):
            label, need = TG_RIGHTS[k]
            if need > level:
                rows.append([ibtn(f"🔒 {label}", "locked")])
            elif k not in have:
                rows.append([ibtn(f"⚠️ {label} (botta yok)", "locked")])
            else:
                on = bool(getattr(member, k, False)) if member else f"tg:{k}" not in off
                rows.append([ibtn(f"{'✅' if on else '❌'} {label}", f"toggleperm|{chat_id}|{target_id}|t{i}",
                                  GREEN if on else RED)])
        title = "📡 Telegram yetkileri"
    else:
        for i, k in enumerate(PERM_KEYS):
            label, need = PERMS[k]
            if need > level:
                rows.append([ibtn(f"🔒 {label}", "locked")])
            else:
                on = k not in off
                rows.append([ibtn(f"{'✅' if on else '❌'} {label}", f"toggleperm|{chat_id}|{target_id}|b{i}",
                                  GREEN if on else RED)])
        title = "🤖 Bot yetkileri"
    caller_level = user_level(chat_id, caller_id)
    ranks = [r for r in ROLE_ORDER[1:] if ROLE_LEVELS[r] < caller_level]
    rows.append([ibtn(("• " if r == role else "") + ROLE_NAMES[r], f"setrank|{chat_id}|{target_id}|{r}",
                      GREEN if r == role else BLUE) for r in ranks])
    other = 'tg' if section == 'bot' else 'bot'
    rows.append([ibtn("→ 📡 Telegram yetkileri" if other == 'tg' else "→ 🤖 Bot yetkileri",
                      f"permtab|{chat_id}|{target_id}|{other}", BLUE)])
    rows.append([ibtn("❌ Rütbeyi al", f"removeadmin|{chat_id}|{target_id}", RED),
                 ibtn("✅ Kapat", f"saveandexit|{chat_id}|{target_id}", GREEN)])
    text = (f"👤 <b>Yetki düzenleme:</b> {who}\n"
            f"Rütbe: <b>{ROLE_NAMES[role]}</b>\n{title}\n\n"
            f"✅ açık · ❌ kapalı · 🔒 rütbesi yetmiyor\n"
            f"<i>Değişiklikler anında uygulanır. Rütbe butonları rütbeyi değiştirir.</i>")
    return text, InlineKeyboardMarkup(rows)

async def _show_perm_panel(query, chat_id: str, target_id: int, section: str, note: str = ''):
    text, markup = await render_perm_panel(chat_id, query.from_user.id, target_id, section)
    if markup is None:
        await query.answer(text, show_alert=True)
        return
    await query.answer(note)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if 'not modified' not in str(e).lower():
            raise

def _parse_perm_cb(data: str):
    try:
        _, chat_id, uid, arg = data.split("|")
        return chat_id, int(uid), arg
    except ValueError:
        return None, None, None

async def permtab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """permtab|chat|uid|bot/tg — paneli açar veya bölüm değiştirir."""
    query = update.callback_query
    chat_id, target_id, section = _parse_perm_cb(query.data)
    if not chat_id:
        await query.answer("Hata.", show_alert=True)
        return
    await _show_perm_panel(query, chat_id, target_id, 'tg' if section == 'tg' else 'bot')

async def toggle_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """toggleperm|chat|uid|b<i> veya t<i> — tek yetkiyi aç/kapa."""
    query = update.callback_query
    chat_id, target_id, code = _parse_perm_cb(query.data)
    keys = PERM_KEYS if code and code[0] == 'b' else TG_KEYS
    try:
        key = keys[int(code[1:])]
    except (TypeError, ValueError, IndexError):
        await query.answer("Geçersiz buton.", show_alert=True)
        return
    err = rank_error(chat_id, query.from_user.id, target_id)
    if err:
        await query.answer(err, show_alert=True)
        return
    level = user_level(chat_id, target_id)
    if (PERMS if keys is PERM_KEYS else TG_RIGHTS)[key][1] > level:
        await query.answer("Bu yetki rütbesinin üstünde.", show_alert=True)
        return
    stored = key if keys is PERM_KEYS else f"tg:{key}"
    turn_on = stored in perm_overrides(chat_id, target_id)
    if keys is TG_KEYS:
        try:
            member = await bot.get_chat_member(chat_id, target_id)
            turn_on = not getattr(member, key, False)
        except TelegramError:
            pass
    async with _db_lock:
        with get_db() as conn:
            if turn_on:
                conn.execute("DELETE FROM user_perm_off WHERE chat_id = ? AND user_id = ? AND perm = ?",
                             (chat_id, target_id, stored))
            else:
                conn.execute("INSERT OR IGNORE INTO user_perm_off (chat_id, user_id, perm) VALUES (?, ?, ?)",
                             (chat_id, target_id, stored))
            conn.commit()
    section = 'bot'
    if keys is TG_KEYS:
        section = 'tg'
        try:
            await apply_tg_rights(chat_id, target_id, level)
        except TelegramError as e:
            await query.answer(friendly_error(e)[:190], show_alert=True)
            return
    label = (PERMS if keys is PERM_KEYS else TG_RIGHTS)[key][0]
    await _show_perm_panel(query, chat_id, target_id, section, f"{'✅ Açıldı' if turn_on else '❌ Kapatıldı'}: {label}")

async def setrank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """setrank|chat|uid|rol — panelden rütbe değiştir."""
    query = update.callback_query
    chat_id, target_id, role = _parse_perm_cb(query.data)
    if role not in ROLE_ORDER[1:]:
        await query.answer("Geçersiz rütbe.", show_alert=True)
        return
    if user_level(chat_id, target_id) == ROLE_LEVELS[role]:
        await query.answer(f"Zaten {ROLE_NAMES[role]}.")
        return
    err = await set_rank(chat_id, query.from_user.id, target_id, role)
    if err:
        await query.answer(err[:190], show_alert=True)
        return
    await send_log(chat_id, f"{ROLE_NAMES[role]} yapıldı: {mention_html(target_id, str(target_id))} | {mention(query.from_user)}",
                   ParseMode.HTML)
    await _show_perm_panel(query, chat_id, target_id, 'bot', f"Rütbe: {ROLE_NAMES[role]}")

async def remove_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id, target_id, _ = _parse_perm_cb(query.data + "|x")
    if not chat_id:
        await query.answer("Hata.", show_alert=True)
        return
    err = await remove_rank(chat_id, query.from_user.id, target_id)
    if err:
        await query.answer(err[:190], show_alert=True)
        return
    await query.answer("Rütbe alındı.")
    try:
        who = mention((await bot.get_chat_member(chat_id, target_id)).user)
    except TelegramError:
        who = mention_html(target_id, str(target_id))
    await query.edit_message_text(f"✅ {who} rütbesi alındı.", parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"🗑 {who} rütbesi alındı | {mention(query.from_user)}", ParseMode.HTML)

async def saveandexit_callback(update: Update, context):
    """saveandexit / cancelperm — paneli kapatır (değişiklikler zaten anında uygulanır)."""
    query = update.callback_query
    chat_id, target_id, _ = _parse_perm_cb(query.data + "|x")
    if not chat_id or rank_error(chat_id, query.from_user.id, target_id):
        await query.answer("Yetkin yok.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("✅ Yetki paneli kapatıldı. Değişiklikler kaydedildi.")

async def cmd_yetkim(update: Update, context):
    """/yetkim [@kişi] — rütbe ve açık/kapalı yetkiler."""
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.effective_message.reply_text("Önce /kanal ile seç!")
        return
    target = update.effective_user
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    level = user_level(chat_id, target.id)
    role = role_of_level(level)
    if not role:
        await msg.reply_text(f"{mention(target)} bu grupta yetkili değil.", parse_mode=ParseMode.HTML)
        return
    lines = [f"{'✅' if has_specific_permission(chat_id, target.id, k) else ('🔒' if need > level else '❌')} {label}"
             for k, (label, need) in PERMS.items()]
    await msg.reply_text(f"{mention(target)} — <b>{ROLE_NAMES[role]}</b>\n\n" + "\n".join(lines),
                         parse_mode=ParseMode.HTML)

async def locked_callback(update: Update, context):
    await update.callback_query.answer("🔒 Bu yetki rütbenin üstünde ya da botta bu yetki yok.", show_alert=True)

async def yetkiler(update: Update, context):
    """/yetkiler (özelden) — seçili gruptaki, senden düşük rütbeli yetkililerin listesi."""
    chat_id = context.user_data.get('selected_channel')
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile grup seç!")
        return
    if not await require(update, chat_id, 'can_manage_roles'):
        return
    uid = update.effective_user.id
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, role FROM roles WHERE chat_id = ?", (chat_id,)).fetchall()
    rows = sorted((r for r in rows if r['role'] in ROLE_LEVELS and can_act_on(chat_id, uid, r['user_id'])),
                  key=lambda r: -ROLE_LEVELS[r['role']])
    if not rows:
        await update.message.reply_text("Düzenleyebileceğin yetkili yok. Rütbe vermek için grupta /admin @kişi yaz.")
        return
    keyboard = []
    for r in rows:
        try:
            u = (await bot.get_chat_member(chat_id, r['user_id'])).user
            name = f"@{u.username}" if u.username else (u.first_name or str(u.id))
        except TelegramError:
            name = str(r['user_id'])
        keyboard.append([ibtn(f"{ROLE_NAMES[r['role']]} · {name}", f"permtab|{chat_id}|{r['user_id']}|bot", BLUE)])
    await update.message.reply_text("👑 <b>Yetki yönetimi</b>\nDüzenlemek istediğin kişiyi seç:",
                                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def start(update: Update, context):
    args = context.args

    if args and args[0].startswith("nightmod_"):
        try:
            chat_id = args[0].replace("nightmod_", "")
            context.user_data['selected_channel'] = chat_id
            nm = get_nightmod(chat_id)
            restrictions = json.loads(nm['restrictions']) if nm and isinstance(nm['restrictions'], str) else (nm['restrictions'] if nm else {})
            sh = nm['start_hour'] if nm else 23
            sm_ = nm['start_minute'] if nm else 0
            eh = nm['end_hour'] if nm else 7
            em_ = nm['end_minute'] if nm else 0
            keyboard = _nightmod_keyboard(chat_id, restrictions)
            await update.message.reply_text(
                f"Night Mode Yapilandirmasi\n"
                f"Kanal: {chat_id}\n"
                f"Saat: {sh:02d}:{sm_:02d} - {eh:02d}:{em_:02d} (UTC+3)\n\n"
                f"Kisitlanacak izinleri sec, sonra Kaydet'e bas:\n"
                f"Saat degistirmek icin: /nightmod 23:00 07:00",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"nightmod start hata: {e}")
            await update.message.reply_text("Night mode yapilandirma hatasi.")
        return

    if args and args[0].startswith("kurulum_"):
        await cmd_kurulum(update, context)
        return
    if args and args[0].startswith("ra_"):
        await show_rich_action(update, context, args[0][3:])
        return
    if not args or not args[0].startswith("aup_"):
        if update.effective_chat.type != 'private':
            await update.message.reply_text(f"🛡 {html.escape(brand())} aktif! Ayarlar için /settings, komutlar için /help.")
            return
        c = current_clone()
        if c and c.get('start_text'):
            body = html.escape(c['start_text'])
        else:
            body = (f"🛡 <b>{html.escape(brand())} Security Bot</b>\n\nGrup ve kanalların için spam, link, flood, raid ve "
                    "captcha koruması.\n\n1️⃣ Aşağıdaki butonla botu grubuna gerekli yetkilerle ekle.\n"
                    "2️⃣ Alttaki menüden grubunu seç, ⚙️ Ayarlar ile her şeyi butonlarla yönet.")
        markup = add_to_chat_markup(context.bot.username)
        if c and c.get('support_link'):
            markup = InlineKeyboardMarkup(list(markup.inline_keyboard) + [[InlineKeyboardButton("💬 Destek", url=c['support_link'])]])
        await update.message.reply_text(body + brand_footer(), parse_mode=ParseMode.HTML, reply_markup=markup)
        await update.message.reply_text("Menü aşağıda 👇", reply_markup=dm_menu_keyboard())
        return
    try:
        rest = args[0][4:]
        last_underscore = rest.rfind("_")
        chat_id_str = rest[:last_underscore]
        target_uid_str = rest[last_underscore + 1:]
        chat_id = chat_id_str
        target_uid = int(target_uid_str)
    except Exception:
        await update.message.reply_text("Geçersiz yetki linki.")
        return

    text, markup = await render_perm_panel(chat_id, update.effective_user.id, target_uid)
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

async def kanal(update: Update, context):
    user_id = update.effective_user.id

    if update.effective_chat and update.effective_chat.type in ['group', 'supergroup', 'channel']:
        chat_id = str(update.effective_chat.id)
        existing = get_channel_settings(chat_id)
        if not existing:
            # sahibi komutu yazan kişi değil, Telegram'daki grup kurucusu olur
            _unregistered_checked.pop(chat_id, None)
            ok = await ensure_registered(chat_id, update.effective_chat.type)
            await update.message.reply_text(
                f"Grup kaydedildi! Artık bota özelden 🛡 Gruplarım ile seçebilirsin.\nGrup ID: {chat_id}" if ok
                else "Kaydedilemedi: önce botu bu gruba yönetici yap.")
        else:
            await update.message.reply_text(
                f"Bu grup kayitli (ID: {chat_id})\n"
                "DM'de /kanal yazarak yonetim paneline gec."
            )
        return

    owned, unknown = [], []
    with get_db() as conn:
        all_chats = conn.execute(
            "SELECT chat_id, chat_type, owner_id FROM channels WHERE mybot(bot_id)"
        ).fetchall()
        for row in all_chats:
            cid = row['chat_id']
            ctype = row['chat_type'] or 'group'
            is_owner = (row['owner_id'] == user_id)
            has_role = conn.execute(
                "SELECT 1 FROM roles WHERE chat_id = ? AND user_id = ?", (cid, user_id)
            ).fetchone()
            if is_owner or has_role:
                owned.append((cid, ctype))
            else:
                unknown.append((cid, ctype))
    # Kayıtta rütbesi olmayan ama Telegram'da kurucu/yönetici olduğu sohbetler: rütbeyi Telegram'dan onar
    sem = asyncio.Semaphore(8)
    async def _check(cid):
        async with sem:
            return await repair_rank(cid, user_id)
    for (cid, ctype), fixed in zip(unknown, await asyncio.gather(*(_check(c) for c, _ in unknown))):
        if fixed:
            owned.append((cid, ctype))

    if not owned:
        await update.message.reply_text(
            "Kayıtlı bir grubun/kanalın bulunamadı.\n\n"
            "Alttaki 🔗 Grup seç / 📢 Kanal seç butonuyla sohbetini seç: bot orada yöneticiyse "
            "otomatik tanınır. Bot henüz ekli değilse önce Gruba ekle butonunu kullan.",
            reply_markup=add_to_chat_markup(context.bot.username))
        return

    keyboard = []
    for cid, ctype in owned:
        icon = "📢" if ctype == "channel" else "👥"
        try:
            chat_info = await bot.get_chat(cid)
            label = f"{icon} {chat_info.title or cid}"
        except Exception:
            label = f"{icon} {cid}"
        keyboard.append([ibtn(label, f"select_{cid}", BLUE)])

    await update.message.reply_text(
        "Yonetmek istedigin kanal/grubu sec:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    chat_id = query.data.replace("select_", "")
    context.user_data['selected_channel'] = chat_id
    try:
        chat_info = await context.bot.get_chat(chat_id)
        title = chat_info.title or chat_id
    except Exception:
        title = chat_id
    await query.message.edit_text(f"✅ Seçildi: {title}\nArtık DM'de komutları kullanabilirsin.")
    if has_permission(chat_id, query.from_user.id, 50):
        await send_settings_panel(query.message, chat_id)

async def id_command(update: Update, context):
    user = update.effective_user
    reply_text = f"Senin ID'n: <code>{user.id}</code>\n{mention(user)}\n\n"
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message.from_user
        reply_text += (
            f"Reply kişinin ID'si: <code>{replied.id}</code>\n"
            f"{mention(replied)}\n\n"
            f"Örnek:\n• <code>/admin {replied.id}</code>\n• <code>/ban {replied.id}</code>"
        )
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)

async def setlog(update: Update, context):
    chat_id = context.user_data.get('selected_channel')
    if not chat_id:
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    channel = get_channel_settings(chat_id)
    if not channel:
        await update.message.reply_text("Kanal bulunamadı!")
        return
    if not has_permission(chat_id, update.effective_user.id, LVL_KURUCU):
        await deny(update, level=LVL_KURUCU)
        return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await update.message.reply_text(f"Mevcut log: {channel.get('log_chat_id', 'Ayarlanmamış')}\nKullanım: /setlog -1001234567890")
        return
    new_log_id = context.args[0]
    channel['log_chat_id'] = new_log_id
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"✅ Log kanalı güncellendi: {new_log_id}")

async def staff(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    channel = get_channel_settings(chat_id) if chat_id else None
    if not channel:
        await update.message.reply_text("Kanal bulunamadı!")
        return

    role_order = ['kurucu', 'yardimci_kurucu', 'basadmin', 'admin']
    role_config = {r: tuple(ROLE_NAMES[r].split(' ', 1)) for r in ROLE_ORDER}

    with get_db() as conn:
        db_roles = conn.execute(
            "SELECT user_id, role FROM roles WHERE chat_id=?", (chat_id,)
        ).fetchall()

    db_role_map = {r['user_id']: r['role'] for r in db_roles}

    try:
        tg_admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        await update.message.reply_text(friendly_error(e))
        return

    grouped = {r: [] for r in role_order}
    seen = set()

    for member in tg_admins:
        u = member.user
        if u.is_bot:
            continue
        uid = u.id
        seen.add(uid)
        if member.status == 'creator' and getattr(member, 'is_anonymous', False) and uid != channel.get('added_by'):
            continue  # gizli (anonim) kurucu listede gösterilmez
        title = getattr(member, 'custom_title', None)
        entry = mention(u) + (f" » {html.escape(title)}" if title else "")

        if uid in db_role_map:
            role = db_role_map[uid]
        elif member.status == 'creator':
            role = 'kurucu'
        else:
            role = 'admin'

        if role in grouped:
            grouped[role].append(entry)

    for uid, role in db_role_map.items():
        if uid in seen:
            continue
        if role not in grouped:
            continue
        try:
            m = await bot.get_chat_member(chat_id, uid)
            if m.status in ('left', 'kicked'):
                continue
            u = m.user
            title = getattr(m, 'custom_title', None)
            entry = mention(u) + (f" » {html.escape(title)}" if title else "")
            grouped[role].append(entry)
        except Exception as e:
            logger.debug(f"staff: {e}")

    if not grouped['kurucu']:
        # kurucu gizliyse yerine en yetkili kişi kurucu olarak görünür
        top = next((r for r in role_order[1:] if grouped[r]), None)
        if top:
            grouped['kurucu'].append(grouped[top].pop(0))

    lines = ["━━━━━━━━━━━━━━━━━━━━", "GRUP PERSONELİ", "━━━━━━━━━━━━━━━━━━━━"]
    total = 0
    for role in role_order:
        members = grouped[role]
        if not members:
            continue
        emoji, label = role_config[role]
        lines.append(f"\n{emoji} {label}")
        for i, entry in enumerate(members):
            connector = "└" if i == len(members) - 1 else "├"
            lines.append(f" {connector} {entry}")
        total += len(members)

    lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Toplam: {total} personel")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ═══════════════════════════ YENİ ÖZELLİKLER ═══════════════════════════

# ── Global ban (tüm gruplarda) ──
async def _resolve_target_id(update: Update, context) -> tuple[int | None, str]:
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, ' '.join(context.args)
    if context.args:
        ref = context.args[0].lstrip('@')
        reason = ' '.join(context.args[1:])
        if ref.lstrip('-').isdigit():
            return (int(ref), reason) if valid_id(int(ref)) else (None, '')
        ub = await get_userbot()
        if ub:
            try:
                ent = await ub.get_entity(ref)
                return ent.id, reason
            except Exception:
                pass
        with get_db() as conn:
            row = conn.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1", (ref,)).fetchone()
        if row:
            return row['user_id'], reason
    return None, ''

async def cmd_gban(update: Update, context):
    """/gban — bot sahibi: tüm botlarda (klonlar dahil) global ban. Klon sahibi: sadece kendi klonunun gruplarında."""
    uid = update.effective_user.id
    if not is_bot_owner(uid):
        return
    target_id, reason = await _resolve_target_id(update, context)
    if not target_id:
        await update.effective_message.reply_text("Kullanım: /gban <id|@kullanıcı> [sebep] (veya mesaja yanıt)")
        return
    if target_id in (FOUNDER_ID, uid, bot_owner_id()):
        await update.effective_message.reply_text("Bu kişi banlanamaz.")
        return
    scope = None if uid == FOUNDER_ID else cur_bot_id()
    async with _db_lock:
        with get_db() as conn:
            if scope is None:
                conn.execute("INSERT OR REPLACE INTO global_bans (user_id, reason, banned_by, banned_at) VALUES (?, ?, ?, ?)",
                             (target_id, reason, uid, time.time()))
            else:
                conn.execute("INSERT OR REPLACE INTO clone_bans (bot_id, user_id, reason, banned_by, banned_at) "
                             "VALUES (?, ?, ?, ?, ?)", (scope, target_id, reason, uid, time.time()))
            conn.commit()
    invalidate_block_caches()
    where = "tüm botlarda" if scope is None else f"{brand()} gruplarında"
    status = await update.effective_message.reply_text(f"🌐 {target_id} {where} banlanıyor...")
    if scope is None:
        with get_db() as conn:
            chats = [r['chat_id'] for r in conn.execute(
                "SELECT chat_id FROM channels WHERE chat_type IN ('group','supergroup','channel')").fetchall()]
    else:
        chats = bot_chat_ids(scope)
    ok = fail = 0
    for cid in chats:
        try:
            await bot.ban_chat_member(cid, target_id)
            ok += 1
        except Exception:
            fail += 1
    await status.edit_text(f"🌐 {target_id} {where} banlandı.\n✅ {ok} sohbet | ❌ {fail} (yetki yok/üye değil)\nSebep: {reason or '-'}")

async def cmd_ungban(update: Update, context):
    uid = update.effective_user.id
    if not is_bot_owner(uid):
        return
    target_id, _ = await _resolve_target_id(update, context)
    if not target_id:
        await update.effective_message.reply_text("Kullanım: /ungban <id|@kullanıcı>")
        return
    scope = None if uid == FOUNDER_ID else cur_bot_id()
    async with _db_lock:
        with get_db() as conn:
            if scope is None:
                conn.execute("DELETE FROM global_bans WHERE user_id = ?", (target_id,))
            else:
                conn.execute("DELETE FROM clone_bans WHERE bot_id = ? AND user_id = ?", (scope, target_id))
            conn.commit()
    invalidate_block_caches()
    if scope is None:
        with get_db() as conn:
            chats = [r['chat_id'] for r in conn.execute("SELECT chat_id FROM channels").fetchall()]
    else:
        chats = bot_chat_ids(scope)
    for cid in chats:
        try:
            await bot.unban_chat_member(cid, target_id, only_if_banned=True)
        except Exception:
            pass
    await update.effective_message.reply_text(f"✅ {target_id} {'global ' if scope is None else ''}banı kaldırıldı.")

async def cmd_gbanlist(update: Update, context):
    uid = update.effective_user.id
    if not is_bot_owner(uid):
        return
    scope = None if uid == FOUNDER_ID and not current_clone() else cur_bot_id()
    with get_db() as conn:
        if scope is None:
            rows = conn.execute("SELECT user_id, reason, banned_at FROM global_bans ORDER BY banned_at DESC LIMIT 50").fetchall()
        else:
            rows = conn.execute("SELECT user_id, reason, banned_at FROM clone_bans WHERE bot_id = ? "
                                "ORDER BY banned_at DESC LIMIT 50", (scope,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("Ban listesi boş.")
        return
    lines = [f"• <code>{r['user_id']}</code> — {html.escape(r['reason'] or '-')} "
             f"({datetime.fromtimestamp(r['banned_at'], TZ_TR):%d.%m.%Y})" for r in rows]
    title = "Global Ban Listesi" if scope is None else f"{html.escape(brand())} Ban Listesi"
    await update.effective_message.reply_text(
        f"🌐 <b>{title}</b> (son 50):\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>",
        parse_mode=ParseMode.HTML)

# ── Ayar komutları ──
async def _require_settings_admin(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    channel = get_channel_settings(chat_id) if chat_id else None
    if not channel:
        await update.effective_message.reply_text("Önce /kanal ile seç!")
        return None, None
    if not await require(update, chat_id, 'can_manage_settings'):
        return None, None
    return chat_id, channel

async def cmd_setwarnaction(update: Update, context):
    """/setwarnaction ban | tempban 1d | kick | mute 2h"""
    chat_id, channel = await _require_settings_admin(update, context)
    if not chat_id:
        return
    s = channel['settings']
    if not context.args or context.args[0].lower() not in ('ban', 'tempban', 'kick', 'mute'):
        await update.effective_message.reply_text(
            f"Şu an: {s.get('warn_action', 'ban')} ({human_duration(int(s.get('warn_action_duration', 86400)))})\n\n"
            "Kullanım:\n/setwarnaction ban\n/setwarnaction tempban 1d\n/setwarnaction kick\n/setwarnaction mute 2h")
        return
    action = context.args[0].lower()
    s['warn_action'] = action
    if action in ('tempban', 'mute'):
        dur = parse_duration(context.args[1]) if len(context.args) > 1 else None
        s['warn_action_duration'] = dur or 86400
    save_channel_settings(chat_id, channel)
    extra = f" ({human_duration(s['warn_action_duration'])})" if action in ('tempban', 'mute') else ""
    await update.effective_message.reply_text(f"✅ Uyarı limiti dolunca uygulanacak ceza: {action}{extra}")

async def cmd_yeniuye(update: Update, context):
    """/yeniuye 10  → yeni üyeler ilk 10 dk link/medya/forward atamaz. /yeniuye off"""
    chat_id, channel = await _require_settings_admin(update, context)
    if not chat_id:
        return
    if not context.args:
        cur = int(channel['settings'].get('newbie_minutes', 0) or 0)
        await update.effective_message.reply_text(
            f"Yeni üye kısıtlaması: {'KAPALI' if cur == 0 else f'{cur} dakika'}\nKullanım: /yeniuye <dakika> veya /yeniuye off")
        return
    arg = context.args[0].lower()
    minutes = 0 if arg in ('off', 'kapat', '0') else (int(arg) if arg.isdigit() else -1)
    if minutes < 0 or minutes > 1440:
        await update.effective_message.reply_text("0-1440 arası dakika gir veya off yaz.")
        return
    channel['settings']['newbie_minutes'] = minutes
    save_channel_settings(chat_id, channel)
    await update.effective_message.reply_text(
        "✅ Yeni üye kısıtlaması kapatıldı." if minutes == 0 else f"✅ Yeni üyeler ilk {minutes} dk link/medya/forward gönderemez.")

async def cmd_linkizin(update: Update, context):
    """/linkizin ekle youtube.com | /linkizin sil youtube.com | /linkizin"""
    chat_id, channel = await _require_settings_admin(update, context)
    if not chat_id:
        return
    wl = channel['settings'].setdefault('link_whitelist', [])
    if len(context.args) < 2 or context.args[0].lower() not in ('ekle', 'sil'):
        await update.effective_message.reply_text(
            "🔗 Link muaf listesi:\n" + ("\n".join(f"• {d}" for d in wl) if wl else "(boş)") +
            "\n\nKullanım:\n/linkizin ekle youtube.com\n/linkizin sil youtube.com\n(t.me/kanalim gibi yol da eklenebilir)")
        return
    item = context.args[1].lower().strip()
    item = re.sub(r'^[a-z]+://', '', item)
    item = item[4:] if item.startswith('www.') else item
    if context.args[0].lower() == 'ekle':
        if item not in wl:
            wl.append(item)
        msg = f"✅ {item} link muaf listesine eklendi."
    else:
        if item in wl:
            wl.remove(item)
        msg = f"✅ {item} listeden çıkarıldı."
    save_channel_settings(chat_id, channel)
    await update.effective_message.reply_text(msg)

async def cmd_captchasure(update: Update, context):
    """/captchasure 3m"""
    chat_id, channel = await _require_settings_admin(update, context)
    if not chat_id:
        return
    dur = parse_duration(context.args[0]) if context.args else None
    if not dur or not (30 <= dur <= 3600):
        await update.effective_message.reply_text("Kullanım: /captchasure 2m (30sn - 60dk arası)")
        return
    channel['settings']['captcha_timeout'] = dur
    save_channel_settings(chat_id, channel)
    await update.effective_message.reply_text(f"✅ Captcha süresi: {human_duration(dur)}")

# ── Ban itiraz sistemi ──
APPEAL_COOLDOWN = 24 * 3600

async def cmd_itiraz(update: Update, context):
    """Banlanan kullanıcı özelden: /itiraz <açıklama>"""
    msg = update.effective_message
    if msg.chat.type != 'private':
        await msg.reply_text("İtiraz için bana özelden yaz: /itiraz <açıklama>")
        return
    user = update.effective_user
    text = msg.text.split(maxsplit=1)[1].strip() if len(msg.text.split(maxsplit=1)) > 1 else ''
    if not text:
        await msg.reply_text("Kullanım: /itiraz <neden banın kaldırılmalı?>")
        return
    with get_db() as conn:
        bans = conn.execute("SELECT chat_id, reason FROM ban_list WHERE user_id = ?", (user.id,)).fetchall()
    if not bans:
        await msg.reply_text("Kayıtlı bir banın bulunmuyor.")
        return
    context.user_data['appeal_text'] = text[:1000]
    if len(bans) == 1:
        await _submit_appeal(update, context, bans[0]['chat_id'])
        return
    kb = []
    for b in bans[:10]:
        try:
            title = (await bot.get_chat(b['chat_id'])).title or b['chat_id']
        except Exception:
            title = b['chat_id']
        kb.append([InlineKeyboardButton(title[:40], callback_data=f"appealpick|{b['chat_id']}")])
    await msg.reply_text("Hangi grup için itiraz ediyorsun?", reply_markup=InlineKeyboardMarkup(kb))

async def _submit_appeal(update: Update, context, chat_id: str):
    user = update.effective_user
    text = context.user_data.pop('appeal_text', '')
    reply = update.effective_message
    now = time.time()
    with get_db() as conn:
        last = conn.execute("SELECT created_at FROM appeals WHERE chat_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 1",
                            (chat_id, user.id)).fetchone()
        ban = conn.execute("SELECT reason FROM ban_list WHERE chat_id = ? AND user_id = ?", (chat_id, user.id)).fetchone()
    if last and now - last['created_at'] < APPEAL_COOLDOWN:
        await reply.reply_text("Bu grup için 24 saatte bir itiraz edebilirsin.")
        return
    async with _db_lock:
        with get_db() as conn:
            cur = conn.execute("INSERT INTO appeals (chat_id, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                               (chat_id, user.id, text, now))
            appeal_id = cur.lastrowid
            conn.commit()
    kb = InlineKeyboardMarkup([[
        ibtn("✅ Banı kaldır", f"appeal|ok|{appeal_id}", GREEN),
        ibtn("❌ Reddet", f"appeal|no|{appeal_id}", RED),
    ]])
    managers_text = (
        f"📨 <b>Ban itirazı</b> #{appeal_id}\n"
        f"Grup: <code>{chat_id}</code>\n"
        f"Kullanıcı: {mention(user)} (<code>{user.id}</code>)\n"
        f"Ban sebebi: {html.escape((ban['reason'] if ban else '') or '-')}\n\n"
        f"💬 {html.escape(text)}"
    )
    for uid in get_channel_managers(chat_id):
        try:
            await bot.send_message(uid, managers_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    await reply.reply_text("✅ İtirazın yöneticilere iletildi. Sonuç sana buradan bildirilecek.")

async def appeal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('|')
    if parts[0] == 'appealpick':
        await query.answer()
        if not context.user_data.get('appeal_text'):
            await query.edit_message_text("Süre doldu, /itiraz komutunu tekrar yaz.")
            return
        await query.edit_message_text("Gönderiliyor...")
        await _submit_appeal(update, context, parts[1])
        return
    decision, appeal_id = parts[1], int(parts[2])
    with get_db() as conn:
        ap = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
    if not ap:
        await query.answer("İtiraz bulunamadı.", show_alert=True)
        return
    chat_id, target = ap['chat_id'], ap['user_id']
    if not await require(update, chat_id, 'can_requests'):
        return
    if ap['status'] != 'pending':
        await query.answer(f"Bu itiraz zaten işlendi: {ap['status']}", show_alert=True)
        return
    await query.answer()
    status = 'approved' if decision == 'ok' else 'rejected'
    async with _db_lock:
        with get_db() as conn:
            conn.execute("UPDATE appeals SET status = ?, handled_by = ? WHERE id = ?", (status, query.from_user.id, appeal_id))
            if status == 'approved':
                conn.execute("DELETE FROM ban_list WHERE chat_id = ? AND user_id = ?", (chat_id, target))
                conn.execute("DELETE FROM temp_bans WHERE chat_id = ? AND user_id = ?", (chat_id, target))
                conn.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, target))
            conn.commit()
    if status == 'approved':
        try:
            await bot.unban_chat_member(chat_id, target, only_if_banned=True)
        except Exception as e:
            logger.debug(f"İtiraz unban hatası: {e}")
        user_msg = "✅ İtirazın kabul edildi, banın kaldırıldı. Gruba tekrar katılabilirsin."
    else:
        user_msg = "❌ İtirazın reddedildi."
    try:
        await bot.send_message(target, user_msg)
    except Exception:
        pass
    try:
        await query.edit_message_text(
            query.message.text_html + f"\n\n<b>Sonuç:</b> {'✅ Kabul' if status == 'approved' else '❌ Red'} — {mention(query.from_user)}",
            parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await send_log(chat_id, f"📨 İtiraz #{appeal_id} {status} | {mention(query.from_user)}", ParseMode.HTML)

# ═══════════════════════════ ZENGİN MESAJ (medya + butonlar + biçim + değişkenler) ═══════════════════════════
# Hoş geldin, veda, kurallar, notlar, /filter ve zamanlanmış mesajlar aynı biçimi kullanır:
#   {'v': [{'text': HTML, 'buttons': [[{'t': etiket, 'k': url|rules|note|popup, 'v': değer, 'c': renk}]]}], 'm': medya}
# Buton yazımı (satır satır, && ile yan yana):  Kanalımız - https://t.me/kanal && Kurallar - rules
# Rose tarzı da olur: [Kanal](buttonurl://t.me/kanal)  [Yan](buttonurl://t.me/x:same)
# Birden fazla mesaj (rastgele seçilir): aralarına tek başına %%% satırı.
RICH_MEDIA = ('photo', 'video', 'animation', 'document', 'audio', 'voice', 'sticker', 'video_note')
RICH_NO_CAPTION = ('sticker', 'video_note')
RICH_MAX_VARIANTS = 10
BTN_COLORS = {'yeşil': GREEN, 'yesil': GREEN, 'green': GREEN, 'kırmızı': RED, 'kirmizi': RED, 'red': RED,
              'mavi': BLUE, 'blue': BLUE}
_ROSE_BTN = re.compile(r'\[([^\[\]\n]{1,64})\]\(buttonurl:(?://)?([^)\s]+?)(:same)?\)')
_VARIANT_SEP = re.compile(r'^[ \t]*%%%[ \t]*$', re.M)
_DOMAIN_URL = re.compile(r'^[\w-]+(\.[\w-]+)*\.[a-zA-Z]{2,}(/\S*)?$')

def _norm_url(u: str) -> str | None:
    u = (u or '').strip()
    if re.match(r'^(https?|tg)://\S+$', u, re.I):
        return u
    if re.fullmatch(r'@\w{4,32}', u):
        return f"https://t.me/{u[1:]}"
    if _DOMAIN_URL.match(u):
        return f"https://{u}"
    return None

def _make_btn(label: str, target: str, color: str | None = None) -> dict | None:
    label, target = (label or '').strip()[:64], (target or '').strip()
    if not label or not target:
        return None
    c = BTN_COLORS.get((color or '').lower())
    tl = target.lower()
    if tl in ('rules', 'kurallar', 'kural'):
        return {'t': label, 'k': 'rules', 'v': '', 'c': c}
    if tl.startswith(('popup:', 'uyari:', 'uyarı:')):
        txt = target.split(':', 1)[1].strip()
        return {'t': label, 'k': 'popup', 'v': txt[:190], 'c': c} if txt else None
    if tl.startswith(('note:', 'not:')) or target.startswith('#'):
        name = target.split(':', 1)[1] if ':' in target else target
        name = name.strip().lstrip('#').lower()
        return {'t': label, 'k': 'note', 'v': name, 'c': c} if _NOTE_NAME.match(name) else None
    url = _norm_url(target)
    return {'t': label, 'k': 'url', 'v': url, 'c': c} if url else None

def _parse_btn_part(part: str) -> dict | None:
    """'Etiket - hedef [#renk]' → buton. Popup metninde ' - ' olabilir, o yüzden önce popup aranır."""
    color = None
    m = re.search(r'\s+#(\w+)\s*$', part)
    if m and m.group(1).lower() in BTN_COLORS:
        color, part = m.group(1), part[:m.start()]
    m = re.match(r'^(.+?)\s+-\s+((?:popup|uyar[ıi]):.+)$', part, re.I | re.S)
    if m:
        return _make_btn(m.group(1), m.group(2), color)
    if ' - ' not in part:
        return None
    label, target = part.rsplit(' - ', 1)
    return _make_btn(label, target, color)

def parse_buttons(text_html: str):
    """Metindeki buton satırlarını ve Rose tarzı butonları ayıklar. Dönüş: (temiz HTML, buton satırları)."""
    rows, kept = [], []
    for line in (text_html or '').split('\n'):
        plain = html.unescape(line).strip()
        if plain and '<' not in line:
            btns = [_parse_btn_part(p.strip()) for p in plain.split('&&')]
            if all(btns):
                rows.append(btns)
                continue
        kept.append(line)
    text = '\n'.join(kept)
    for m in _ROSE_BTN.finditer(text):
        b = _make_btn(html.unescape(m.group(1)), html.unescape(m.group(2)))
        if not b:
            continue
        if m.group(3) and rows:
            rows[-1].append(b)
        else:
            rows.append([b])
    text = _ROSE_BTN.sub('', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    rows = [r[:8] for r in rows][:20]
    return text, rows

def build_rich(text_html: str, media: dict | None = None) -> dict:
    """Admin mesajından zengin mesaj: %%% ile ayrılmış her parça bir seçenek (rastgele gönderilir)."""
    variants = []
    for part in _VARIANT_SEP.split(text_html or ''):
        t, rows = parse_buttons(part.strip())
        if t or rows:
            variants.append({'text': t[:4000], 'buttons': rows})
    return {'v': variants[:RICH_MAX_VARIANTS] or [{'text': '', 'buttons': []}], 'm': media}

def rich_from_plain(text: str) -> dict:
    """Eski düz metin ayarı (biçimsiz) → zengin mesaj."""
    return build_rich(html.escape(text or '', quote=False))

def msg_media(m) -> dict | None:
    for ft in RICH_MEDIA:
        obj = getattr(m, ft, None) if m is not None else None
        if obj:
            if ft == 'photo':  # en büyük boyut
                return {'type': ft, 'id': max(obj, key=lambda p: getattr(p, 'width', 0) or 0).file_id}
            return {'type': ft, 'id': obj.file_id}
    return None

def msg_html(m) -> str:
    """Mesajın biçimli HTML'i (metin ya da medya açıklaması)."""
    if m is None:
        return ''
    if getattr(m, 'text', None):
        return getattr(m, 'text_html', None) or html.escape(m.text)
    if getattr(m, 'caption', None):
        return getattr(m, 'caption_html', None) or html.escape(m.caption)
    return ''

def html_after(m, plain_index: int) -> str:
    """Mesajın plain_index karakterinden sonraki kısmının HTML'i (komut ve argümanlar atlanır, biçim korunur)."""
    text = (getattr(m, 'text', None) or getattr(m, 'caption', None) or '')
    th = msg_html(m)
    pre = text[:plain_index]
    for esc in (html.escape(pre), html.escape(pre, quote=False)):
        if th.startswith(esc):
            return th[len(esc):].strip()
    return html.escape(text[plain_index:], quote=False).strip()

def cmd_args_html(m, skip: int = 1) -> str:
    """Komuttan (ve ilk skip-1 argümandan) sonraki metnin HTML'i; satır sonları korunur."""
    text = (getattr(m, 'text', None) or getattr(m, 'caption', None) or '')
    mt = re.match(r'^\s*' + r'\S+(?:\s+|$)' * skip, text)
    return html_after(m, mt.end() if mt else len(text))

def rich_plain(rich: dict | None, limit: int = 300) -> str:
    """Panel önizlemesi için ilk seçeneğin düz metni."""
    v = ((rich or {}).get('v') or [{}])[0]
    t = html.unescape(re.sub(r'<[^>]+>', '', v.get('text') or ''))
    return t[:limit] + ('…' if len(t) > limit else '')

def rich_summary(rich: dict | None) -> str:
    rich = rich or {}
    media = rich.get('m')
    nb = sum(len(r) for v in rich.get('v') or [] for r in v.get('buttons') or [])
    parts = [f"🖼 {RICH_MEDIA_LABELS.get(media['type'], media['type'])}" if media else "🖼 medya yok",
             f"🔘 {nb} buton", f"🎲 {len(rich.get('v') or [])} seçenek"]
    return " · ".join(parts)

RICH_MEDIA_LABELS = {'photo': "Fotoğraf", 'video': "Video", 'animation': "GIF", 'document': "Dosya", 'audio': "Müzik",
                     'voice': "Ses", 'sticker': "Sticker", 'video_note': "Yuvarlak video"}

def fill_rich_vars(text: str, users=(), title: str | None = None, count=None) -> str:
    """{kullanıcı} {ad} {soyad} {username} {id} {grup} {uye_sayisi} {tarih} {saat} — metin HTML'dir, değerler kaçışlanır."""
    users = [u for u in (users or ()) if u is not None]
    now = datetime.now(TZ_TR)
    esc = lambda v: html.escape(str(v or ''), quote=False)  # noqa: E731
    mentions = ", ".join(mention(u) for u in users)
    vals = {
        'kullanıcı': mentions, 'kullanici': mentions, 'user': mentions, 'mention': mentions,
        'ad': esc(", ".join(getattr(u, 'first_name', None) or '' for u in users)),
        'isim': esc(", ".join(getattr(u, 'first_name', None) or '' for u in users)),
        'soyad': esc(", ".join(getattr(u, 'last_name', None) or '' for u in users if getattr(u, 'last_name', None))),
        'username': esc(", ".join(f"@{u.username}" if getattr(u, 'username', None) else (getattr(u, 'first_name', None) or '')
                                  for u in users)),
        'id': ", ".join(str(u.id) for u in users),
        'grup': esc(title), 'kanal': esc(title), 'chat': esc(title), 'group': esc(title),
        'uye_sayisi': esc(count if count is not None else '?'), 'üye_sayısı': esc(count if count is not None else '?'),
        'tarih': now.strftime('%d.%m.%Y'), 'saat': now.strftime('%H:%M'),
    }
    return re.sub(r'\{(\w+)\}', lambda m: vals.get(m.group(1).lower(), m.group(0)), text or '')

def rich_action_id(chat_id: str, kind: str, value: str) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM rich_actions WHERE chat_id = ? AND kind = ? AND value = ?",
                           (str(chat_id), kind, value)).fetchone()
        if row:
            return row['id']
        rid = conn.execute("INSERT INTO rich_actions (chat_id, kind, value) VALUES (?, ?, ?)",
                           (str(chat_id), kind, value)).lastrowid
        conn.commit()
        return rid

def rich_markup(chat_id: str, rows: list) -> InlineKeyboardMarkup | None:
    out = []
    for row in rows or []:
        r = []
        for b in row:
            if b['k'] == 'url':
                r.append(ibtn(b['t'], url=b['v'], style=b.get('c')))
            else:  # kurallar / not / popup: ra|id → bot özelden gösterir ya da uyarı penceresi açar
                r.append(ibtn(b['t'], f"ra|{rich_action_id(chat_id, b['k'], b['v'])}", b.get('c')))
        if r:
            out.append(r)
    return InlineKeyboardMarkup(out) if out else None

def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', '', text or ''))

async def send_rich(chat_id, rich: dict | None, *, reply_msg=None, users=(), title: str | None = None,
                    header: str = '', count=None, thread_id=None, variant: int | None = None) -> list:
    """Zengin mesajı gönderir (rastgele seçenek, değişkenler, medya, butonlar). Biçim/buton hatasında sadeleştirip
    yeniden dener. Dönüş: gönderilen mesajlar."""
    rich = rich or {}
    variants = rich.get('v') or [{'text': '', 'buttons': []}]
    var = variants[variant % len(variants)] if variant is not None else random.choice(variants)
    text = (header or '') + fill_rich_vars(var.get('text') or '', users, title, count)
    markup = rich_markup(str(chat_id), var.get('buttons') or [])
    media = rich.get('m')
    if not text.strip() and not media:
        if not markup:
            return []
        text = "👇"

    async def _send(kind, payload, caption, mk, as_html=True):
        kw = {'reply_markup': mk} if mk is not None else {}
        if thread_id and reply_msg is None:
            kw['message_thread_id'] = thread_id
        if kind == 'text':
            kw['disable_web_page_preview'] = True
            if as_html:
                kw['parse_mode'] = ParseMode.HTML
            if reply_msg is not None:
                return await reply_msg.reply_text(payload, **kw)
            return await bot.send_message(chat_id, payload, **kw)
        if caption and kind not in RICH_NO_CAPTION:
            kw['caption'] = caption
            if as_html:
                kw['parse_mode'] = ParseMode.HTML
        if reply_msg is not None:
            return await getattr(reply_msg, f"reply_{kind}")(payload, **kw)
        return await getattr(bot, f"send_{kind}")(chat_id, payload, **kw)

    async def _safe(kind, payload, caption, mk):
        try:
            return await _send(kind, payload, caption, mk)
        except BadRequest as e:
            err = str(e).lower()
            logger.debug(f"Zengin mesaj sadeleştiriliyor {chat_id}: {e}")
            if mk is not None and any(w in err for w in ('button', 'url', 'keyboard', 'markup')):
                mk = None
            if kind == 'text':
                payload = _strip_custom_emoji_tags(payload)
            elif caption:
                caption = _strip_custom_emoji_tags(caption)
        try:
            return await _send(kind, payload, caption, mk)
        except BadRequest as e:
            logger.debug(f"Zengin mesaj düz metne düşüyor {chat_id}: {e}")
        if kind == 'text':
            return await _send('text', _strip_html(payload), None, None, as_html=False)
        return await _send(kind, payload, _strip_html(caption) if caption else None, None, as_html=False)

    sent = []
    try:
        if not media:
            sent.append(await _safe('text', text, None, markup))
        elif media['type'] in RICH_NO_CAPTION or len(_strip_html(text)) > 1024:
            sent.append(await _safe(media['type'], media['id'], None, None if text.strip() else markup))
            if text.strip():
                sent.append(await _safe('text', text, None, markup))
        else:
            sent.append(await _safe(media['type'], media['id'], text if text.strip() else None, markup))
    except TelegramError as e:
        logger.debug(f"Zengin mesaj gönderilemedi {chat_id}: {e}")
    return [m for m in sent if m is not None]

def schedule_delete(chat_id, messages, seconds: int):
    """Mesajları seconds sonra siler (veritabanında tutulur; bot yeniden başlasa da silinir)."""
    ids = [getattr(m, 'message_id', m) for m in messages or []]
    ids = [i for i in ids if isinstance(i, int)]
    if not ids or seconds <= 0:
        return
    with get_db() as conn:
        conn.executemany("INSERT OR REPLACE INTO auto_delete (chat_id, message_id, delete_at) VALUES (?, ?, ?)",
                         [(str(chat_id), i, time.time() + seconds) for i in ids])
        conn.commit()

async def auto_delete_job(context):
    now = time.time()
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id, message_id FROM auto_delete WHERE delete_at <= ? LIMIT 200", (now,)).fetchall()
        conn.executemany("DELETE FROM auto_delete WHERE chat_id = ? AND message_id = ?",
                         [(r['chat_id'], r['message_id']) for r in rows])
        conn.commit()
    by_chat: dict = {}
    for r in rows:
        by_chat.setdefault(r['chat_id'], []).append(r['message_id'])
    for cid, ids in by_chat.items():
        for i in range(0, len(ids), 100):
            try:
                await bot.delete_messages(cid, ids[i:i + 100])
            except Exception as e:
                logger.debug(f"Otomatik silme {cid}: {e}")

async def rich_action_callback(update: Update, context):
    """ra|id — popup uyarısı; kurallar / not ise bot özelden açılır (Telegram'ın start bağlantısıyla)."""
    query = update.callback_query
    try:
        aid = int(query.data.split('|')[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    with get_db() as conn:
        row = conn.execute("SELECT * FROM rich_actions WHERE id = ?", (aid,)).fetchone()
    if not row:
        await query.answer("Bu buton artık geçerli değil.", show_alert=True)
        return
    if row['kind'] == 'popup':
        await query.answer(row['value'][:200], show_alert=True)
        return
    await query.answer(url=f"https://t.me/{context.bot.username}?start=ra_{aid}")

async def show_rich_action(update: Update, context, aid: str) -> None:
    """/start ra_<id> — kurallar ya da notu özelden gösterir."""
    msg = update.effective_message
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM rich_actions WHERE id = ?", (int(aid),)).fetchone()
    except ValueError:
        row = None
    if not row:
        await msg.reply_text("Bu bağlantı artık geçerli değil.")
        return
    if row['kind'] == 'rules':
        await send_rules(row['chat_id'], msg, update.effective_user)
    elif row['kind'] == 'note':
        if not await _send_note(row['chat_id'], row['value'], msg):
            await msg.reply_text("Bu not artık yok.")
    else:
        await msg.reply_text(html.escape(row['value']), parse_mode=ParseMode.HTML)

# ── Kayıtlı zengin mesajlar (ayarlarda) ──
def welcome_rich(s: dict) -> dict:
    return s.get('welcome_rich') or rich_from_plain(s.get('welcome_msg') or _default_channel_settings()['welcome_msg'])

GOODBYE_DEFAULT = "👋 {ad} aramızdan ayrıldı. Yolun açık olsun!"

def goodbye_rich(s: dict) -> dict:
    return s.get('goodbye_rich') or rich_from_plain(GOODBYE_DEFAULT)

def rules_rich(s: dict) -> dict | None:
    if s.get('rules_rich'):
        return s['rules_rich']
    rules = (s.get('rules') or '').strip()
    return rich_from_plain(rules) if rules else None

def store_rich(s: dict, kind: str, rich: dict):
    """kind: welcome / goodbye / rules — düz metin karşılığı da (eski ekranlar, ağ kopyası) güncellenir."""
    plain = rich_plain(rich, 4000)
    if kind == 'welcome':
        s['welcome_rich'], s['welcome_msg'] = rich, plain[:1000]
    elif kind == 'goodbye':
        s['goodbye_rich'] = rich
    elif kind == 'rules':
        s['rules_rich'], s['rules'] = rich, plain[:3500]

async def send_rules(chat_id: str, reply_msg, user=None) -> bool:
    channel = get_channel_settings(chat_id)
    rich = rules_rich(channel['settings']) if channel else None
    if not rich:
        await reply_msg.reply_text("Bu grupta henüz kural belirlenmemiş.")
        return False
    title = await _chat_title(chat_id)
    await send_rich(reply_msg.chat_id, rich, reply_msg=reply_msg, users=[user] if user else (),
                    title=title, header=f"📜 <b>{html.escape(title)} Kuralları</b>\n\n", variant=0)
    return True

def rich_from_command(msg, skip: int = 1) -> dict | None:
    """/setwelcome metin  ·  medyaya/mesaja yanıt: /setwelcome [açıklama]  → zengin mesaj (None: içerik yok)."""
    text_html = cmd_args_html(msg, skip)
    reply = getattr(msg, 'reply_to_message', None)
    media = msg_media(reply) if reply else None
    if reply and not text_html:
        text_html = msg_html(reply)
    if not text_html and not media:
        return None
    return build_rich(text_html, media)

RICH_HELP = (
    "<b>Biçim:</b> mesajı Telegram'da nasıl yazarsan (kalın, italik, link, spoiler, alıntı) öyle kaydedilir.\n"
    "<b>Medya:</b> fotoğraf/video/GIF/sticker'a yanıt verip komutu yaz.\n"
    "<b>Butonlar</b> (her satır bir sıra, <code>&amp;&amp;</code> ile yan yana):\n"
    "<code>Kanalımız - https://t.me/kanal &amp;&amp; Destek - @destek</code>\n"
    "<code>Kuralları oku - rules</code> · <code>Bilgi - popup:Metin</code> · <code>Not - #isim</code>\n"
    "Renk: satır sonuna <code>#yeşil</code> <code>#kırmızı</code> <code>#mavi</code> · "
    "Rose tarzı: <code>[Kanal](buttonurl://t.me/kanal)</code>\n"
    "<b>Değişkenler:</b> <code>{kullanıcı}</code> <code>{ad}</code> <code>{soyad}</code> <code>{username}</code> "
    "<code>{id}</code> <code>{grup}</code> <code>{uye_sayisi}</code> <code>{tarih}</code> <code>{saat}</code>\n"
    "<b>Rastgele:</b> birden fazla mesajı tek başına <code>%%%</code> satırıyla ayır.")

# ── Hoş geldin / veda gönderimi ──
WELCOME_BATCH_DELAY = 4      # sn: bu süre içinde katılanlar tek mesajda karşılanır
_welcome_buffer: dict = {}   # chat_id -> {'users': [...], 'reply': mesaj, 'title': ad}
_last_welcome: dict = {}     # chat_id -> son hoş geldin mesaj ID'leri (eskisini silmek için)

async def send_welcome(chat_id: str, users: list, reply_msg=None, title: str | None = None) -> list:
    """Ayarlara göre hoş geldin: özelden (olmazsa grupta), eskisini sil, X dk sonra sil."""
    channel = get_channel_settings(chat_id)
    if not channel or not users:
        return []
    s = channel['settings']
    if not s.get('welcome_enabled', True):
        return []
    rich = welcome_rich(s)
    title = title or await _chat_title(chat_id)
    try:
        count = await bot.get_chat_member_count(chat_id)
    except Exception:
        count = None
    if s.get('welcome_dm'):
        left = []
        for u in users:
            if not await send_rich(u.id, rich, users=[u], title=title, count=count):
                left.append(u)  # bota hiç yazmamış kişiye özelden mesaj gidemez: grupta karşılanır
        users = left
        if not users:
            return []
    if s.get('welcome_clean') and _last_welcome.get(chat_id):
        try:
            await bot.delete_messages(chat_id, _last_welcome.pop(chat_id))
        except Exception as e:
            logger.debug(f"Eski hoş geldin silinemedi {chat_id}: {e}")
    sent = await send_rich(chat_id, rich, reply_msg=reply_msg, users=users, title=title, count=count,
                           thread_id=getattr(reply_msg, 'message_thread_id', None))
    if sent:
        _last_welcome[chat_id] = [m.message_id for m in sent if getattr(m, 'message_id', None)]
        schedule_delete(chat_id, sent, int(s.get('welcome_autodel') or 0) * 60)
    return sent

async def _welcome_flush_job(context):
    chat_id = context.job.data
    buf = _welcome_buffer.pop(chat_id, None)
    if buf:
        await send_welcome(chat_id, buf['users'], None, buf['title'])

async def queue_welcome(chat_id: str, users: list, msg, context):
    """Toplu katılımda tek mesaj: kısa süre içinde gelenler biriktirilip birlikte karşılanır."""
    if not users:
        return
    channel = get_channel_settings(chat_id)
    jq = getattr(context, 'job_queue', None)
    if not channel or not channel['settings'].get('welcome_batch', True) or jq is None:
        await send_welcome(chat_id, users, msg if len(users) == 1 else None, getattr(msg.chat, 'title', None))
        return
    buf = _welcome_buffer.get(chat_id)
    if buf and time.time() - buf['at'] < WELCOME_BATCH_DELAY + 30:
        buf['users'] += [u for u in users if u.id not in {x.id for x in buf['users']}]
        return
    # (iş çalışmadan kalmış eski tampon varsa yenisiyle değiştirilir; kimse karşılamasız kalmaz)
    _welcome_buffer[chat_id] = {'users': (buf['users'] if buf else []) + list(users),
                                'title': getattr(msg.chat, 'title', None), 'at': time.time()}
    jq.run_once(_welcome_flush_job, WELCOME_BATCH_DELAY, data=chat_id, name=f"welcome_{chat_id}")

async def left_member_handler(update: Update, context):
    """Veda mesajı: kendi isteğiyle çıkanlara (atılan/banlananlara değil)."""
    msg = update.message
    if not msg or not msg.left_chat_member:
        return
    user = msg.left_chat_member
    if user.is_bot:
        return
    chat_id = str(msg.chat_id)
    with get_db() as conn:
        conn.execute("UPDATE users SET left_at = ? WHERE chat_id = ? AND user_id = ?", (time.time(), chat_id, user.id))
        conn.commit()
    if get_channel_settings(chat_id):
        log_member_event(chat_id, user.id, 'leave')
    channel = get_channel_settings(chat_id)
    if not channel or not channel['settings'].get('goodbye_enabled'):
        return
    if msg.from_user and msg.from_user.id != user.id:
        return
    s = channel['settings']
    sent = await send_rich(chat_id, goodbye_rich(s), users=[user], title=msg.chat.title,
                           thread_id=getattr(msg, 'message_thread_id', None))
    schedule_delete(chat_id, sent, int(s.get('welcome_autodel') or 0) * 60)

# ── Komutlar: /setwelcome /setgoodbye /welcome /goodbye /resetwelcome /setrules /rules ──
async def _rich_cmd_target(update: Update, context, perm: str = 'can_content'):
    chat_id = _get_effective_chat_id(update, context)
    msg = update.effective_message
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return None, None
    channel = get_channel_settings(chat_id)
    if channel['chat_type'] == 'channel':
        await msg.reply_text("Kanallarda bu mesaj kullanılamaz.")
        return None, None
    if not await require(update, chat_id, perm):
        return None, None
    return chat_id, channel

async def _set_rich_cmd(update: Update, context, kind: str):
    chat_id, channel = await _rich_cmd_target(update, context)
    if not chat_id:
        return
    msg = update.effective_message
    rich = rich_from_command(msg)
    label = {'welcome': "Hoş geldin", 'goodbye': "Veda", 'rules': "Kurallar"}[kind]
    if not rich:
        cmd = {'welcome': 'setwelcome', 'goodbye': 'setgoodbye', 'rules': 'setrules'}[kind]
        await msg.reply_text(
            f"✏️ <b>{label} mesajı</b>\nKullanım: <code>/{cmd} metin</code> ya da bir mesaja/medyaya yanıt: "
            f"<code>/{cmd}</code>\n\n{RICH_HELP}", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    s = channel['settings']
    store_rich(s, kind, rich)
    if kind == 'welcome':
        s['welcome_enabled'] = True
    elif kind == 'goodbye':
        s['goodbye_enabled'] = True
    save_channel_settings(chat_id, channel)
    await msg.reply_text(f"✅ {label} mesajı kaydedildi ({rich_summary(rich)}). Önizleme:")
    await send_rich(msg.chat_id, rich, reply_msg=msg, users=[update.effective_user], title=await _chat_title(chat_id))
    await send_log(chat_id, f"✏️ {label} mesajı güncellendi | {mention(update.effective_user)}", ParseMode.HTML)

async def set_welcome(update: Update, context):
    await _set_rich_cmd(update, context, 'welcome')

async def cmd_setgoodbye(update: Update, context):
    await _set_rich_cmd(update, context, 'goodbye')

async def cmd_setrules(update: Update, context):
    await _set_rich_cmd(update, context, 'rules')

async def cmd_rules(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    msg = update.effective_message
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    if not rules_rich(get_channel_settings(chat_id)['settings']):
        note = "Bu grupta henüz kural belirlenmemiş."
        if has_specific_permission(chat_id, update.effective_user.id, 'can_content'):
            note += "\n/setrules ile ekleyebilirsin."
        await msg.reply_text(note)
        return
    await send_rules(chat_id, msg, update.effective_user)

async def _preview_cmd(update: Update, context, kind: str):
    chat_id, channel = await _rich_cmd_target(update, context)
    if not chat_id:
        return
    s = channel['settings']
    rich = welcome_rich(s) if kind == 'welcome' else goodbye_rich(s)
    on = s.get('welcome_enabled', True) if kind == 'welcome' else s.get('goodbye_enabled')
    msg = update.effective_message
    await msg.reply_text(f"{'👋 Hoş geldin' if kind == 'welcome' else '🚪 Veda'} mesajı: {'Açık' if on else 'Kapalı'} · "
                         f"{rich_summary(rich)}\nDeğiştirmek için /set{'welcome' if kind == 'welcome' else 'goodbye'}")
    await send_rich(msg.chat_id, rich, reply_msg=msg, users=[update.effective_user], title=await _chat_title(chat_id))

async def cmd_welcome(update: Update, context):
    await _preview_cmd(update, context, 'welcome')

async def cmd_goodbye(update: Update, context):
    await _preview_cmd(update, context, 'goodbye')

async def cmd_resetwelcome(update: Update, context):
    chat_id, channel = await _rich_cmd_target(update, context)
    if not chat_id:
        return
    channel['settings'].pop('welcome_rich', None)
    channel['settings']['welcome_msg'] = _default_channel_settings()['welcome_msg']
    save_channel_settings(chat_id, channel)
    await update.effective_message.reply_text("✅ Hoş geldin mesajı varsayılana döndü.")

# ── Zamanlanmış mesajlar ──
SCHED_MIN, SCHED_MAX, SCHED_LIMIT = 10 * 60, 7 * 86400, 10

def parse_interval(text: str) -> int | None:
    """6h / 6sa / 6 saat / 30dk / 30m / 1g / 1d → saniye"""
    m = re.fullmatch(r'(\d{1,4})\s*(m|dk|dak|dakika|h|sa|saat|d|g|gün|gun)', (text or '').strip().lower())
    if not m:
        return None
    unit = m.group(2)
    mult = 60 if unit in ('m', 'dk', 'dak', 'dakika') else 3600 if unit in ('h', 'sa', 'saat') else 86400
    return int(m.group(1)) * mult

def sched_list(chat_id: str) -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM scheduled_msgs WHERE chat_id = ? ORDER BY id", (chat_id,))]

def sched_add(chat_id: str, rich: dict, interval: int, uid: int) -> int:
    with get_db() as conn:
        sid = conn.execute("""INSERT INTO scheduled_msgs (chat_id, rich, interval, next_at, created_by, created_at)
                              VALUES (?, ?, ?, ?, ?, ?)""",
                           (chat_id, json.dumps(rich, ensure_ascii=False), interval, time.time() + interval, uid,
                            time.time())).lastrowid
        conn.commit()
    return sid

def _sched_check(chat_id: str, interval: int | None) -> str | None:
    if interval is None:
        return "Süre biçimi: <code>30dk</code>, <code>6sa</code>, <code>1g</code> (ya da 30m, 6h, 1d)"
    if not SCHED_MIN <= interval <= SCHED_MAX:
        return "Süre en az 10 dakika, en fazla 7 gün olabilir."
    if len(sched_list(chat_id)) >= SCHED_LIMIT:
        return f"Bir grupta en fazla {SCHED_LIMIT} zamanlanmış mesaj olabilir."
    return None

async def cmd_zamanla(update: Update, context):
    """/zamanla 6sa metin  ·  bir mesaja/medyaya yanıt: /zamanla 6sa"""
    chat_id, channel = await _rich_cmd_target(update, context)
    if not chat_id:
        return
    msg = update.effective_message
    interval = parse_interval(context.args[0]) if context.args else None
    rich = rich_from_command(msg, skip=2) if context.args else None
    if not context.args or not rich:
        await msg.reply_text(
            "⏰ <b>Zamanlanmış mesaj</b>\nKullanım: <code>/zamanla 6sa Kuralları okumayı unutmayın!</code>\n"
            "ya da bir mesaja/medyaya yanıt: <code>/zamanla 6sa</code>\nListe ve silme: /zamanlar\n\n" + RICH_HELP,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    err = _sched_check(chat_id, interval)
    if err:
        await msg.reply_text(f"❌ {err}", parse_mode=ParseMode.HTML)
        return
    sid = sched_add(chat_id, rich, interval, update.effective_user.id)
    await msg.reply_text(f"✅ Zamanlanmış mesaj #{sid}: her {human_duration(interval)} bir gönderilecek "
                         f"(ilki {human_duration(interval)} sonra). Liste: /zamanlar")
    await send_log(chat_id, f"⏰ Zamanlanmış mesaj eklendi (her {human_duration(interval)}) | {mention(update.effective_user)}",
                   ParseMode.HTML)

def _sched_data(chat_id: str, op: str, sid: int, panel: bool) -> str:
    return f"s|{chat_id}|{op}|{sid}" if panel else f"zm|{chat_id}|{op}|{sid}"

def render_sched(chat_id: str, panel: bool):
    """panel=True: ayar panelindeki sayfa (s|…), False: /zamanlar mesajı (zm|…) — butonlar aynı işlemleri yapar."""
    items = sched_list(chat_id)
    lines = []
    rows = []
    for it in items:
        rich = json.loads(it['rich'])
        nxt = datetime.fromtimestamp(it['next_at'], TZ_TR).strftime('%d.%m %H:%M')
        lines.append(f"{'🟢' if it['enabled'] else '⏸'} <b>#{it['id']}</b> her {human_duration(it['interval'])} · "
                     f"sonraki {nxt}\n<i>{html.escape(rich_plain(rich, 60)) or rich_summary(rich)}</i>")
        d_toggle, d_view, d_prev, d_del = (_sched_data(chat_id, op, it['id'], panel) for op in ('zt', 'zp', 'zc', 'zd'))
        rows.append([ibtn(f"{'⏸' if it['enabled'] else '▶️'} #{it['id']}", d_toggle, RED if it['enabled'] else GREEN),
                     ibtn("👁", d_view, BLUE),
                     ibtn("🧹 Öncekini sil" if it['delete_prev'] else "📌 Öncekini tut", d_prev),
                     ibtn("🗑", d_del, RED)])
    text = ("⏰ <b>Zamanlanmış mesajlar</b>\n\n" + ("\n\n".join(lines) if lines else "Henüz yok.") +
            "\n\nEklemek için: <code>/zamanla 6sa mesaj</code> (medya/buton desteklenir)")
    return text, rows

async def sched_op(chat_id: str, op: str, sid: int, query) -> str:
    """zt aç/kapat · zp önizle · zc öncekini sil/tut · zd sil. Dönüş: kısa bildirim."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scheduled_msgs WHERE id = ? AND chat_id = ?", (sid, chat_id)).fetchone()
    if not row:
        return "Bulunamadı"
    with get_db() as conn:
        if op == 'zt':
            conn.execute("UPDATE scheduled_msgs SET enabled = ?, next_at = ? WHERE id = ?",
                         (0 if row['enabled'] else 1, time.time() + row['interval'], sid))
            note = "⏸ Durduruldu" if row['enabled'] else "▶️ Başlatıldı"
        elif op == 'zc':
            conn.execute("UPDATE scheduled_msgs SET delete_prev = ? WHERE id = ?", (0 if row['delete_prev'] else 1, sid))
            note = "Kaydedildi"
        elif op == 'zd':
            conn.execute("DELETE FROM scheduled_msgs WHERE id = ?", (sid,))
            note = "🗑 Silindi"
        else:
            note = "👁 Önizleme gönderildi"
        conn.commit()
    if op == 'zp':
        await send_rich(query.message.chat_id, json.loads(row['rich']), users=[query.from_user],
                        title=await _chat_title(chat_id))
    return note

async def cmd_zamanlar(update: Update, context):
    chat_id, channel = await _rich_cmd_target(update, context)
    if not chat_id:
        return
    text, rows = render_sched(chat_id, False)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                              reply_markup=InlineKeyboardMarkup(rows) if rows else None)

async def sched_callback(update: Update, context):
    """zm|cid|op|id — /zamanlar listesindeki butonlar."""
    query = update.callback_query
    parts = query.data.split('|')
    if len(parts) < 4 or not parts[3].isdigit():
        await query.answer()
        return
    chat_id, op, sid = parts[1], parts[2], int(parts[3])
    if not has_specific_permission(chat_id, query.from_user.id, 'can_content'):
        await deny(update, 'can_content')
        return
    note = await sched_op(chat_id, op, sid, query)
    await query.answer(note)
    text, rows = render_sched(chat_id, False)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows) if rows else None)
    except BadRequest:
        pass

async def scheduled_msgs_job(context):
    now = time.time()
    with get_db() as conn:
        due = [dict(r) for r in conn.execute(
            "SELECT * FROM scheduled_msgs WHERE enabled = 1 AND next_at <= ? ORDER BY next_at LIMIT 50", (now,))]
    for it in due:
        chat_id = it['chat_id']
        nxt = it['next_at'] + it['interval']
        if nxt <= now:  # bot kapalıyken kaçırılan turlar art arda gönderilmez: bir sonraki tur tam aralık sonra
            nxt = now + it['interval']
        if not get_channel_settings(chat_id):
            with get_db() as conn:
                conn.execute("DELETE FROM scheduled_msgs WHERE id = ?", (it['id'],))
                conn.commit()
            continue
        if it['delete_prev'] and it['last_msg_ids']:
            try:
                await bot.delete_messages(chat_id, json.loads(it['last_msg_ids']))
            except Exception as e:
                logger.debug(f"Önceki zamanlanmış mesaj silinemedi {chat_id}: {e}")
        sent = await send_rich(chat_id, json.loads(it['rich']), title=await _chat_title(chat_id))
        with get_db() as conn:
            conn.execute("UPDATE scheduled_msgs SET next_at = ?, last_msg_ids = ? WHERE id = ?",
                         (nxt, json.dumps([m.message_id for m in sent if getattr(m, 'message_id', None)]), it['id']))
            conn.commit()


# ── Panel: karşılama / veda / zamanlanmış mesaj sayfaları ──
def render_rich_page(cid: str, base: str, s: dict, title: str):
    if base == 'sched':
        text, rows = render_sched(cid, True)
        text = text.replace("⏰ <b>Zamanlanmış mesajlar</b>", f"⏰ <b>Zamanlanmış mesajlar</b> — {title}", 1)
        rows = rows + [[ibtn("➕ Yeni zamanlanmış mesaj", f"s|{cid}|i|sched", GREEN)], _back(cid, 'welcome')]
        return text, rows
    if base == 'goodbye':
        g = goodbye_rich(s)
        text = (f"🚪 <b>Veda mesajı</b> — {title}\n\nDurum: <b>{'Açık' if s.get('goodbye_enabled') else 'Kapalı'}</b> · "
                f"{rich_summary(g)}\n<blockquote expandable>{html.escape(rich_plain(g, 500)) or '—'}</blockquote>\n"
                "Sadece kendi isteğiyle çıkanlara gönderilir (atılan/banlanana değil). "
                "Otomatik silme süresi hoş geldinle aynıdır.")
        rows = [[toggle_btn("Veda mesajı", s.get('goodbye_enabled'), f"s|{cid}|t|goodbye_enabled|goodbye")],
                [ibtn("✏️ Değiştir", f"s|{cid}|i|goodbye", BLUE), ibtn("👁 Önizle", f"s|{cid}|pv|goodbye", BLUE)],
                ([ibtn("🖼 Medyayı kaldır", f"s|{cid}|wm|goodbye", RED)] if g.get('m') else []) +
                ([ibtn("↩️ Varsayılana dön", f"s|{cid}|wr|goodbye")] if s.get('goodbye_rich') else []),
                _back(cid, 'welcome')]
        return text, [r for r in rows if r]
    w = welcome_rich(s)
    rules = rules_rich(s)
    text = (f"👋 <b>Karşılama</b> — {title}\n\nHoş geldin: <b>{'Açık' if s.get('welcome_enabled', True) else 'Kapalı'}</b> · "
            f"{rich_summary(w)}\n<blockquote expandable>{html.escape(rich_plain(w, 500)) or '—'}</blockquote>\n"
            f"📜 Kurallar: {'var' if rules else 'yok'} · 🚪 Veda: {'açık' if s.get('goodbye_enabled') else 'kapalı'} · "
            f"⏰ Zamanlı mesaj: {len(sched_list(cid))}\n\nMedya, buton ve değişkenler için ✏️ Değiştir'e bas "
            "(ya da grupta /setwelcome yazıp yardımına bak).")
    rows = [[toggle_btn("Hoş geldin mesajı", s.get('welcome_enabled', True), f"s|{cid}|t|welcome_enabled|welcome")],
            [ibtn("✏️ Değiştir", f"s|{cid}|i|welcome", BLUE), ibtn("👁 Önizle", f"s|{cid}|pv|welcome", BLUE)],
            ([ibtn("🖼 Medyayı kaldır", f"s|{cid}|wm|welcome", RED)] if w.get('m') else []) +
            ([ibtn("↩️ Varsayılana dön", f"s|{cid}|wr|welcome")] if s.get('welcome_rich') else []),
            [toggle_btn("🧹 Eskisini sil", s.get('welcome_clean'), f"s|{cid}|t|welcome_clean|welcome"),
             toggle_btn("👥 Toplu tek mesaj", s.get('welcome_batch', True), f"s|{cid}|t|welcome_batch|welcome")],
            _num_row(cid, 'welcome_autodel', s, 'welcome', "⏱ Otomatik sil"),
            [toggle_btn("📩 Özelden gönder", s.get('welcome_dm'), f"s|{cid}|t|welcome_dm|welcome")],
            [ibtn("📜 Kuralları yaz", f"s|{cid}|i|rules", BLUE)] +
            ([ibtn("👁 Kurallar", f"s|{cid}|pv|rules", BLUE), ibtn("🗑", f"s|{cid}|rx", RED)] if rules else []),
            [ibtn("🚪 Veda mesajı ›", f"s|{cid}|p|goodbye", BLUE), ibtn("⏰ Zamanlı mesajlar ›", f"s|{cid}|p|sched", BLUE)],
            _back(cid)]
    return text, [r for r in rows if r]

async def rich_panel_op(cid: str, channel: dict, op: str, args: list, query):
    """pv önizle · wr varsayılana dön · wm medyayı kaldır · zt/zp/zc/zd zamanlanmış mesaj. Dönüş: (sayfa, bildirim)"""
    s = channel['settings']
    if op == 'ba':
        if args and args[0] in BLACKLIST_ACTIONS:
            s['blacklist_action'] = args[0]
            save_channel_settings(cid, channel)
        return 'comm', f"Kara liste: {BLACKLIST_ACTIONS.get(s.get('blacklist_action', 'mute'))}"
    if op == 'ts':
        if args and args[0] in ('name', 'emoji'):
            s['tag_style'] = args[0]
            save_channel_settings(cid, channel)
        return 'tag', "Kaydedildi"
    if op in ('zt', 'zp', 'zc', 'zd'):
        if not args or not args[0].isdigit():
            return 'sched', ''
        return 'sched', await sched_op(cid, op, int(args[0]), query)
    kind = args[0] if args else ''
    page = 'goodbye' if kind == 'goodbye' else 'welcome'
    if kind not in ('welcome', 'goodbye', 'rules'):
        return page, ''
    if op == 'pv':
        if kind == 'rules':
            if not rules_rich(s):
                return page, "Kural yok"
            await send_rules(cid, query.message, query.from_user)
        else:
            rich = welcome_rich(s) if kind == 'welcome' else goodbye_rich(s)
            await send_rich(query.message.chat_id, rich, users=[query.from_user], title=await _chat_title(cid))
        return page, "👁 Önizleme gönderildi"
    key = f"{kind}_rich"
    if op == 'wr':
        s.pop(key, None)
        if kind == 'welcome':
            s['welcome_msg'] = _default_channel_settings()['welcome_msg']
        save_channel_settings(cid, channel)
        return page, "↩️ Varsayılana döndü"
    if op == 'wm':
        rich = welcome_rich(s) if kind == 'welcome' else goodbye_rich(s) if kind == 'goodbye' else rules_rich(s)
        if rich and rich.get('m'):
            rich = {**rich, 'm': None}
            store_rich(s, kind, rich)
            save_channel_settings(cid, channel)
        return page, "🖼 Medya kaldırıldı"
    return page, ''


# ═══════════════════════════ /etiket (toplu etiketleme) ═══════════════════════════
TAG_DELAY = 3.0        # sn: iki etiket mesajı arası (Telegram grupta dakikada ~20 mesaja izin verir)
TAG_COOLDOWN = 600     # sn: bir etiketleme bitince aynı grupta yenisi için bekleme
TAG_MAX = 3000
TAG_EMOJIS = ["😀", "😎", "🥳", "🤩", "😇", "🤠", "🥰", "😺", "🦊", "🐼", "🐯", "🦁", "🐸", "🐵", "🦄", "🐝", "🌸", "🌻",
              "🍀", "🍉", "🍓", "🍒", "🍩", "⚽", "🏀", "🎈", "🎁", "🎯", "🎵", "⭐", "🌙", "🔥", "💎", "🚀", "🌈", "❤️"]
_tag_runs: dict = {}   # chat_id -> {'stop': bool, 'by': uid, 'done': int, 'total': int, 'task': Task}
_tag_last: dict = {}   # chat_id -> son etiketlemenin bittiği zaman

async def tag_targets(chat_id: str, active_only: bool) -> list:
    """Etiketlenecek üyeler [(id, ad)]. Telethon (ana bot) açıksa tüm üye listesi, değilse botun gördüğü üyeler."""
    with get_db() as conn:
        optout = {r['user_id'] for r in conn.execute("SELECT user_id FROM tag_optout WHERE chat_id = ?", (chat_id,))}
    skip = optout | {TELEGRAM_SERVICE_ID, GROUP_ANON_BOT_ID}
    users: dict = {}
    ub = await get_userbot()
    if ub and not active_only and (chat_bot_id(chat_id) or BOT_ID) == BOT_ID:
        try:
            async for p in ub.iter_participants(int(chat_id), limit=TAG_MAX):
                if not getattr(p, 'bot', False) and not getattr(p, 'deleted', False) and p.id not in skip:
                    users[p.id] = getattr(p, 'first_name', None) or getattr(p, 'username', None) or str(p.id)
        except Exception as e:
            logger.debug(f"Üye listesi alınamadı {chat_id}: {e}")
    # Botun gördüğü üyeler de eklenir (Telethon büyük gruplarda listenin hepsini vermeyebilir)
    since = time.time() - 7 * 86400 if active_only else 0
    with get_db() as conn:
        rows = conn.execute("""SELECT user_id, first_name, username FROM users WHERE chat_id = ? AND is_bot = 0
                               AND left_at IS NULL AND COALESCE(last_seen, 0) >= ? ORDER BY last_seen DESC LIMIT ?""",
                            (chat_id, since, TAG_MAX)).fetchall()
    for r in rows:
        if r['user_id'] not in skip and len(users) < TAG_MAX:
            users.setdefault(r['user_id'], r['first_name'] or r['username'] or str(r['user_id']))
    return list(users.items())

def _tag_chunk_html(chunk: list, style: str) -> str:
    if style == 'emoji':
        return " ".join(f'<a href="tg://user?id={uid}">{random.choice(TAG_EMOJIS)}</a>' for uid, _ in chunk)
    return ", ".join(mention_html(uid, name) for uid, name in chunk)

async def run_tagging(chat_id: str, text_html: str, targets: list, size: int, style: str, status_msg, thread_id=None):
    run = _tag_runs[chat_id]
    fails = 0
    try:
        for i in range(0, len(targets), size):
            if run['stop']:
                break
            chunk = targets[i:i + size]
            body = (text_html + "\n\n" if text_html else "") + _tag_chunk_html(chunk, style)
            kw = {'parse_mode': ParseMode.HTML, 'disable_web_page_preview': True}
            if thread_id:
                kw['message_thread_id'] = thread_id
            try:
                await bot.send_message(chat_id, body, **kw)
                fails = 0
            except BadRequest:
                try:  # biçim hatası: metni düz gönder
                    await bot.send_message(chat_id, html.escape(_strip_html(text_html)) + "\n\n" +
                                           _tag_chunk_html(chunk, style), **kw)
                except TelegramError as e:
                    logger.debug(f"Etiket mesajı gönderilemedi {chat_id}: {e}")
                    fails += 1
            except TelegramError as e:
                logger.debug(f"Etiket mesajı gönderilemedi {chat_id}: {e}")
                fails += 1
            if fails >= 3:
                run['stop'] = True
                break
            run['done'] = min(len(targets), i + size)
            if TAG_DELAY and i + size < len(targets):
                await asyncio.sleep(TAG_DELAY)
    finally:
        _tag_runs.pop(chat_id, None)
        _tag_last[chat_id] = time.time()
        end = "⏹ Etiketleme durduruldu" if run['stop'] else "✅ Etiketleme bitti"
        try:
            await status_msg.edit_text(f"{end}: {run['done']}/{len(targets)} kişi etiketlendi.")
        except Exception as e:
            logger.debug(f"Etiket durum mesajı güncellenemedi: {e}")

def _spawn(coro):
    """Arka plan görevi. PTB'nin create_task'ı kapanışta görevin bitmesini bekler (uzun etiketleme yeniden başlatmayı
    geciktirirdi); düz asyncio görevi kapanışta iptal edilir. Referans _tag_runs içinde tutulur."""
    return asyncio.get_running_loop().create_task(coro)

async def cmd_etiket(update: Update, context):
    """/etiket mesaj — üyeleri gruplar hâlinde etiketleyerek mesaj atar."""
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type not in ('group', 'supergroup'):
        await msg.reply_text("/etiket grupta kullanılır.")
        return
    chat_id = str(chat.id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    if not await require(update, chat_id, 'can_tag'):
        return
    if chat_id in _tag_runs:
        await msg.reply_text("⏳ Bu grupta etiketleme zaten sürüyor. Durdurmak için: /etiketdur")
        return
    wait = TAG_COOLDOWN - (time.time() - _tag_last.get(chat_id, 0))
    if wait > 0:
        await msg.reply_text(f"⏳ Grup kirlenmesin diye etiketlemeler arasında bekleme var. {int(wait // 60) + 1} dk sonra tekrar dene.")
        return
    text_html = cmd_args_html(msg) or (msg_html(msg.reply_to_message) if msg.reply_to_message else '')
    s = channel['settings']
    targets = await tag_targets(chat_id, bool(s.get('tag_active_only')))
    if not targets:
        await msg.reply_text("Etiketlenecek kimse bulunamadı. (Bot, grupta yazan ya da katılan üyeleri tanır.)")
        return
    size = max(1, min(10, int(s.get('tag_size') or 5)))
    style = 'emoji' if s.get('tag_style') == 'emoji' else 'name'
    secs = int((len(targets) + size - 1) // size * max(TAG_DELAY, 3))
    status = await msg.reply_text(
        f"🏷 Etiketleme başladı: <b>{len(targets)}</b> kişi, {size}'{'li' if size in (1, 5, 10) else 'lü'} · "
        f"yaklaşık {max(1, secs // 60)} dk\nDurdurmak için: /etiketdur", parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[ibtn("⏹ Durdur", f"et|{chat_id}|stop", RED)]]))
    run = {'stop': False, 'by': update.effective_user.id, 'done': 0, 'total': len(targets)}
    _tag_runs[chat_id] = run
    run['task'] = _spawn(run_tagging(chat_id, text_html, targets, size, style, status,
                                              getattr(msg, 'message_thread_id', None)))
    await send_log(chat_id, f"🏷 /etiket başlatıldı ({len(targets)} kişi) | {mention(update.effective_user)}", ParseMode.HTML)

async def cmd_etiketdur(update: Update, context):
    chat_id = str(update.effective_chat.id)
    if not get_channel_settings(chat_id) or not await require(update, chat_id, 'can_tag'):
        return
    run = _tag_runs.get(chat_id)
    if not run:
        await update.effective_message.reply_text("Şu an süren bir etiketleme yok.")
        return
    run['stop'] = True
    await update.effective_message.reply_text("⏹ Etiketleme durduruluyor…")

async def tag_stop_callback(update: Update, context):
    """et|cid|stop"""
    query = update.callback_query
    chat_id = query.data.split('|')[1]
    if not has_specific_permission(chat_id, query.from_user.id, 'can_tag'):
        await deny(update, 'can_tag')
        return
    run = _tag_runs.get(chat_id)
    if run:
        run['stop'] = True
    await query.answer("⏹ Durduruluyor" if run else "Etiketleme zaten bitti.")

async def cmd_etiketme(update: Update, context):
    """/etiketme — üye kendini bu gruptaki /etiket listesinden çıkarır (tekrar yazınca geri alır)."""
    chat = update.effective_chat
    if chat.type not in ('group', 'supergroup'):
        await update.effective_message.reply_text("Bunu etiketlenmek istemediğin grupta yaz.")
        return
    uid, chat_id = update.effective_user.id, str(chat.id)
    with get_db() as conn:
        n = conn.execute("DELETE FROM tag_optout WHERE chat_id = ? AND user_id = ?", (chat_id, uid)).rowcount
        if not n:
            conn.execute("INSERT INTO tag_optout (chat_id, user_id) VALUES (?, ?)", (chat_id, uid))
        conn.commit()
    await update.effective_message.reply_text(
        "🔔 Tekrar /etiket listesindesin." if n else "🔕 Artık bu grupta /etiket ile etiketlenmeyeceksin. Geri almak için tekrar /etiketme yaz.")

# ═══════════════════════════ KANAL ZORUNLULUĞU ═══════════════════════════
FSUB_OK_TTL = 600       # sn: üyeliği doğrulanan kişi bu süre boyunca tekrar sorgulanmaz
FSUB_WARN_EVERY = 60    # sn: aynı kişiye en fazla bu sıklıkla uyarı
FSUB_WARN_TTL = 60      # sn: uyarı mesajı bu süre sonra silinir
_fsub_ok: dict = {}
_fsub_warned: dict = {}
_fsub_broken: dict = {}  # grup → kanal kontrolü yapılamadı (bot kanalda yönetici değil) uyarısı zamanı

async def fsub_is_member(group_id: str, channel_id, user_id: int, fresh: bool = False) -> bool:
    key = (str(channel_id), user_id)
    if not fresh and time.time() - _fsub_ok.get(key, 0) < FSUB_OK_TTL:
        return True
    try:
        m = await bot.get_chat_member(channel_id, user_id)
    except BadRequest as e:
        if 'user not found' in str(e).lower() or 'participant' in str(e).lower():
            return False
        return await _fsub_failed(group_id, e)
    except TelegramError as e:
        return await _fsub_failed(group_id, e)
    ok = m.status in ('member', 'administrator', 'creator') or (m.status == 'restricted' and getattr(m, 'is_member', False))
    if ok:
        _fsub_ok[key] = time.time()
    return ok

async def _fsub_failed(group_id: str, e) -> bool:
    """Kanal kontrol edilemiyorsa üyeler engellenmez (yanlışlıkla herkesi susturmamak için); yöneticilere bildirilir."""
    logger.debug(f"Kanal zorunluluğu kontrol edilemedi {group_id}: {e}")
    if time.time() - _fsub_broken.get(group_id, 0) > 3600:
        _fsub_broken[group_id] = time.time()
        await send_log(group_id, "⚠️ Kanal zorunluluğu kontrol edilemiyor: bot kanalda yönetici mi? (/kanalzorunlu ile tekrar ayarla)")
    return True

async def fsub_guard(update: Update, context):
    """Kanala katılmayanın grup mesajı silinir; 'Kanala katıl / Katıldım' butonlu uyarı gelir."""
    msg = update.effective_message
    if not msg or not update.message or msg.chat.type not in ('group', 'supergroup'):
        return
    user = msg.from_user
    if not user or user.is_bot or msg.sender_chat or user.id in (TELEGRAM_SERVICE_ID, GROUP_ANON_BOT_ID):
        return
    if getattr(msg, 'new_chat_members', None) or getattr(msg, 'left_chat_member', None):
        return
    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    s = channel['settings']
    if not s.get('fsub_enabled') or not s.get('fsub_channel'):
        return
    if await is_staff_user(chat_id, user.id, channel):
        return
    if await fsub_is_member(chat_id, s['fsub_channel'], user.id):
        return
    try:
        await msg.delete()
    except TelegramError as e:
        logger.debug(f"Kanal zorunluluğu: mesaj silinemedi {chat_id}: {e}")
    key = (chat_id, user.id)
    if time.time() - _fsub_warned.get(key, 0) >= FSUB_WARN_EVERY:
        _fsub_warned[key] = time.time()
        title = html.escape(s.get('fsub_title') or 'kanal')
        rows = []
        if s.get('fsub_link'):
            rows.append([ibtn(f"📢 {(s.get('fsub_title') or 'Kanala katıl')[:40]}", url=s['fsub_link'], style=BLUE)])
        rows.append([ibtn("✅ Katıldım", f"fs|{chat_id}|{user.id}", GREEN)])
        try:
            warn = await bot.send_message(
                chat_id, f"📢 {mention(user)}, bu grupta yazabilmek için önce <b>{title}</b> kanalına katılmalısın.",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
                message_thread_id=getattr(msg, 'message_thread_id', None))
            schedule_delete(chat_id, [warn], FSUB_WARN_TTL)
        except TelegramError as e:
            logger.debug(f"Kanal zorunluluğu uyarısı gönderilemedi {chat_id}: {e}")
    raise ApplicationHandlerStop

async def fsub_callback(update: Update, context):
    """fs|cid|uid — ✅ Katıldım"""
    query = update.callback_query
    _, chat_id, uid = query.data.split('|')
    if query.from_user.id != int(uid):
        await query.answer("Bu buton sana ait değil.", show_alert=True)
        return
    channel = get_channel_settings(chat_id)
    s = channel['settings'] if channel else {}
    if not s.get('fsub_enabled') or not s.get('fsub_channel') or \
            await fsub_is_member(chat_id, s['fsub_channel'], query.from_user.id, fresh=True):
        await query.answer("✅ Teşekkürler, artık yazabilirsin!")
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return
    await query.answer("Henüz kanala katılmamışsın. Önce 📢 butonuyla katıl, sonra tekrar bas.", show_alert=True)

async def fsub_set_channel(group_id: str, ref: str):
    """Kanalı doğrular (bot kanalda yönetici olmalı). Dönüş: (ayarlar sözlüğü, None) veya (None, hata)."""
    ref = (ref or '').strip()
    m = re.match(r'^(?:https?://)?t\.me/([A-Za-z]\w{3,31})/?$', ref)
    if m:
        ref = '@' + m.group(1)
    if not (re.fullmatch(r'@[A-Za-z]\w{3,31}', ref) or re.fullmatch(r'-100\d{5,}', ref)):
        return None, "Kanalı <code>@kullaniciadi</code>, <code>t.me/kanal</code> ya da <code>-100…</code> ID olarak yaz."
    try:
        ch = await bot.get_chat(ref)
        me = await bot.get_chat_member(ch.id, bot_id_for(group_id))
    except Exception as e:
        return None, f"Kanal bulunamadı ya da bot kanalda değil. {html.escape(friendly_error(e))}"
    if getattr(ch, 'type', '') not in ('channel', 'supergroup'):
        return None, "Bu bir kanal değil."
    if me.status not in ('administrator', 'creator'):
        return None, "Botu önce kanala <b>yönetici</b> olarak ekle (üyeleri görebilmesi için gerekli)."
    link = f"https://t.me/{ch.username}" if getattr(ch, 'username', None) else getattr(ch, 'invite_link', None)
    if not link:
        try:
            link = await bot.export_chat_invite_link(ch.id)
        except TelegramError:
            link = None
    return {'fsub_channel': ch.id, 'fsub_title': ch.title, 'fsub_link': link, 'fsub_enabled': True}, None

async def cmd_kanalzorunlu(update: Update, context):
    """/kanalzorunlu @kanal · /kanalzorunlu kapat · /kanalzorunlu (durum)"""
    chat_id = _get_effective_chat_id(update, context)
    msg = update.effective_message
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    if not await require(update, chat_id, 'can_manage_settings'):
        return
    channel = get_channel_settings(chat_id)
    s = channel['settings']
    arg = context.args[0] if context.args else ''
    if not arg:
        st = (f"Açık — <b>{html.escape(s.get('fsub_title') or '?')}</b>" if s.get('fsub_enabled') and s.get('fsub_channel')
              else "Kapalı")
        await msg.reply_text(f"📢 <b>Kanal zorunluluğu</b>: {st}\n\nAyarla: <code>/kanalzorunlu @kanal</code>\n"
                             "Kapat: <code>/kanalzorunlu kapat</code>\nBot, kanalda yönetici olmalı. "
                             "Yöneticiler ve bot sahibi muaftır.", parse_mode=ParseMode.HTML)
        return
    if arg.lower() in ('kapat', 'off', 'kaldır', 'kaldir'):
        s['fsub_enabled'] = False
        save_channel_settings(chat_id, channel)
        await msg.reply_text("📢 Kanal zorunluluğu kapatıldı.")
        return
    vals, err = await fsub_set_channel(chat_id, arg)
    if err:
        await msg.reply_text(f"❌ {err}", parse_mode=ParseMode.HTML)
        return
    s.update(vals)
    save_channel_settings(chat_id, channel)
    await msg.reply_text(f"✅ Kanal zorunluluğu açık: <b>{html.escape(vals['fsub_title'] or '')}</b>\n"
                         "Kanala katılmayanların mesajı silinir ve katılma butonu gösterilir.", parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"📢 Kanal zorunluluğu: {html.escape(vals['fsub_title'] or '')} | {mention(update.effective_user)}",
                   ParseMode.HTML)

# ═══════════════════════════ AFK ═══════════════════════════
AFK_NOTIFY_EVERY = 60   # sn: aynı grupta aynı AFK kişi için en fazla bu sıklıkla hatırlatma

_afk_notified: dict = {}

def _dur_text(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return "kısa süre"
    if sec < 3600:
        return f"{sec // 60} dakika"
    if sec < 86400:
        return f"{sec // 3600} saat"
    return f"{sec // 86400} gün"

def _ago(sec: float) -> str:
    """'5 dakikadır', '2 saattir', '3 gündür'"""
    t = _dur_text(sec)
    if t == "kısa süre":
        return "az önce"
    n, unit = t.split()
    return f"{n} " + {'dakika': "dakikadır", 'saat': "saattir", 'gün': "gündür"}[unit]

def afk_get(user_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM afk WHERE user_id = ?", (user_id,)).fetchone()

async def cmd_afk(update: Update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    parts = (msg.text or '').split(maxsplit=1)
    reason = parts[1].strip()[:200] if len(parts) > 1 else ''
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO afk (user_id, reason, since, name) VALUES (?, ?, ?, ?)",
                     (user.id, reason, time.time(), user.first_name or user.username or str(user.id)))
        conn.commit()
    await msg.reply_text(f"💤 {mention(user)} artık AFK" + (f": {html.escape(reason)}" if reason else "."),
                         parse_mode=ParseMode.HTML)

async def afk_handler(update: Update, context):
    """AFK kişi yazınca AFK kalkar; AFK birini yanıtlayan/etiketleyen kişiye bilgi verilir."""
    msg = update.effective_message
    if not msg or not update.message or msg.chat.type == 'private' or not msg.from_user or msg.from_user.is_bot:
        return
    user = msg.from_user
    text = msg.text or msg.caption or ''
    if not re.match(r'^/afk(@\w+)?(\s|$)', text, re.I):
        row = afk_get(user.id)
        if row:
            with get_db() as conn:
                conn.execute("DELETE FROM afk WHERE user_id = ?", (user.id,))
                conn.commit()
            try:
                await msg.reply_text(f"👋 {mention(user)} geri döndü ({_dur_text(time.time() - row['since'])} AFK'ydı).",
                                     parse_mode=ParseMode.HTML)
            except TelegramError as e:
                logger.debug(f"AFK dönüş mesajı gönderilemedi: {e}")
    targets = set()
    reply = msg.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        targets.add(reply.from_user.id)
    for ent in (getattr(msg, 'entities', None) or ()):
        if getattr(ent, 'type', None) == 'text_mention' and getattr(ent, 'user', None):
            targets.add(ent.user.id)
    names = set(m.lower() for m in re.findall(r'@(\w{4,32})', text))
    if names:
        with get_db() as conn:
            for n in names:
                r = conn.execute("SELECT user_id FROM users WHERE LOWER(username) = ? LIMIT 1", (n,)).fetchone()
                if r:
                    targets.add(r['user_id'])
    targets.discard(user.id)
    chat_id = str(msg.chat_id)
    for uid in targets:
        row = afk_get(uid)
        if not row:
            continue
        key = (chat_id, uid)
        if time.time() - _afk_notified.get(key, 0) < AFK_NOTIFY_EVERY:
            continue
        _afk_notified[key] = time.time()
        why = f": {html.escape(row['reason'])}" if row['reason'] else "."
        try:
            await msg.reply_text(f"💤 {html.escape(row['name'] or 'Bu kişi')} şu an AFK ({_ago(time.time() - row['since'])}){why}",
                                 parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.debug(f"AFK bilgisi gönderilemedi: {e}")

# ── Panel: kanal zorunluluğu ve etiket ayarları ──
def render_fsub_tag_page(cid: str, base: str, s: dict, title: str):
    if base == 'fsub':
        on = s.get('fsub_enabled') and s.get('fsub_channel')
        text = (f"📢 <b>Kanal zorunluluğu</b> — {title}\n\nDurum: <b>{'Açık' if on else 'Kapalı'}</b>\n"
                f"Kanal: {html.escape(s.get('fsub_title') or 'ayarlanmamış')}\n\n"
                "Kanala katılmayan üyenin mesajı silinir ve '📢 Kanala katıl / ✅ Katıldım' butonlu uyarı gelir. "
                "Yöneticiler muaftır. Bot, kanalda yönetici olmalı.")
        rows = [[ibtn("✏️ Kanalı ayarla", f"s|{cid}|i|fsub", BLUE)]]
        if s.get('fsub_channel'):
            rows.insert(0, [toggle_btn("Kanal zorunluluğu", s.get('fsub_enabled'), f"s|{cid}|t|fsub_enabled|fsub")])
        rows.append(_back(cid))
        return text, rows
    style = s.get('tag_style') or 'name'
    text = (f"🏷 <b>Etiket ayarları</b> — {title}\n\n<code>/etiket mesaj</code> üyeleri gruplar hâlinde etiketler. "
            "<code>/etiketdur</code> durdurur, üyeler <code>/etiketme</code> ile listeden çıkabilir.\n\n"
            f"Bir mesajda: <b>{int(s.get('tag_size') or 5)} kişi</b> · Biçim: <b>{'emoji' if style == 'emoji' else 'isim'}</b> · "
            f"Kimler: <b>{'son 7 günün aktifleri' if s.get('tag_active_only') else 'herkes'}</b>")
    rows = [_num_row(cid, 'tag_size', s, 'tag', "Kişi"),
            [ibtn(("✅ " if style == 'name' else "") + "İsimle", f"s|{cid}|ts|name", GREEN if style == 'name' else None),
             ibtn(("✅ " if style == 'emoji' else "") + "Emojiyle", f"s|{cid}|ts|emoji", GREEN if style == 'emoji' else None)],
            [toggle_btn("Sadece son 7 günün aktifleri", s.get('tag_active_only'), f"s|{cid}|t|tag_active_only|tag")],
            _back(cid)]
    return text, rows


# ═══════════════════════════ ORTAK KARA LİSTE + CAS ═══════════════════════════
# Botun başka bir grubunda banlanmış (ban_list) ya da CAS (dünya çapındaki spam listesi) kaydı olan kişi katılınca
# ayara göre: yöneticilere bildir / sustur / banla. Yetkilinin eklediği kişiye dokunulmaz.
BLACKLIST_ACTIONS = {'notify': "Bildir", 'mute': "Sustur", 'ban': "Banla"}
CAS_URL = "https://api.cas.chat/check"
_cas_cache: dict = {}   # user_id -> (zaman, kayıtlı mı)

async def _cas_fetch(user_id: int) -> bool | None:
    import httpx  # python-telegram-bot ile birlikte kurulu
    async with httpx.AsyncClient(timeout=4) as cl:
        r = await cl.get(CAS_URL, params={'user_id': user_id})
        return bool(r.json().get('ok'))

async def cas_banned(user_id: int) -> bool:
    """CAS kaydı var mı? (1 saat önbellek; servis yanıt vermezse 'temiz' sayılır)"""
    c = _cas_cache.get(user_id)
    if c and time.time() - c[0] < 3600:
        return c[1]
    try:
        res = await _cas_fetch(user_id)
    except Exception as e:
        logger.debug(f"CAS sorgulanamadı {user_id}: {e}")
        return False
    _cas_cache[user_id] = (time.time(), bool(res))
    if len(_cas_cache) > 20000:
        _cas_cache.clear()
    return bool(res)

def ban_flags(user_id: int, exclude_chat: str | None = None) -> list:
    """Kişinin bu botun diğer gruplarındaki banları [(chat_id, sebep, zaman)]."""
    with get_db() as conn:
        rows = conn.execute("""SELECT b.chat_id, b.reason, b.banned_at FROM ban_list b JOIN channels c ON c.chat_id = b.chat_id
                               WHERE b.user_id = ? AND b.chat_id != ? AND mybot(c.bot_id) ORDER BY b.banned_at DESC""",
                            (user_id, str(exclude_chat or ''))).fetchall()
    return [(r['chat_id'], r['reason'], r['banned_at']) for r in rows]

async def blacklist_reasons(chat_id: str, user, s: dict) -> list:
    reasons = []
    if s.get('shared_blacklist', True):
        flags = ban_flags(user.id, chat_id)
        if flags:
            reasons.append(f"botun {len({f[0] for f in flags})} başka grubunda banlı")
    if s.get('cas_enabled') and await cas_banned(user.id):
        reasons.append("CAS spam listesinde kayıtlı")
    return reasons

async def blacklist_on_join(chat_id: str, user, channel: dict) -> bool:
    """Katılan kişi kara listedeyse ayarlı işlemi uygular. True: kişi kısıtlandı/banlandı (hoş geldin yok)."""
    s = channel['settings']
    reasons = await blacklist_reasons(chat_id, user, s)
    if not reasons:
        return False
    action = s.get('blacklist_action', 'mute')
    why = " · ".join(reasons)
    who = mention(user)
    try:
        if action == 'ban':
            await bot.ban_chat_member(chat_id, user.id)
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("INSERT OR REPLACE INTO ban_list (chat_id, user_id, username, reason, banned_at, banned_by) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (chat_id, user.id, user.username or user.first_name,
                                                               f"kara liste: {why}", time.time(), cur_bot_id()))
                    conn.commit()
            text, markup = f"🚩 {who} kara listede ({why}) → <b>banlandı</b>.", mod_markup(chat_id, user.id, 'ban')
        elif action == 'mute':
            await bot.restrict_chat_member(chat_id, user.id, permissions=ChatPermissions.no_permissions())
            text, markup = (f"🚩 {who} kara listede ({why}) → <b>susturuldu</b>. Yöneticiler serbest bırakabilir.",
                            mod_markup(chat_id, user.id, 'mute'))
        else:
            text, markup = f"🚩 Dikkat: {who} {why}.", mod_markup(chat_id, user.id, 'warn')
    except TelegramError as e:
        logger.debug(f"Kara liste işlemi uygulanamadı {chat_id}/{user.id}: {e}")
        return False
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError as e:
        logger.debug(f"Kara liste bildirimi gönderilemedi: {e}")
    await send_log(chat_id, f"{text} | {chat_id}", ParseMode.HTML, reply_markup=markup)
    await log_mod_action(chat_id, f"karaliste:{action}", user.id, user.username or '', cur_bot_id(), 'bot', why)
    return action != 'notify'

# ═══════════════════════════ İSİM DEĞİŞİKLİĞİ TAKİBİ ═══════════════════════════
_name_seen: dict = {}  # user_id -> (ad, soyad, kullanıcı adı) — her mesajda veritabanına gitmemek için

def _name_str(t) -> str:
    first, last, uname = t
    return (f"{first} {last}".strip() or "—") + (f" (@{uname})" if uname else "")

async def track_name(chat_id: str, user, s: dict | None = None):
    """Ad/soyad/kullanıcı adı değişince geçmişe yazar ve (açıksa) log kanalına bildirir."""
    if not user or getattr(user, 'is_bot', False):
        return
    cur = (getattr(user, 'first_name', None) or '', getattr(user, 'last_name', None) or '', getattr(user, 'username', None) or '')
    prev = _name_seen.get(user.id)
    if prev is None:
        with get_db() as conn:
            row = conn.execute("SELECT first_name, last_name, username FROM name_history WHERE user_id = ? "
                               "ORDER BY seen_at DESC LIMIT 1", (user.id,)).fetchone()
        prev = (row['first_name'] or '', row['last_name'] or '', row['username'] or '') if row else None
    if len(_name_seen) > 50000:
        _name_seen.clear()
    _name_seen[user.id] = cur
    if prev == cur:
        return
    with get_db() as conn:
        conn.execute("INSERT INTO name_history (user_id, first_name, last_name, username, seen_at) VALUES (?, ?, ?, ?, ?)",
                     (user.id, *cur, time.time()))
        conn.commit()
    if prev is not None and (s or {}).get('name_track', True):
        await send_log(chat_id, f"✏️ İsim değişikliği: {mention(user)} (ID <code>{user.id}</code>)\n"
                                f"Eski: {html.escape(_name_str(prev))}\nYeni: {html.escape(_name_str(cur))}", ParseMode.HTML)

def name_history(user_id: int, limit: int = 10) -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT first_name, last_name, username, seen_at FROM name_history WHERE user_id = ? "
                            "ORDER BY seen_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [((r['first_name'] or '', r['last_name'] or '', r['username'] or ''), r['seen_at']) for r in rows]

# ═══════════════════════════ KULLANICI SİCİLİ (/sicil) ═══════════════════════════
def _action_kind(action: str) -> str | None:
    a = (action or '').lower()
    if 'unban' in a or 'unmute' in a or 'unwarn' in a or a.endswith(':ok'):
        return None
    if 'ban' in a:
        return 'ban'
    if 'mute' in a or 'sustur' in a:
        return 'mute'
    if 'kick' in a or 'atıldı' in a:
        return 'kick'
    if 'warn' in a:
        return 'warn'
    return None

async def build_sicil(user_id: int) -> str:
    with get_db() as conn:
        u = conn.execute("SELECT first_name, last_name, username FROM users WHERE user_id = ? ORDER BY last_seen DESC LIMIT 1",
                         (user_id,)).fetchone()
        logs = conn.execute("""SELECT m.chat_id, m.action, m.reason, m.timestamp FROM mod_log m
                               JOIN channels c ON c.chat_id = m.chat_id
                               WHERE m.target_user_id = ? AND mybot(c.bot_id) ORDER BY m.timestamp DESC LIMIT 200""",
                            (user_id,)).fetchall()
    hist = name_history(user_id)
    name = _name_str(hist[0][0]) if hist else (_name_str((u['first_name'] or '', u['last_name'] or '', u['username'] or ''))
                                                 if u else str(user_id))
    lines = [f"📋 <b>Sicil</b> — {mention_html(user_id, name)} · ID <code>{user_id}</code>"]
    if len(hist) > 1:
        lines.append("🏷 Eski isimler: " + " ← ".join(html.escape(_name_str(h[0])) for h in hist[1:6]))
    flags = ban_flags(user_id)
    lines.append(f"🚩 Ortak kara liste: {'<b>' + str(len({f[0] for f in flags})) + ' grupta banlı</b>' if flags else 'temiz'}")
    lines.append(f"🌐 CAS: {'⚠️ kayıtlı' if await cas_banned(user_id) else 'temiz'}")
    counts = {'warn': 0, 'mute': 0, 'kick': 0, 'ban': 0}
    shown = []
    for r in logs:
        k = _action_kind(r['action'])
        if not k:
            continue
        counts[k] += 1
        if len(shown) < 15:
            shown.append(r)
    lines.append(f"📊 Son {MOD_LOG_RETENTION_DAYS} gün: ⚠️ {counts['warn']} uyarı · 🔇 {counts['mute']} susturma · "
                 f"👢 {counts['kick']} atma · 🚫 {counts['ban']} ban")
    if shown:
        titles = {}
        for r in shown:
            if r['chat_id'] not in titles:
                titles[r['chat_id']] = await _chat_title(r['chat_id'])
        items = [f"• {datetime.fromtimestamp(r['timestamp'], TZ_TR):%d.%m %H:%M} — {html.escape(titles[r['chat_id']][:30])}: "
                 f"{html.escape(r['action'])}{(' (' + html.escape((r['reason'] or '')[:60]) + ')') if r['reason'] else ''}"
                 for r in shown]
        lines.append("<blockquote expandable>" + "\n".join(items) + "</blockquote>")
    else:
        lines.append("Kayıtlı ceza yok.")
    return "\n".join(lines)

async def cmd_sicil(update: Update, context):
    """/sicil @kullanıcı | ID | yanıt — botun tüm gruplarındaki ceza geçmişi (grupta yazılırsa özelden gönderilir)."""
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    if not await require(update, chat_id, 'can_warn'):
        return
    target, _ = await _resolve_target_id(update, context)
    if not target:
        await msg.reply_text("Kullanım: /sicil @kullanıcı, /sicil ID ya da bir mesaja yanıt olarak /sicil")
        return
    text = await build_sicil(target)
    if update.effective_chat.type == 'private':
        await msg.reply_text(text, parse_mode=ParseMode.HTML)
        return
    try:  # başka grupların bilgisi grupta herkese görünmesin
        await bot.send_message(update.effective_user.id, text, parse_mode=ParseMode.HTML)
        await msg.reply_text("📩 Sicil özelden gönderildi.")
    except TelegramError:
        await msg.reply_text("📩 Sicili özelden gönderebilmem için önce bana özelden /start yaz.",
                             reply_markup=InlineKeyboardMarkup([[ibtn("🤖 Bota git", url=f"https://t.me/{context.bot.username}")]]))

# ═══════════════════════════ OYLAMALI SUSTURMA (/oylama) ═══════════════════════════
VOTE_TTL = 600              # sn: oylama süresi
VOTE_STARTER_COOLDOWN = 300  # sn: aynı kişi bu sürede bir oylama başlatabilir
VOTE_MIN_AGE = 86400        # sn: en az bu kadardır grupta olan oy verebilir
_votes: dict = {}           # (chat_id, hedef) -> {'yes': set, 'at': zaman, 'name': ad}
_vote_started: dict = {}

def _can_vote(chat_id: str, uid: int) -> bool:
    with get_db() as conn:
        r = conn.execute("SELECT joined_at FROM users WHERE chat_id = ? AND user_id = ?", (chat_id, uid)).fetchone()
    return bool(r) and time.time() - (r['joined_at'] or 0) >= VOTE_MIN_AGE

def _vote_markup(chat_id: str, target: int, n: int, need: int):
    return InlineKeyboardMarkup([[ibtn(f"🔇 Sustur ({n}/{need})", f"vm|{chat_id}|{target}|y", RED),
                                  ibtn("❌ İptal (yönetici)", f"vm|{chat_id}|{target}|x")]])

async def cmd_oylama(update: Update, context):
    """Bir mesaja yanıt: /oylama — üyeler oy verir, yeterli oy gelince kişi geçici susturulur."""
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type not in ('group', 'supergroup'):
        await msg.reply_text("/oylama grupta, bir mesaja yanıt olarak kullanılır.")
        return
    chat_id = str(chat.id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    s = channel['settings']
    if not s.get('vote_mute', True):
        await msg.reply_text("Bu grupta oylamalı susturma kapalı.")
        return
    reply = msg.reply_to_message
    if not reply or not reply.from_user:
        await msg.reply_text("Susturulmasını istediğin kişinin mesajına yanıt olarak /oylama yaz.")
        return
    target, voter = reply.from_user, update.effective_user
    if target.is_bot or target.id == voter.id or await is_staff_user(chat_id, target.id, channel):
        await msg.reply_text("Bu kişi için oylama başlatılamaz.")
        return
    staff = await is_staff_user(chat_id, voter.id, channel)
    if not staff and not _can_vote(chat_id, voter.id):
        await msg.reply_text("Yeni üyeler oylama başlatamaz (en az 1 gündür grupta olmalısın).")
        return
    now = time.time()
    if not staff and now - _vote_started.get((chat_id, voter.id), 0) < VOTE_STARTER_COOLDOWN:
        await msg.reply_text("Kısa süre önce oylama başlattın, biraz bekle.")
        return
    key = (chat_id, target.id)
    if key in _votes and now - _votes[key]['at'] < VOTE_TTL:
        await msg.reply_text("Bu kişi için zaten bir oylama sürüyor.")
        return
    need = max(2, int(s.get('vote_needed') or 5))
    minutes = int(s.get('vote_mute_minutes') or 60)
    _votes[key] = {'yes': {voter.id}, 'at': now, 'name': target.first_name or str(target.id)}
    _vote_started[(chat_id, voter.id)] = now
    await msg.reply_text(
        f"🗳 {mention(target)} için <b>susturma oylaması</b> ({human_duration(minutes * 60)})\n"
        f"Başlatan: {mention(voter)} · {need} oy gerekli · {VOTE_TTL // 60} dk içinde",
        parse_mode=ParseMode.HTML, reply_markup=_vote_markup(chat_id, target.id, 1, need))

async def vote_callback(update: Update, context):
    """vm|cid|hedef|y/x"""
    query = update.callback_query
    _, chat_id, target_s, op = query.data.split('|')
    target = int(target_s)
    key = (chat_id, target)
    v = _votes.get(key)
    channel = get_channel_settings(chat_id)
    if not v or not channel or time.time() - v['at'] > VOTE_TTL:
        _votes.pop(key, None)
        await query.answer("Bu oylamanın süresi doldu.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return
    uid = query.from_user.id
    if op == 'x':
        if not has_specific_permission(chat_id, uid, 'can_mute'):
            await deny(update, 'can_mute')
            return
        _votes.pop(key, None)
        await query.answer("Oylama iptal edildi")
        await query.edit_message_text(f"❌ Oylama {mention(query.from_user)} tarafından iptal edildi.", parse_mode=ParseMode.HTML)
        return
    if uid == target:
        await query.answer("Kendin için oy veremezsin.", show_alert=True)
        return
    if not await is_staff_user(chat_id, uid, channel) and not _can_vote(chat_id, uid):
        await query.answer("Yeni üyeler oy veremez (en az 1 gündür grupta olmalısın).", show_alert=True)
        return
    if uid in v['yes']:
        await query.answer("Zaten oy verdin.")
        return
    v['yes'].add(uid)
    s = channel['settings']
    need = max(2, int(s.get('vote_needed') or 5))
    if len(v['yes']) < need:
        await query.answer("✅ Oyun alındı")
        try:
            await query.edit_message_reply_markup(reply_markup=_vote_markup(chat_id, target, len(v['yes']), need))
        except TelegramError:
            pass
        return
    _votes.pop(key, None)
    minutes = int(s.get('vote_mute_minutes') or 60)
    try:
        await bot.restrict_chat_member(chat_id, target, permissions=ChatPermissions.no_permissions(),
                                       until_date=int(time.time() + minutes * 60))
    except TelegramError as e:
        await query.answer("Susturulamadı (bot yetkisi?)", show_alert=True)
        logger.debug(f"Oylama susturması uygulanamadı {chat_id}/{target}: {e}")
        return
    await query.answer("🔇 Susturuldu")
    who = mention_html(target, v['name'])
    await query.edit_message_text(f"🔇 {who} {len(v['yes'])} oyla {human_duration(minutes * 60)} susturuldu.",
                                  parse_mode=ParseMode.HTML, reply_markup=mod_markup(chat_id, target, 'mute'))
    await send_log(chat_id, f"🗳 Oylama: {who} {len(v['yes'])} oyla {human_duration(minutes * 60)} susturuldu | {chat_id}",
                   ParseMode.HTML, reply_markup=mod_markup(chat_id, target, 'mute'))
    await log_mod_action(chat_id, 'oylama:mute', target, '', uid, query.from_user.username or '', f"{len(v['yes'])} oy")

# ── Panel: topluluk koruması sayfası ──
def render_comm_page(cid: str, s: dict, title: str):
    act = s.get('blacklist_action', 'mute')
    text = (f"🧑‍⚖️ <b>Topluluk koruması</b> — {title}\n\n"
            "🚩 <b>Ortak kara liste:</b> botun başka bir grubunda banlanan kişi buraya katılınca seçilen işlem uygulanır.\n"
            "🌐 <b>CAS:</b> dünya çapındaki spam listesinde kayıtlı hesaplar da yakalanır.\n"
            "✏️ <b>İsim takibi:</b> ad/kullanıcı adı değişiklikleri log kanalına yazılır (geçmiş /sicil'de).\n"
            "🗳 <b>Oylamalı susturma:</b> üyeler bir mesaja /oylama ile oy verip kişiyi geçici susturabilir "
            "(yeni üyeler oy veremez, yetkililere kullanılamaz).")
    rows = [[toggle_btn("🚩 Ortak kara liste", s.get('shared_blacklist', True), f"s|{cid}|t|shared_blacklist|comm"),
             toggle_btn("🌐 CAS", s.get('cas_enabled'), f"s|{cid}|t|cas_enabled|comm")],
            [ibtn(("✅ " if act == k else "") + lbl, f"s|{cid}|ba|{k}", GREEN if act == k else None)
             for k, lbl in BLACKLIST_ACTIONS.items()],
            [toggle_btn("✏️ İsim takibi", s.get('name_track', True), f"s|{cid}|t|name_track|comm"),
             toggle_btn("🗳 Oylama", s.get('vote_mute', True), f"s|{cid}|t|vote_mute|comm")],
            _num_row(cid, 'vote_needed', s, 'comm', "Gerekli oy"),
            _num_row(cid, 'vote_mute_minutes', s, 'comm', "Susturma"),
            _back(cid)]
    return text, rows

# ═══════════════════════════ ÇEKİLİŞ ═══════════════════════════
# /cekilis [süre] [kazanan] [ödül] [| kanal=@kanal mesaj=20 gun=7]  ·  /cekilis_bitir [kazanan]
GW_MAX_WINNERS = 20

def _gw_row(chat_id: str):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM giveaways WHERE chat_id = ?", (chat_id,)).fetchone()
    return dict(r) if r else None

def _gw_text(row: dict, count: int) -> str:
    req = json.loads(row.get('req') or '{}')
    prize = row.get('prize') or ''
    lines = [f"🎁 <b>Çekiliş</b>{': ' + html.escape(prize) if prize else ''}",
             f"🏆 Kazanan sayısı: <b>{row.get('winners') or 1}</b>"]
    if row.get('ends_at'):
        lines.append(f"⏰ Bitiş: <b>{datetime.fromtimestamp(row['ends_at'], TZ_TR):%d.%m %H:%M}</b>")
    conds = []
    if req.get('channel'):
        conds.append(f"📢 {html.escape(req.get('channel_title') or 'kanal')} kanalına üye olmak")
    if req.get('min_msgs'):
        conds.append(f"💬 grupta en az {req['min_msgs']} mesaj (son {MESSAGE_STATS_RETENTION_DAYS} gün)")
    if req.get('min_days'):
        conds.append(f"📅 en az {req['min_days']} gündür grupta olmak")
    conds.append("🛡 profil fotoğrafı ya da kullanıcı adı olan gerçek hesap")
    lines.append("📋 <b>Şartlar</b>\n" + "\n".join(conds))
    lines.append(f"👥 Katılımcı: <b>{count}</b>")
    return "\n".join(lines)

def _gw_markup(chat_id: str, row: dict, count: int):
    rows = [[ibtn(f"🎁 Katıl ({count})", f"giveaway|{chat_id}", GREEN)]]
    req = json.loads(row.get('req') or '{}')
    if req.get('channel_link'):
        rows.append([ibtn(f"📢 {(req.get('channel_title') or 'Kanal')[:40]}", url=req['channel_link'], style=BLUE)])
    return InlineKeyboardMarkup(rows)

def parse_giveaway_args(text: str):
    """'1g 3 iPhone 15 | kanal=@x mesaj=20 gun=7' → (süre sn | None, kazanan, ödül, şartlar)"""
    main_part, _, cond_part = (text or '').partition('|')
    tokens = main_part.split()
    duration = parse_interval(tokens[0]) if tokens else None
    if duration:
        tokens = tokens[1:]
    winners = 1
    if tokens and tokens[0].isdigit() and 1 <= int(tokens[0]) <= GW_MAX_WINNERS:
        winners, tokens = int(tokens[0]), tokens[1:]
    conds = {}
    for item in cond_part.split():
        k, _, v = item.partition('=')
        k = k.lower().replace('ü', 'u')
        if k == 'kanal' and v:
            conds['kanal'] = v
        elif k in ('mesaj', 'msg') and v.isdigit():
            conds['min_msgs'] = min(int(v), 100000)
        elif k in ('gun', 'gn') and v.isdigit():
            conds['min_days'] = min(int(v), 3650)
    return duration, winners, " ".join(tokens)[:200], conds

async def cekilis(update: Update, context):
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await msg.reply_text("Yetkin yok!")
        return
    if _gw_row(chat_id):
        await msg.reply_text("Bu grupta süren bir çekiliş var. Bitirmek için: /cekilis_bitir")
        return
    body = (msg.text or '').split(maxsplit=1)
    duration, winners, prize, conds = parse_giveaway_args(body[1] if len(body) > 1 else '')
    if duration is not None and not 60 <= duration <= 30 * 86400:
        await msg.reply_text("Süre 1 dakika ile 30 gün arasında olmalı.")
        return
    req = {k: v for k, v in conds.items() if k != 'kanal'}
    if conds.get('kanal'):
        vals, err = await fsub_set_channel(chat_id, conds['kanal'])
        if err:
            await msg.reply_text(f"❌ Kanal şartı: {err}", parse_mode=ParseMode.HTML)
            return
        req.update(channel=vals['fsub_channel'], channel_title=vals['fsub_title'], channel_link=vals['fsub_link'])
    row = {'prize': prize, 'winners': winners, 'ends_at': time.time() + duration if duration else None,
           'req': json.dumps(req, ensure_ascii=False)}
    sent = await bot.send_message(chat_id, _gw_text(row, 0), parse_mode=ParseMode.HTML,
                                  reply_markup=_gw_markup(chat_id, row, 0), **thread_kw(msg, chat_id))
    async with _db_lock:
        with get_db() as conn:
            conn.execute("""INSERT OR REPLACE INTO giveaways (chat_id, message_id, participants, created_at, prize, winners,
                            ends_at, req, started_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (chat_id, sent.message_id, json.dumps([]), time.time(), prize, winners, row['ends_at'], row['req'],
                          update.effective_user.id))
            conn.commit()
    if msg.chat.type == 'private' or duration is None:
        await msg.reply_text("✅ Çekiliş başladı." + ("" if duration else " Bitirmek için: /cekilis_bitir [kazanan sayısı]") +
                             "\nŞart eklemek için örnek: /cekilis 1g 3 Ödül | kanal=@kanal mesaj=20 gun=7")
    await send_log(chat_id, f"🎉 {mention(update.effective_user)} çekiliş başlattı: {html.escape(prize or '-')} | {chat_id}",
                   ParseMode.HTML)

async def giveaway_eligible(chat_id: str, user, req: dict) -> str | None:
    """Katılım şartları. Dönüş: uygun değilse sebep."""
    if user.is_bot:
        return "Botlar katılamaz."
    try:
        m = await bot.get_chat_member(chat_id, user.id)
        if m.status not in ('member', 'administrator', 'creator') and not (m.status == 'restricted' and getattr(m, 'is_member', False)):
            return "Önce gruba katılmalısın."
    except TelegramError:
        return "Grup üyeliğin doğrulanamadı."
    if not user.username:
        try:
            photos = await bot.get_user_profile_photos(user.id, limit=1)
            has_photo = bool(getattr(photos, 'total_count', 0))
        except TelegramError:
            has_photo = True  # doğrulanamazsa engelleme
        if not has_photo:
            return "Profil fotoğrafı ya da kullanıcı adı olmayan hesaplar katılamaz (sahte hesap koruması)."
    if req.get('channel') and not await fsub_is_member(chat_id, req['channel'], user.id, fresh=True):
        return f"Önce {req.get('channel_title') or 'kanala'} katılmalısın."
    if req.get('min_msgs'):
        with get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM message_stats WHERE chat_id = ? AND user_id = ?", (chat_id, user.id)).fetchone()[0]
        if n < req['min_msgs']:
            return f"Grupta en az {req['min_msgs']} mesajın olmalı (şu an {n})."
    if req.get('min_days'):
        with get_db() as conn:
            r = conn.execute("SELECT joined_at FROM users WHERE chat_id = ? AND user_id = ?", (chat_id, user.id)).fetchone()
        if not r or time.time() - (r['joined_at'] or time.time()) < req['min_days'] * 86400:
            return f"En az {req['min_days']} gündür grupta olmalısın."
    return None

async def giveaway_button(update: Update, context):
    query = update.callback_query
    chat_id = query.data.split("|", 1)[1]
    row = _gw_row(chat_id) if get_channel_settings(chat_id) else None
    if not row:
        await query.answer("Bu çekiliş sona erdi.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return
    user = query.from_user
    participants = json.loads(row['participants'])
    if user.id in participants:
        await query.answer("Zaten katıldın, bol şans! 🍀")
        return
    why = await giveaway_eligible(chat_id, user, json.loads(row.get('req') or '{}'))
    if why:
        await query.answer(why, show_alert=True)
        return
    async with _db_lock:
        with get_db() as conn:
            cur = conn.execute("SELECT participants FROM giveaways WHERE chat_id = ?", (chat_id,)).fetchone()
            if not cur:
                await query.answer("Bu çekiliş sona erdi.", show_alert=True)
                return
            participants = json.loads(cur['participants'])
            if user.id not in participants:
                participants.append(user.id)
                conn.execute("UPDATE giveaways SET participants = ? WHERE chat_id = ?", (json.dumps(participants), chat_id))
                conn.commit()
    await query.answer("🎉 Çekilişe katıldın, bol şans!")
    try:
        await query.edit_message_text(_gw_text(row, len(participants)), parse_mode=ParseMode.HTML,
                                      reply_markup=_gw_markup(chat_id, row, len(participants)))
    except TelegramError as e:
        logger.debug(f"Çekiliş mesajı güncellenemedi: {e}")

async def finish_giveaway(chat_id: str, winners_count: int | None = None, thread: dict | None = None) -> str:
    """Çekilişi bitirir; hâlâ grupta olan katılımcılar arasından kazanan seçer. Dönüş: duyuru metni."""
    row = _gw_row(chat_id)
    if not row:
        return ''
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM giveaways WHERE chat_id = ?", (chat_id,))
            conn.commit()
    participants = json.loads(row['participants'])
    want = winners_count or row.get('winners') or 1
    pool = participants[:]
    random.shuffle(pool)
    names = []
    for wid in pool:
        if len(names) >= want:
            break
        try:
            m = await bot.get_chat_member(chat_id, wid)
        except TelegramError:
            continue
        if m.status in ('left', 'kicked'):
            continue
        names.append(mention(m.user))
    prize = row.get('prize') or ''
    try:
        await bot.edit_message_text(_gw_text(row, len(participants)) + "\n\n✅ <b>Çekiliş bitti</b>", chat_id=chat_id,
                                    message_id=row['message_id'], parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug(f"Çekiliş mesajı kapatılamadı: {e}")
    if not names:
        text = "🎉 Çekiliş sona erdi! Şartları sağlayan katılımcı yoktu."
    else:
        text = (f"🎉 <b>Çekiliş sona erdi!</b>{' — ' + html.escape(prize) if prize else ''}\n🏆 Kazanan"
                + ("lar" if len(names) > 1 else "") + ": " + ", ".join(names) + f"\n👥 Katılımcı: {len(participants)}")
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, **(thread or {}))
    except TelegramError as e:
        logger.debug(f"Çekiliş sonucu gönderilemedi: {e}")
    await send_log(chat_id, f"🏆 Çekiliş bitti: {', '.join(names) or 'kazanan yok'} | {chat_id}", ParseMode.HTML)
    return text

async def giveaway_end(update: Update, context):
    """/cekilis_bitir [kazanan] — aktif çekilişi hemen bitirir."""
    message = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await message.reply_text("Yetkin yok!")
        return
    if not _gw_row(chat_id):
        await message.reply_text("Aktif çekiliş yok.")
        return
    n = max(1, min(GW_MAX_WINNERS, int(context.args[0]))) if context.args and context.args[0].isdigit() else None
    await finish_giveaway(chat_id, n, thread_kw(message, chat_id))
    if message.chat.type == 'private':
        await message.reply_text("✅ Çekiliş bitirildi.")

async def giveaway_job(context):
    with get_db() as conn:
        due = [r['chat_id'] for r in conn.execute(
            "SELECT chat_id FROM giveaways WHERE ends_at IS NOT NULL AND ends_at <= ?", (time.time(),))]
    for cid in due:
        await finish_giveaway(cid)

# ═══════════════════════════ GRAFİKLİ İSTATİSTİK ═══════════════════════════
# Tek renk tonu (mavi), katılan/ayrılan için ikinci ton; açık zemin; ızgara ve eksenler geri planda.
CHART_COLORS = {'surface': '#fcfcfb', 'ink': '#0b0b0b', 'ink2': '#52514e', 'grid': '#e4e3de',
                'series': '#2a78d6', 'series2': '#eb6834'}

def log_member_event(chat_id: str, user_id: int, kind: str):
    with get_db() as conn:
        conn.execute("INSERT INTO member_events (chat_id, user_id, kind, at) VALUES (?, ?, ?, ?)",
                     (str(chat_id), user_id, kind, time.time()))
        conn.commit()

def _tr_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ_TR).strftime('%d.%m')

def stats_data(chat_id: str) -> dict:
    now = time.time()
    since30, since7 = now - 30 * 86400, now - 7 * 86400
    days = [_tr_day(now - i * 86400) for i in range(29, -1, -1)]
    daily = dict.fromkeys(days, 0)
    hours = [0] * 24
    joins, leaves = dict.fromkeys(days, 0), dict.fromkeys(days, 0)
    with get_db() as conn:
        for r in conn.execute("SELECT sent_at FROM message_stats WHERE chat_id = ? AND sent_at >= ?", (chat_id, since30)):
            d = _tr_day(r['sent_at'])
            if d in daily:
                daily[d] += 1
            hours[datetime.fromtimestamp(r['sent_at'], TZ_TR).hour] += 1
        for r in conn.execute("SELECT kind, at FROM member_events WHERE chat_id = ? AND at >= ?", (chat_id, since30)):
            d = _tr_day(r['at'])
            target = joins if r['kind'] == 'join' else leaves
            if d in target:
                target[d] += 1
        top = conn.execute("""SELECT user_id, MAX(first_name) AS name, COUNT(*) AS n FROM message_stats
                              WHERE chat_id = ? AND sent_at >= ? GROUP BY user_id ORDER BY n DESC LIMIT 8""",
                           (chat_id, since7)).fetchall()
        active7 = conn.execute("SELECT COUNT(DISTINCT user_id) FROM message_stats WHERE chat_id = ? AND sent_at >= ?",
                               (chat_id, since7)).fetchone()[0]
    return {'days': days, 'daily': [daily[d] for d in days], 'hours': hours,
            'joins': [joins[d] for d in days], 'leaves': [leaves[d] for d in days],
            'top': [(re.sub(r'\s+', ' ', re.sub(r'[^\u0000-\uffff]', '', r['name'] or '')).strip()[:18] or str(r['user_id']), r['n'])
                    for r in top],
            'msgs7': sum(daily[d] for d in days[-7:]), 'msgs30': sum(daily.values()), 'active7': active7,
            'joins30': sum(joins.values()), 'leaves30': sum(leaves.values())}

def render_stats_chart(d: dict, title: str) -> bytes | None:
    """2×2 küçük grafik: günlük mesaj (30 gün) · saatlere göre · katılan/ayrılan · en aktif 8 kişi (7 gün). PNG."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator
    except Exception as e:
        logger.debug(f"matplotlib yok, grafik atlanıyor: {e}")
        return None
    import io
    c = CHART_COLORS
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9, 'text.color': c['ink'], 'axes.labelcolor': c['ink2'],
                         'xtick.color': c['ink2'], 'ytick.color': c['ink2'], 'axes.edgecolor': c['grid']})
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5), dpi=110, facecolor=c['surface'])
    fig.suptitle(title, fontsize=13, fontweight='bold', color=c['ink'], x=0.02, ha='left')

    def style(ax, name):
        ax.set_facecolor(c['surface'])
        ax.set_title(name, loc='left', fontsize=10.5, color=c['ink'], pad=8)
        for side in ('top', 'right', 'left'):
            ax.spines[side].set_visible(False)
        ax.grid(axis='y', color=c['grid'], linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(length=0)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))  # sayımlar: 0,5 mesaj olmaz

    x = list(range(len(d['days'])))
    ax = axes[0][0]
    style(ax, "Günlük mesaj — son 30 gün")
    ax.bar(x, d['daily'], width=0.75, color=c['series'], edgecolor=c['surface'], linewidth=1)
    ax.set_xticks(x[::5] + [x[-1]])
    ax.set_xticklabels([d['days'][i] for i in x[::5]] + [d['days'][-1]])
    if max(d['daily']) > 0:
        i = max(range(len(x)), key=lambda k: d['daily'][k])
        ax.annotate(str(d['daily'][i]), (i, d['daily'][i]), textcoords='offset points', xytext=(0, 3), ha='center',
                    fontsize=8, color=c['ink2'])

    ax = axes[0][1]
    style(ax, "Saatlere göre mesaj (Türkiye saati, 30 gün)")
    ax.bar(range(24), d['hours'], width=0.75, color=c['series'], edgecolor=c['surface'], linewidth=1)
    ax.set_xticks(range(0, 24, 3))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 3)])

    ax = axes[1][0]
    style(ax, "Katılan / ayrılan — son 30 gün")
    w = 0.38
    ax.bar([i - w / 2 for i in x], d['joins'], width=w, color=c['series'], label=f"Katılan ({sum(d['joins'])})")
    ax.bar([i + w / 2 for i in x], d['leaves'], width=w, color=c['series2'], label=f"Ayrılan ({sum(d['leaves'])})")
    ax.set_xticks(x[::5] + [x[-1]])
    ax.set_xticklabels([d['days'][i] for i in x[::5]] + [d['days'][-1]])
    ax.legend(frameon=False, loc='upper left', fontsize=8.5)

    ax = axes[1][1]
    style(ax, "En aktif üyeler — son 7 gün")
    ax.grid(False)
    ax.spines['bottom'].set_visible(False)
    top = list(reversed(d['top']))
    if top:
        ax.barh(range(len(top)), [n for _, n in top], height=0.6, color=c['series'])
        ax.set_yticks(range(len(top)))
        ax.set_yticklabels([n for n, _ in top])
        ax.set_xticks([])
        for i, (_, n) in enumerate(top):
            ax.annotate(str(n), (n, i), textcoords='offset points', xytext=(4, 0), va='center', fontsize=8, color=c['ink2'])
    else:
        ax.set_xticks([])
        ax.set_yticks([])
        ax.text(0.5, 0.5, "Henüz veri yok", ha='center', va='center', color=c['ink2'], transform=ax.transAxes)
    for a in axes.flat:
        for lbl in a.get_yticklabels():
            lbl.set_color(c['ink2'])
    fig.tight_layout(rect=(0, 0, 1, 0.95), h_pad=2.5, w_pad=2)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', facecolor=c['surface'])
    plt.close(fig)
    return buf.getvalue()

async def stats(update: Update, context):
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await msg.reply_text("Yetkin yok!")
        return
    channel = get_channel_settings(chat_id)
    s = channel['stats']
    d = stats_data(chat_id)
    title = await _chat_title(chat_id)
    text = (f"📊 <b>{html.escape(title)}</b>\n"
            f"💬 Mesaj: 7 gün <b>{d['msgs7']}</b> · 30 gün <b>{d['msgs30']}</b> · aktif üye (7 gün): <b>{d['active7']}</b>\n"
            f"👥 30 günde katılan <b>{d['joins30']}</b> · ayrılan <b>{d['leaves30']}</b>\n"
            f"🛡 Toplam: 🚫 {s.get('bans', 0)} ban · 🔇 {s.get('mutes', 0)} susturma · 👢 {s.get('kicks', 0)} atma · "
            f"🔁 {s.get('spams', 0)} spam · 🙋 {s.get('requests', 0)} istek")
    png = await asyncio.to_thread(render_stats_chart, d, f"{title} — istatistik")
    if png:
        await msg.reply_photo(png, caption=text, parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(text, parse_mode=ParseMode.HTML)

# ── Notlar (/save, /not, #isim) ──
_NOTE_NAME = re.compile(r'^[\w\-]{1,32}$')

async def cmd_save_note(update: Update, context):
    """/save isim metin  veya bir mesaja yanıt olarak /save isim"""
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_content'):
        return
    if not context.args or not _NOTE_NAME.match(context.args[0]):
        await msg.reply_text("Kullanım: /save <isim> <metin>  (veya bir mesaja yanıt: /save <isim>)")
        return
    name = context.args[0].lower()
    content, file_id, file_type = None, None, None
    parts = msg.text_html.split(maxsplit=2) if msg.text else []
    if len(parts) > 2:
        content = parts[2]
    elif msg.reply_to_message:
        r = msg.reply_to_message
        content = r.text_html or r.caption_html or None
        for ft in ('photo', 'video', 'animation', 'document', 'sticker', 'voice', 'audio'):
            obj = getattr(r, ft, None)
            if obj:
                file_id = obj[-1].file_id if ft == 'photo' else obj.file_id
                file_type = ft
                break
    if not content and not file_id:
        await msg.reply_text("Not içeriği boş olamaz.")
        return
    async with _db_lock:
        with get_db() as conn:
            conn.execute("""INSERT OR REPLACE INTO notes (chat_id, name, content, file_id, file_type, created_by, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (chat_id, name, content, file_id, file_type, update.effective_user.id, time.time()))
            conn.commit()
    await msg.reply_text(f"✅ Not kaydedildi: #{name}")

async def _send_note(chat_id: str, name: str, reply_to):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE chat_id = ? AND name = ?", (chat_id, name.lower())).fetchone()
    if not row:
        return False
    rich = build_rich(row['content'] or '', {'type': row['file_type'], 'id': row['file_id']} if row['file_id'] else None)
    await send_rich(reply_to.chat_id, rich, reply_msg=reply_to, users=[getattr(reply_to, 'from_user', None)],
                    title=await _chat_title(chat_id))
    return True

async def cmd_get_note(update: Update, context):
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not context.args:
        await msg.reply_text("Kullanım: /not <isim>")
        return
    if not await _send_note(chat_id, context.args[0], msg):
        await msg.reply_text("Böyle bir not yok. /notlar ile listeyi gör.")

async def hashtag_note_handler(update: Update, context):
    msg = update.effective_message
    if not msg or not msg.text or msg.chat.type == 'private':
        return
    m = re.match(r'^#([\w\-]{1,32})\b', msg.text)
    if m:
        await _send_note(str(msg.chat_id), m.group(1), msg)

async def cmd_notes(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id:
        return
    with get_db() as conn:
        rows = conn.execute("SELECT name FROM notes WHERE chat_id = ? ORDER BY name", (chat_id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("Kayıtlı not yok. /save ile ekle.")
        return
    await update.effective_message.reply_text(
        "📝 <b>Notlar</b>\n<blockquote expandable>" + "\n".join(f"• #{r['name']}" for r in rows) +
        "</blockquote>\nGörmek için grupta #isim yaz.", parse_mode=ParseMode.HTML)

async def cmd_delete_note(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id:
        await update.effective_message.reply_text("Önce /kanal ile seç!")
        return
    if not await require(update, chat_id, 'can_content'):
        return
    if not context.args:
        await update.effective_message.reply_text("Kullanım: /notsil <isim>")
        return
    async with _db_lock:
        with get_db() as conn:
            n = conn.execute("DELETE FROM notes WHERE chat_id = ? AND name = ?", (chat_id, context.args[0].lower())).rowcount
            conn.commit()
    await update.effective_message.reply_text("✅ Not silindi." if n else "Böyle bir not yok.")

# ═══════════════════════════ HIZLI KURULUM ═══════════════════════════
# Bot eklenince ekleyen kişiye özelden gelir; /kurulum ile tekrar açılır. Butonlarla: paket → hoş geldin → korumalar → özet.
SETUP_TOGGLES = [  # (ayar anahtarı, etiket)
    ('anti_link', "🔗 Link engeli"), ('anti_spam_flood', "🌊 Flood"), ('spam_protection', "🔁 Tekrar spam"),
    ('captcha_enabled', "🧩 Captcha"), ('media_shield', "🔞 Uygunsuz medya"), ('anti_forward', "↪️ İletme engeli"),
    ('anti_raid', "🚨 Raid koruması"), ('newbie_minutes', "🐣 Yeni üye kısıtı (60 dk)"), ('word_ban_enabled', "🔤 Kelime filtresi"),
]
SETUP_PRESETS = {
    'light':  ("🟢 Hafif", {'anti_link', 'anti_spam_flood'}),
    'normal': ("🟡 Normal", {'anti_link', 'anti_spam_flood', 'spam_protection', 'captcha_enabled', 'media_shield'}),
    'strict': ("🔴 Sıkı", {'anti_link', 'anti_spam_flood', 'spam_protection', 'captcha_enabled', 'media_shield',
                          'newbie_minutes', 'anti_forward', 'anti_raid'}),
}

def _setup_state(context, cid: str) -> dict:
    st = context.user_data.get('setup')
    if not st or st.get('cid') != cid:
        s = (get_channel_settings(cid) or {}).get('settings', {})
        st = {'cid': cid, 'preset': None, 'welcome': 'def' if s.get('welcome_enabled', True) else 'off',
              'on': {k for k, _ in SETUP_TOGGLES if s.get(k)}}
        context.user_data['setup'] = st
    return st

async def render_setup(cid: str, step: str, st: dict):
    title = html.escape(await _chat_title(cid))
    rows = []
    if step == 'start':
        text = (f"⚡ <b>Hızlı kurulum</b> — {title}\n\n<b>1/3</b> Bir koruma paketi seç. Sonra hepsini tek tek değiştirebilirsin.\n\n"
                "🟢 <b>Hafif</b>: link + flood\n🟡 <b>Normal</b>: + tekrar spam, captcha, uygunsuz medya\n"
                "🔴 <b>Sıkı</b>: + yeni üye kısıtı, iletme engeli, raid koruması")
        rows = [[ibtn(SETUP_PRESETS[k][0], f"ks|{cid}|p|{k}", BLUE) for k in ('light', 'normal', 'strict')],
                [ibtn("⚙️ Kendim seçeyim", f"ks|{cid}|p|custom", BLUE)],
                [ibtn("✖️ Şimdilik geç", f"ks|{cid}|x", RED)]]
    elif step == 'welcome':
        cur = {'off': "Kapalı", 'def': "Varsayılan", 'own': "Kendi metnim"}[st['welcome']]
        text = (f"⚡ <b>Hızlı kurulum</b> — {title}\n\n<b>2/3</b> Yeni gelenlere hoş geldin mesajı gönderilsin mi?\n"
                f"Şu an: <b>{cur}</b>")
        rows = [[ibtn("🚫 Kapalı", f"ks|{cid}|w|off", RED), ibtn("👋 Varsayılan", f"ks|{cid}|w|def", GREEN)],
                [ibtn("✏️ Kendi metnimi yazacağım", f"ks|{cid}|w|own", BLUE)]]
    elif step == 'prot':
        text = (f"⚡ <b>Hızlı kurulum</b> — {title}\n\n<b>3/3</b> Hangi korumalar açık olsun? Dokunarak aç/kapat.")
        rows = [[toggle_btn(lbl, k in st['on'], f"ks|{cid}|t|{k}")] for k, lbl in SETUP_TOGGLES]
        rows.append([ibtn("➡️ Devam", f"ks|{cid}|s|sum", GREEN)])
    else:  # özet
        on = [lbl for k, lbl in SETUP_TOGGLES if k in st['on']]
        wel = {'off': "kapalı", 'def': "varsayılan metin", 'own': "kendi metnin"}[st['welcome']]
        text = (f"⚡ <b>Hızlı kurulum</b> — {title}\n\n<b>Özet</b>\n"
                f"🛡 Açık korumalar: {', '.join(on) if on else 'yok'}\n👋 Hoş geldin: {wel}\n\n"
                "Uygula'ya bas, ayarlar kaydedilsin. Sonradan /settings ile her şeyi değiştirebilirsin.")
        rows = [[ibtn("✅ Uygula", f"ks|{cid}|a|go", GREEN)],
                [ibtn("🛡 Korumaları değiştir", f"ks|{cid}|s|prot", BLUE), ibtn("👋 Hoş geldin", f"ks|{cid}|s|welcome", BLUE)]]
    return text, InlineKeyboardMarkup(rows)

def apply_setup(cid: str, st: dict):
    channel = get_channel_settings(cid)
    s = channel['settings']
    for k, _ in SETUP_TOGGLES:
        if k == 'newbie_minutes':
            s[k] = 60 if k in st['on'] else 0
        else:
            s[k] = k in st['on']
    s['welcome_enabled'] = st['welcome'] != 'off'
    if st['welcome'] == 'def':
        s['welcome_msg'] = _default_channel_settings()['welcome_msg']
        s.pop('welcome_rich', None)
    channel['settings'] = s
    save_channel_settings(cid, channel)

async def send_setup_wizard(chat_id_dm: int, cid: str, context) -> bool:
    """Kurulum sihirbazını özelden gönderir. Bot o kişiye yazamıyorsa False."""
    st = _setup_state(context, cid) if context else {'cid': cid, 'preset': None, 'welcome': 'def', 'on': set()}
    text, markup = await render_setup(cid, 'start', st)
    try:
        await bot.send_message(chat_id_dm, text, reply_markup=markup, parse_mode=ParseMode.HTML)
        return True
    except TelegramError as e:
        logger.debug(f"Kurulum sihirbazı gönderilemedi {chat_id_dm}: {e}")
        return False

async def cmd_kurulum(update: Update, context):
    """/kurulum — hızlı kurulum. Grupta yazılırsa özelden açmak için buton verir."""
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type != 'private':
        cid = str(chat.id)
        if not get_channel_settings(cid):
            await msg.reply_text("Bu grup kayıtlı değil. Önce botu yönetici yap.")
            return
        if not await require(update, cid, 'can_manage_settings'):
            return
        await msg.reply_text("⚡ Hızlı kurulum bota özelden yapılır:", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⚡ Kurulumu aç", url=f"https://t.me/{context.bot.username}?start=kurulum_{cid}")]]))
        return
    cid = context.user_data.get('selected_channel')
    if context.args and context.args[0].startswith('kurulum_'):
        cid = context.args[0][len('kurulum_'):]
    if not cid or not get_channel_settings(cid):
        await msg.reply_text("Önce 🛡 Gruplarım ile bir grup seç, sonra /kurulum yaz.")
        return
    if not await require(update, cid, 'can_manage_settings'):
        return
    context.user_data['selected_channel'] = cid
    context.user_data.pop('setup', None)
    st = _setup_state(context, cid)
    text, markup = await render_setup(cid, 'start', st)
    await msg.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

async def setup_callback(update: Update, context):
    """ks|cid|op|arg — hızlı kurulum butonları."""
    query = update.callback_query
    parts = query.data.split('|')
    cid, op, arg = parts[1], parts[2], (parts[3] if len(parts) > 3 else '')
    if not get_channel_settings(cid):
        await query.answer("Grup bulunamadı.", show_alert=True)
        return
    if not has_specific_permission(cid, query.from_user.id, 'can_manage_settings'):
        await deny(update, 'can_manage_settings')
        return
    st = _setup_state(context, cid)
    step, note = 'start', ''
    if op == 'x':
        await query.answer()
        await query.edit_message_text("Kurulum geçildi. İstediğin zaman /kurulum ile açabilirsin.")
        return
    if op == 'p':
        if arg in SETUP_PRESETS:
            st['preset'], st['on'] = arg, set(SETUP_PRESETS[arg][1])
        else:
            st['preset'] = 'custom'
        step = 'welcome'
    elif op == 'w':
        st['welcome'] = arg if arg in ('off', 'def', 'own') else 'def'
        step = 'prot' if st['preset'] == 'custom' else 'sum'
        if arg == 'own':
            await _ask_input(query, context, cid, 'welcome')  # sorguyu kendisi yanıtlar
            text, markup = await render_setup(cid, step, st)
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            return
    elif op == 't':
        st['on'] ^= {arg} if arg in dict(SETUP_TOGGLES) else set()
        step = 'prot'
    elif op == 's':
        step = arg if arg in ('welcome', 'prot', 'sum') else 'sum'
    elif op == 'a':
        apply_setup(cid, st)
        context.user_data.pop('setup', None)
        await query.answer("✅ Ayarlar kaydedildi")
        await query.edit_message_text(
            f"✅ <b>Kurulum tamam!</b> — {html.escape(await _chat_title(cid))}\n\n"
            "Ayrıntılı ayarlar için /settings · Otomatik yanıtlar için grupta /filter", parse_mode=ParseMode.HTML)
        await send_log(cid, f"⚡ Hızlı kurulum uygulandı | {mention(query.from_user)}", ParseMode.HTML)
        return
    await query.answer(note)
    text, markup = await render_setup(cid, step, st)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if 'not modified' not in str(e).lower():
            raise

# ── Metin tabanlı otomatik yanıt (/filter) — her sohbette ayrı ──
FILTER_LIMIT = 150
FILTER_COOLDOWN = 5  # aynı filtre aynı grupta en fazla 5 sn'de bir yanıt verir
FILTER_MEDIA = ('sticker', 'photo', 'animation', 'video', 'voice', 'audio', 'document', 'video_note')
_filter_cache: dict = {}
_filter_last: dict = {}

def filter_norm(text: str) -> str:
    """Eşleştirme biçimi: Türkçe/büyük-küçük harf/noktalama farkı yok sayılır ("Selam!" == "selam")."""
    t = unicodedata.normalize('NFKC', text or '').replace('İ', 'i').replace('I', 'ı').lower()
    t = re.sub(r'[^\w\s]', ' ', t).translate(_TR_FOLD)  # noktalama leetspeak'e çevrilmeden atılır ("Selam!" → "selam")
    t = re.sub(r'(.)\1{2,}', r'\1', t)  # "selaaam" → "selam"
    return re.sub(r'\s+', ' ', t).strip()

def chat_filters(chat_id: str) -> list:
    chat_id = str(chat_id)
    if chat_id not in _filter_cache:
        with get_db() as conn:
            _filter_cache[chat_id] = [dict(r) for r in conn.execute(
                "SELECT rowid AS fid, * FROM chat_filters WHERE chat_id = ? ORDER BY LENGTH(trigger_norm) DESC", (chat_id,))]
    return _filter_cache[chat_id]

def _filter_changed(chat_id: str):
    _filter_cache.pop(str(chat_id), None)

def match_filter(chat_id: str, text: str):
    norm = filter_norm(text)
    if not norm:
        return None
    for f in chat_filters(chat_id):
        if f['mode'] == 'exact' and norm == f['trigger_norm']:
            return f
        if f['mode'] == 'contains' and re.search(r'(?<!\w)' + re.escape(f['trigger_norm']) + r'(?!\w)', norm):
            return f
    return None

def _parse_trigger(raw: str):
    """'selam' → tam eşleşme; '*selam*' → içerirse. Dönüş: (görünen, normal, mod) veya None."""
    raw = raw.strip()
    mode = 'exact'
    if len(raw) > 2 and raw.startswith('*') and raw.endswith('*'):
        raw, mode = raw[1:-1].strip(), 'contains'
    norm = filter_norm(raw)
    if not norm or len(raw) > 100:
        return None
    return raw, norm, mode

def _tail_html(msg, text: str, tail_plain: str) -> str:
    """Komut metninin sonundaki tail_plain kısmının biçimli HTML'i."""
    idx = len(text.rstrip()) - len(tail_plain)
    return html_after(msg, idx) if idx >= 0 and text.rstrip().endswith(tail_plain) else html.escape(tail_plain, quote=False)

def _split_filter_args(text: str):
    """'/filter "iyi akşamlar" Size de' → ('iyi akşamlar', 'Size de'); '/filter selam Aleyküm selam' → ('selam', ...)"""
    body = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ''
    body = body.strip()
    m = re.match(r'^["“”\'](.+?)["“”\']\s*(.*)$', body, re.S)
    if m:
        return m.group(1), m.group(2).strip()
    first, _, rest = body.partition(' ')
    return first, rest.strip()

async def cmd_filter(update: Update, context):
    """/filter — bir mesaja yanıtla: tetikleyici o mesaj, yanıt yazdığın metin.
    Medyaya (sticker/foto…) yanıtla: /filter <tetikleyici> → bot o medya ile cevap verir.
    Yanıtsız: /filter <tetikleyici> <yanıt> · çok kelimeli tetikleyici: /filter "iyi akşamlar" yanıt · içerirse: *kelime*"""
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    if not await require(update, chat_id, 'can_filters'):
        return
    text = msg.text or ''
    reply = msg.reply_to_message
    trigger_raw = answer = None
    file_id = file_type = None
    if reply and any(getattr(reply, ft, None) for ft in FILTER_MEDIA):
        trigger_raw, extra = _split_filter_args(text)
        for ft in FILTER_MEDIA:
            obj = getattr(reply, ft, None)
            if obj:
                file_id, file_type = (obj[-1].file_id if ft == 'photo' else obj.file_id), ft
                break
        answer = _tail_html(msg, text, extra) if extra else msg_html(reply)
    elif reply and (reply.text or reply.caption):
        trigger_raw = reply.text or reply.caption
        answer = cmd_args_html(msg, 1)
    else:
        trigger_raw, answer = _split_filter_args(text)
        answer = _tail_html(msg, text, answer) if answer else ''
    parsed = _parse_trigger(trigger_raw or '')
    if not parsed or (not answer and not file_id):
        await msg.reply_text(
            "🧩 <b>Filtre kullanımı</b>\n"
            "• Bir mesaja yanıt: <code>/filter Aleyküm selam</code> (tetikleyici yanıtladığın mesaj)\n"
            "• Yanıtsız: <code>/filter selam Aleyküm selam</code>\n"
            "• Çok kelimeli: <code>/filter \"iyi akşamlar\" Size de!</code>\n"
            "• Mesajın içinde geçerse: <code>/filter *selam* Merhaba!</code>\n"
            "• Sticker/foto/GIF ile cevap: medyaya yanıtla → <code>/filter selam</code>\n"
            "• Butonlar: cevabın altına satır satır <code>Kanal - https://t.me/kanal</code>\n"
            "Değişkenler: <code>{kullanıcı}</code> <code>{ad}</code> <code>{grup}</code> · Biçim (kalın, link…) korunur\n"
            "Liste: /filters · Sil: /stop selam · Hepsini sil: /stopall", parse_mode=ParseMode.HTML)
        return
    shown, norm, mode = parsed
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM chat_filters WHERE chat_id = ? AND trigger_norm = ?", (chat_id, norm)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM chat_filters WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    if not exists and count >= FILTER_LIMIT:
        await msg.reply_text(f"⚠️ Bu grupta en fazla {FILTER_LIMIT} filtre olabilir. /stop ile eskileri sil.")
        return
    async with _db_lock:
        with get_db() as conn:
            conn.execute("""INSERT OR REPLACE INTO chat_filters
                            (chat_id, trigger_norm, trigger_text, mode, reply_text, file_id, file_type, created_by, created_at,
                             is_html) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                         (chat_id, norm, shown, mode, (answer or '')[:3500], file_id, file_type,
                          update.effective_user.id, time.time()))
            conn.commit()
    _filter_changed(chat_id)
    how = "içinde geçince" if mode == 'contains' else "yazılınca"
    await msg.reply_text(f"✅ Filtre {'güncellendi' if exists else 'eklendi'}: <b>{html.escape(shown)}</b> {how} cevap verilecek.",
                         parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"🧩 Filtre eklendi: {html.escape(shown)} | {mention(update.effective_user)}", ParseMode.HTML)

async def cmd_filters(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.effective_message.reply_text("Önce /kanal ile grup seç!")
        return
    items = chat_filters(chat_id)
    if not items:
        await update.effective_message.reply_text("Bu grupta filtre yok. Eklemek için: /filter")
        return
    lines = [f"• {'*' if f['mode'] == 'contains' else ''}{html.escape(f['trigger_text'])}"
             f"{'*' if f['mode'] == 'contains' else ''}{' 🖼' if f['file_id'] else ''}" for f in
             sorted(items, key=lambda f: f['trigger_norm'])]
    await update.effective_message.reply_text(
        f"🧩 <b>Filtreler</b> ({len(items)})\n<blockquote expandable>" + "\n".join(lines) +
        "</blockquote>\nSilmek için: /stop &lt;tetikleyici&gt;", parse_mode=ParseMode.HTML)

async def cmd_stop_filter(update: Update, context):
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    if not await require(update, chat_id, 'can_filters'):
        return
    raw = (msg.text or '').split(maxsplit=1)
    trig = raw[1].strip().strip('"“”\'') if len(raw) > 1 else (msg.reply_to_message.text if msg.reply_to_message else '')
    parsed = _parse_trigger(trig or '')
    if not parsed:
        await msg.reply_text("Kullanım: /stop <tetikleyici>")
        return
    async with _db_lock:
        with get_db() as conn:
            n = conn.execute("DELETE FROM chat_filters WHERE chat_id = ? AND trigger_norm = ?", (chat_id, parsed[1])).rowcount
            conn.commit()
    _filter_changed(chat_id)
    await msg.reply_text("✅ Filtre silindi." if n else "Böyle bir filtre yok. /filters ile listeye bak.")

async def cmd_stopall(update: Update, context):
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    if not await require(update, chat_id, 'can_filters'):
        return
    n = len(chat_filters(chat_id))
    if not n:
        await msg.reply_text("Bu grupta filtre yok.")
        return
    await msg.reply_text(f"⚠️ Bu gruptaki <b>{n}</b> filtrenin hepsi silinsin mi?", parse_mode=ParseMode.HTML,
                         reply_markup=InlineKeyboardMarkup([[ibtn("🗑 Evet, hepsini sil", f"fall|{chat_id}|y", RED),
                                                             ibtn("↩️ Vazgeç", f"fall|{chat_id}|n")]]))

async def filter_clear_callback(update: Update, context):
    query = update.callback_query
    _, chat_id, ans = query.data.split('|')
    if not has_specific_permission(chat_id, query.from_user.id, 'can_filters'):
        await deny(update, 'can_filters')
        return
    await query.answer()
    if ans != 'y':
        await query.edit_message_text("Vazgeçildi.")
        return
    async with _db_lock:
        with get_db() as conn:
            n = conn.execute("DELETE FROM chat_filters WHERE chat_id = ?", (chat_id,)).rowcount
            conn.commit()
    _filter_changed(chat_id)
    await query.edit_message_text(f"🗑 {n} filtre silindi.")
    await send_log(chat_id, f"🧹 Tüm filtreler silindi ({n}) | {mention(query.from_user)}", ParseMode.HTML)

async def filter_reply_handler(update: Update, context):
    """Gruba gelen mesaj bir filtreyle eşleşirse bot cevap verir."""
    msg = update.effective_message
    if not msg or msg.chat.type == 'private':
        return
    text = msg.text or msg.caption
    if not text or text.startswith('/'):
        return
    chat_id = str(msg.chat_id)
    if not get_channel_settings(chat_id):
        return
    f = match_filter(chat_id, text)
    if not f:
        return
    key = (chat_id, f['trigger_norm'])
    now = time.time()
    if now - _filter_last.get(key, 0) < FILTER_COOLDOWN:
        return
    _filter_last[key] = now
    body = (f['reply_text'] or '') if f.get('is_html') else html.escape(f['reply_text'] or '', quote=False)
    rich = build_rich(body, {'type': f['file_type'], 'id': f['file_id']} if f['file_id'] else None)
    await send_rich(chat_id, rich, reply_msg=msg, users=[msg.from_user], title=msg.chat.title)

# ═══════════════════════════ KLON BOT ═══════════════════════════
# Akış: kullanıcı ana botta 🤖 Klon → BotFather token'ını gönderir → bot sahibine isteyen kişi, token ve bot ismi
# Onayla/Reddet ile gelir → onaylanırsa klon aynı altyapıyla açılır, reddedilirse açılmaz. Klon sahibi marka adı, karşılama metni, destek
# linki ve yardım başlığını değiştirebilir; kendi botunda /duyuru, /gban, /panel kullanır.
CLONE_TOKEN_RE = re.compile(r'^\d{6,12}:[A-Za-z0-9_-]{30,50}$')
CLONE_LIMIT_PER_USER = 1
CLONE_FIELDS = {  # alan: (etiket, istem, en fazla uzunluk)
    'brand':        ("🏷 Marka adı", "Botunun mesajlarda görünecek adını yaz (ör. Alfa Guard).", 32),
    'start_text':   ("👋 Karşılama metni", "/start yazınca görünecek karşılama metnini yaz.", 800),
    'support_link': ("🔗 Destek linki", "Destek grubu/kanal linkini yaz (https://t.me/...). Kaldırmak için: -", 120),
    'help_title':   ("❓ Yardım başlığı", "Yardım menüsünün başlığını yaz.", 60),
}

def clone_row(bot_id: int) -> dict | None:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM clones WHERE bot_id = ?", (bot_id,)).fetchone()
    return dict(r) if r else None

def user_clones(user_id: int) -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM clones WHERE owner_id = ? AND status != 'deleted' ORDER BY created_at", (user_id,))]

def pending_request(user_id: int) -> dict | None:
    """Kullanıcının bot sahibinin onayını bekleyen klon isteği."""
    with get_db() as conn:
        r = conn.execute("SELECT * FROM clone_requests WHERE user_id = ? AND status = 'pending' AND token IS NOT NULL "
                         "ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
    return dict(r) if r else None

def _mask_token(token: str | None) -> str:
    return f"{(token or '').split(':')[0]}:••••••" if token else "-"

def _clone_req_text(r: dict, decided: str = '') -> str:
    """Bot sahibine giden onay mesajı. Karar verilince token gizlenir."""
    who = f"@{html.escape(r['username'])}" if r.get('username') else html.escape(r.get('first_name') or 'Kullanıcı')
    tok = _mask_token(r.get('token')) if decided else (r.get('token') or '-')
    text = (f"🤖 <b>Klon bot onayı</b> #{r['id']}\n\n"
            f"👤 Botu açmak isteyen kişi: {who} — ID: <code>{r['user_id']}</code>\n"
            f"🔑 Bot token: <code>{html.escape(tok)}</code>\n"
            f"🏷 Bot ismi: {html.escape(r.get('bot_name') or '-')} (@{html.escape(r.get('bot_username') or '?')})")
    return text + (f"\n\n— {decided}" if decided else "")

async def _edit_req_msg(r: dict, decided: str):
    if not r.get('admin_msg_id'):
        return
    try:
        await (MAIN_BOT or bot).edit_message_text(_clone_req_text(r, decided), chat_id=FOUNDER_ID,
                                                  message_id=r['admin_msg_id'], parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug(f"Klon isteği mesajı güncellenemedi: {e}")

async def _main_send(chat_id, text, **kw):
    """Ana bottan mesaj (klon bildirimleri, bot sahibine istekler). Gönderilemezse sessizce geçer."""
    try:
        return await (MAIN_BOT or bot).send_message(chat_id, text, **kw)
    except Exception as e:
        logger.debug(f"Ana bot mesajı gönderilemedi {chat_id}: {e}")

async def start_clone(row: dict) -> str | None:
    """Klonu başlatır. Hata metni ya da None döner."""
    bid = row['bot_id']
    if bid in CLONE_APPS:
        return None
    app = (Application.builder().token(row['token']).rate_limiter(UlusRateLimiter(max_retries=3))
           .job_queue(UlusJobQueue()).build())
    register_handlers(app, main_bot=False)
    try:
        await app.initialize()
    except (InvalidToken, Forbidden) as e:
        logger.warning(f"Klon başlatılamadı {bid}: {e}")
        return "Token geçersiz veya iptal edilmiş."
    except TelegramError as e:
        logger.warning(f"Klon başlatılamadı {bid}: {e}")
        return friendly_error(e)
    if app.bot.id != bid:
        await app.shutdown()
        return "Token başka bir bota ait."
    CLONES[bid] = row
    RUNNING_BOTS[bid] = app.bot
    CLONE_APPS[bid] = app
    token = _ctx_bot.set(app.bot)
    try:
        await setup_bot_profile(app.bot)
    finally:
        _ctx_bot.reset(token)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    with get_db() as conn:
        conn.execute("UPDATE clones SET status = 'active', username = ?, name = ? WHERE bot_id = ?",
                     (app.bot.username, app.bot.first_name, bid))
        conn.commit()
    CLONES[bid] = clone_row(bid)
    logger.info(f"Klon çalışıyor: @{app.bot.username} (sahip {row['owner_id']})")
    return None

async def stop_clone(bid: int, status: str = 'stopped'):
    app = CLONE_APPS.pop(bid, None)
    RUNNING_BOTS.pop(bid, None)
    CLONES.pop(bid, None)
    if app:
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.debug(f"Klon durdurma {bid}: {e}")
    with get_db() as conn:
        conn.execute("UPDATE clones SET status = ? WHERE bot_id = ?", (status, bid))
        conn.commit()

async def start_all_clones():
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM clones WHERE status = 'active'")]
    for row in rows:
        err = await start_clone(row)
        if err:
            await _clone_invalid(row, err)

async def stop_all_clones():
    for bid in list(CLONE_APPS):
        app = CLONE_APPS.pop(bid)
        RUNNING_BOTS.pop(bid, None)
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.debug(f"Klon kapatma {bid}: {e}")

async def _clone_invalid(row: dict, why: str):
    await stop_clone(row['bot_id'], 'invalid')
    text = (f"⚠️ Klon botun @{html.escape(row.get('username') or str(row['bot_id']))} durduruldu: {html.escape(why)}\n"
            "BotFather'dan yeni token alıp 🤖 Klon menüsünden <b>Token değiştir</b> ile tekrar başlatabilirsin.")
    for uid in {row['owner_id'], FOUNDER_ID}:
        try:
            await _main_send(uid, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug(f"Klon uyarısı gönderilemedi {uid}: {e}")

async def clone_health_job(context):
    """Token iptal edilmiş/silinmiş klonları durdurur ve sahibine haber verir."""
    for bid, app in list(CLONE_APPS.items()):
        try:
            await app.bot.get_me()
        except (InvalidToken, Forbidden) as e:
            row = CLONES.get(bid) or clone_row(bid)
            if row:
                await _clone_invalid(row, f"token geçersiz ({e})")
        except TelegramError:
            pass

def _clone_menu(user_id: int):
    mine = user_clones(user_id)
    pend = pending_request(user_id)
    rows = []
    wait = (f"\n\n⏳ <b>@{html.escape(pend.get('bot_username') or '?')}</b> bot sahibinin onayını bekliyor. "
            "Onaylanınca açılacak.") if pend else ""
    if not mine:
        text = ("🤖 <b>Klon bot</b>\n\nKendi bot adınla, ULUS altyapısını kullanan bir koruma botu aç.\n"
                "1) @BotFather'da /newbot ile bot oluştur\n2) Aldığın token'ı aşağıdaki butonla gönder\n"
                "3) Bot sahibi onaylayınca botun çalışmaya başlar" + wait)
        rows.append([ibtn("↩️ İsteği geri çek", "cl|cancel", RED)] if pend else [ibtn("🔑 Token gönder", "cl|tok", GREEN)])
        return text, InlineKeyboardMarkup(rows)
    c = mine[0]
    st = {'active': "🟢 Çalışıyor", 'stopped': "⏸ Durduruldu", 'invalid': "⚠️ Token geçersiz"}.get(c['status'], c['status'])
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM channels WHERE bot_id = ?", (c['bot_id'],)).fetchone()[0]
    text = (f"🤖 <b>Klon botun</b>: @{html.escape(c.get('username') or '?')}\nDurum: {st} · Grup/kanal: {n}\n\n"
            f"🏷 Marka: <b>{html.escape(c.get('brand') or c.get('name') or '-')}</b>\n"
            f"🔗 Destek: {html.escape(c.get('support_link') or '-')}\n"
            f"❓ Yardım başlığı: {html.escape(c.get('help_title') or '-')}\n"
            f"👋 Karşılama: {html.escape((c.get('start_text') or 'varsayılan')[:80])}" + wait)
    b = c['bot_id']
    rows = [[ibtn(CLONE_FIELDS[f][0], f"cl|ed|{b}|{f}", BLUE) for f in ('brand', 'start_text')],
            [ibtn(CLONE_FIELDS[f][0], f"cl|ed|{b}|{f}", BLUE) for f in ('support_link', 'help_title')],
            [ibtn("⏸ Durdur", f"cl|stop|{b}", RED) if c['status'] == 'active' else ibtn("▶️ Başlat", f"cl|go|{b}", GREEN),
             ibtn("↩️ İsteği geri çek", "cl|cancel", RED) if pend else ibtn("🔑 Token değiştir", "cl|tok", BLUE)],
            [ibtn("🗑 Klonu sil", f"cl|del|{b}", RED)]]
    return text, InlineKeyboardMarkup(rows)

async def cmd_klon(update: Update, context):
    """/klon — klon bot menüsü (sadece ana botta, özelden)."""
    if update.effective_chat.type != 'private':
        await update.effective_message.reply_text("Klon işlemleri bota özelden yapılır.")
        return
    text, markup = _clone_menu(update.effective_user.id)
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

async def _decide_clone_request(query, rid: int, approve: bool):
    """Bot sahibi Onayla/Reddet'e bastı. Onay → klon açılır; red → açılmaz, token silinir."""
    with get_db() as conn:
        r = conn.execute("SELECT * FROM clone_requests WHERE id = ?", (rid,)).fetchone()
    r = dict(r) if r else None
    if not r or r['status'] != 'pending':
        await query.answer("Bu istek zaten işlendi.", show_alert=True)
        return
    if not r.get('token'):  # eski sürümden kalan izin isteği
        with get_db() as conn:
            conn.execute("UPDATE clone_requests SET status = 'cancelled', decided_at = ? WHERE id = ?", (time.time(), rid))
            conn.commit()
        await query.answer("Eski sürümden kalan istek; kullanıcı /klon ile token göndermeli.", show_alert=True)
        await _main_send(r['user_id'], "🤖 Klon sistemi yenilendi: /klon yazıp bot token'ını gönder, bot sahibi onaylayınca botun açılır.")
        return
    if approve:
        err, me_username = await _activate_clone(r['user_id'], r['token'], r['bot_id'])
        if err:
            with get_db() as conn:
                conn.execute("UPDATE clone_requests SET status = 'failed', token = NULL, decided_at = ? WHERE id = ?",
                             (time.time(), rid))
                conn.commit()
            await query.answer(f"Açılamadı: {err}"[:190], show_alert=True)
            await _edit_req_msg(r, f"⚠️ Açılamadı: {html.escape(err)}")
            await _main_send(r['user_id'], f"⚠️ Klon botun açılamadı: {html.escape(err)}\n/klon ile yeni token gönderebilirsin.",
                             parse_mode=ParseMode.HTML)
            return
        status, decided = 'approved', "✅ Onaylandı, bot açıldı"
    else:
        status, decided = 'rejected', "❌ Reddedildi"
    with get_db() as conn:
        conn.execute("UPDATE clone_requests SET status = ?, token = NULL, decided_at = ? WHERE id = ?", (status, time.time(), rid))
        conn.commit()
    await query.answer(decided)
    await _edit_req_msg(r, decided)
    if approve:
        await _main_send(r['user_id'],
            f"✅ <b>Klon botun onaylandı ve açıldı!</b> @{html.escape(me_username)}\n\n"
            f"Gruba eklemek için: https://t.me/{me_username}?startgroup=ulus&admin={ADMIN_RIGHTS_GROUP}\n"
            "Ad, karşılama metni ve destek linki için: /klon", parse_mode=ParseMode.HTML)
    else:
        await _main_send(r['user_id'], f"❌ @{r.get('bot_username') or '?'} için klon bot isteğin reddedildi.")

async def clone_callback(update: Update, context):
    """cl|... — klon menüsü ve bot sahibinin onay/yönetim butonları."""
    query = update.callback_query
    parts = query.data.split('|')
    op = parts[1]
    uid = query.from_user.id
    if op == 'req':  # eski sürümün "izin iste" butonu
        await query.answer("Artık izin istemene gerek yok: token'ı gönder, bot sahibi onaylayınca botun açılır.", show_alert=True)
    elif op in ('ok', 'no'):
        if uid != FOUNDER_ID:
            await query.answer("Bu işlemi sadece bot sahibi yapabilir.", show_alert=True)
            return
        await _decide_clone_request(query, int(parts[2]), op == 'ok')
        if query.message and query.message.chat.id == uid and len(parts) > 3:  # /klonlar listesinden basıldıysa
            text, markup = _clones_admin_view()
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            except BadRequest:
                pass
        return
    elif op == 'cancel':
        pend = pending_request(uid)
        if not pend:
            await query.answer("Bekleyen isteğin yok.", show_alert=True)
        else:
            with get_db() as conn:
                conn.execute("UPDATE clone_requests SET status = 'cancelled', token = NULL, decided_at = ? WHERE id = ?",
                             (time.time(), pend['id']))
                conn.commit()
            await _edit_req_msg(pend, "↩️ Kullanıcı isteği geri çekti")
            await query.answer("↩️ İstek geri çekildi")
    elif op == 'tok':
        context.user_data['await_clone'] = {'field': 'token', 'expires': time.time() + 600}
        await query.answer()
        await query.message.reply_text("🔑 @BotFather'dan aldığın bot token'ını yaz (ör. <code>123456789:ABC...</code>).\n"
                                       "Vazgeçmek için: iptal", parse_mode=ParseMode.HTML,
                                       reply_markup=ForceReply(input_field_placeholder="123456789:ABC..."))
        return
    elif op in ('ed', 'stop', 'go', 'del', 'delok', 'adm'):
        bid = int(parts[2])
        row = clone_row(bid)
        if not row or (row['owner_id'] != uid and uid != FOUNDER_ID):
            await query.answer("Bu klon senin değil.", show_alert=True)
            return
        if op == 'ed':
            field = parts[3]
            if field not in CLONE_FIELDS:
                await query.answer("Geçersiz.", show_alert=True)
                return
            context.user_data['await_clone'] = {'field': field, 'bot_id': bid, 'expires': time.time() + 600}
            await query.answer()
            await query.message.reply_text(f"{CLONE_FIELDS[field][1]}\nVazgeçmek için: iptal",
                                           reply_markup=ForceReply(input_field_placeholder=CLONE_FIELDS[field][0]))
            return
        if op == 'stop':
            await stop_clone(bid)
            await query.answer("⏸ Durduruldu")
        elif op == 'go':
            if row['status'] == 'deleted' or not row['token']:
                await query.answer("Bu klon silinmiş.", show_alert=True)
                return
            err = await start_clone(row)
            if err:
                await query.answer(err[:190], show_alert=True)
                return
            await query.answer("▶️ Başlatıldı")
        elif op == 'del':
            await query.answer()
            await query.edit_message_text(f"🗑 @{html.escape(row.get('username') or '')} klonu silinsin mi? Bot durur, "
                                          "gruplardaki ayarlar kalır.", parse_mode=ParseMode.HTML,
                                          reply_markup=InlineKeyboardMarkup([[ibtn("🗑 Evet, sil", f"cl|delok|{bid}", RED),
                                                                              ibtn("↩️ Vazgeç", f"cl|adm|{bid}")]]))
            return
        elif op == 'delok':
            await stop_clone(bid, 'deleted')
            with get_db() as conn:
                conn.execute("UPDATE clones SET token = '' WHERE bot_id = ?", (bid,))
                conn.commit()
            await query.answer("🗑 Silindi")
            if uid != row['owner_id']:
                await _main_send(row['owner_id'], f"🗑 Klon botun @{row.get('username')} bot sahibi tarafından silindi.")
        else:
            await query.answer()
        if uid == FOUNDER_ID and uid != row['owner_id']:
            text, markup = _clones_admin_view()
        else:
            text, markup = _clone_menu(uid)
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        return
    else:
        await query.answer()
        return
    text, markup = _clone_menu(uid)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest:
        pass

async def clone_input_handler(update: Update, context):
    """Klon için beklenen metin girişi (token / özelleştirme alanı). Sadece özelden ve bekleyen giriş varsa."""
    pend = context.user_data.get('await_clone')
    msg = update.effective_message
    if not pend or update.effective_chat.type != 'private' or not msg or not msg.text:
        return
    if time.time() > pend['expires']:
        context.user_data.pop('await_clone', None)
        return
    text = msg.text.strip()
    if text.lower() in ('iptal', 'vazgeç', 'vazgec', '/iptal'):
        context.user_data.pop('await_clone', None)
        await msg.reply_text("Vazgeçildi.")
        raise ApplicationHandlerStop
    uid = update.effective_user.id
    if pend['field'] == 'token':
        context.user_data.pop('await_clone', None)
        try:
            await msg.delete()  # token sohbette açık kalmasın
        except TelegramError:
            pass
        reply = await _submit_clone_token(update.effective_user, text)
        await context.bot.send_message(uid, reply, parse_mode=ParseMode.HTML)
        t, mk = _clone_menu(uid)
        await context.bot.send_message(uid, t, reply_markup=mk, parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop
    field, bid = pend['field'], pend['bot_id']
    row = clone_row(bid)
    if not row or (row['owner_id'] != uid and uid != FOUNDER_ID):
        context.user_data.pop('await_clone', None)
        raise ApplicationHandlerStop
    limit = CLONE_FIELDS[field][2]
    value = '' if text == '-' else text[:limit]
    if field == 'support_link' and value and not re.match(r'^https://t\.me/[\w/+\-]+$', value):
        await msg.reply_text("Link https://t.me/ ile başlamalı. Tekrar yaz ya da iptal.")
        raise ApplicationHandlerStop
    with get_db() as conn:
        conn.execute(f"UPDATE clones SET {field} = ? WHERE bot_id = ?", (value or None, bid))
        conn.commit()
    if bid in CLONES:
        CLONES[bid] = clone_row(bid)
    context.user_data.pop('await_clone', None)
    await msg.reply_text(f"✅ {CLONE_FIELDS[field][0]} güncellendi.")
    t, mk = _clone_menu(row['owner_id'] if uid == FOUNDER_ID else uid)
    await msg.reply_text(t, reply_markup=mk, parse_mode=ParseMode.HTML)
    raise ApplicationHandlerStop

async def _submit_clone_token(user, token: str) -> str:
    """Token'ı doğrular ve bot sahibine onaya gönderir (bot sahibinin kendi klonu doğrudan açılır)."""
    uid = user.id
    if not CLONE_TOKEN_RE.match(token):
        return "❌ Bu bir bot token'ına benzemiyor. @BotFather'daki tam token'ı gönder."
    if token == TOKEN:
        return "❌ Bu ana botun token'ı."
    try:
        me = await Bot(token).get_me()
    except (InvalidToken, Forbidden):
        return "❌ Token geçersiz. @BotFather'dan doğru token'ı kopyala."
    except TelegramError as e:
        return friendly_error(e)
    if me.id == BOT_ID:
        return "❌ Bu ana botun token'ı."
    existing = clone_row(me.id)
    if existing and existing['owner_id'] != uid and existing['status'] != 'deleted':
        return "❌ Bu bot zaten başka birinin klonu."
    with get_db() as conn:
        other = conn.execute("SELECT 1 FROM clone_requests WHERE bot_id = ? AND status = 'pending' AND user_id != ?",
                             (me.id, uid)).fetchone()
    if other:
        return "❌ Bu bot için başka birinin bekleyen isteği var."
    if uid == FOUNDER_ID:
        err, uname = await _activate_clone(uid, token, me.id)
        if err:
            return f"❌ Bot başlatılamadı: {html.escape(err)}"
        return (f"✅ <b>Klon botun hazır!</b> @{html.escape(uname)}\n\n"
                f"Gruba eklemek için: https://t.me/{uname}?startgroup=ulus&admin={ADMIN_RIGHTS_GROUP}")
    old = pending_request(uid)
    async with _db_lock:
        with get_db() as conn:
            if old:  # aynı kişinin önceki bekleyen isteği: yenisi geçerli
                conn.execute("UPDATE clone_requests SET status = 'cancelled', token = NULL, decided_at = ? WHERE id = ?",
                             (time.time(), old['id']))
            rid = conn.execute("""INSERT INTO clone_requests (user_id, username, first_name, status, created_at,
                                  bot_id, token, bot_username, bot_name) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                               (uid, user.username, user.first_name, time.time(), me.id, token, me.username,
                                me.first_name)).lastrowid
            conn.commit()
            r = dict(conn.execute("SELECT * FROM clone_requests WHERE id = ?", (rid,)).fetchone())
    if old:
        await _edit_req_msg(old, "↩️ Yerine yeni istek gönderildi")
    sent = await _main_send(FOUNDER_ID, _clone_req_text(r), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(
        [[ibtn("✅ Onayla", f"cl|ok|{rid}", GREEN), ibtn("❌ Reddet", f"cl|no|{rid}", RED)]]))
    if sent is not None and getattr(sent, 'message_id', None):
        with get_db() as conn:
            conn.execute("UPDATE clone_requests SET admin_msg_id = ? WHERE id = ?", (sent.message_id, rid))
            conn.commit()
    return (f"📨 <b>@{html.escape(me.username)}</b> için isteğin bot sahibine gönderildi.\n"
            "Onaylanınca botun açılacak ve sana haber vereceğim.")

async def _activate_clone(uid: int, token: str, bot_id: int):
    """Onaylanan token ile klonu kaydeder ve başlatır. Dönüş: (hata | None, kullanıcı adı)."""
    try:
        me = await Bot(token).get_me()
    except (InvalidToken, Forbidden):
        return "Token geçersiz veya iptal edilmiş.", ''
    except TelegramError as e:
        return friendly_error(e), ''
    if me.id != bot_id:
        return "Token başka bir bota ait.", ''
    existing = clone_row(me.id)
    if existing and existing['owner_id'] != uid and existing['status'] != 'deleted':
        return "Bu bot zaten başka birinin klonu.", ''
    mine = [c for c in user_clones(uid) if c['bot_id'] != me.id]
    old = mine[0] if mine else None
    if old and uid != FOUNDER_ID and len(mine) >= CLONE_LIMIT_PER_USER:
        await stop_clone(old['bot_id'], 'deleted')  # token değiştir = eski klonun yerine yenisi
    async with _db_lock:
        with get_db() as conn:
            conn.execute("""INSERT INTO clones (bot_id, owner_id, token, username, name, status, created_at)
                            VALUES (?, ?, ?, ?, ?, 'active', ?)
                            ON CONFLICT(bot_id) DO UPDATE SET token = excluded.token, owner_id = excluded.owner_id,
                            username = excluded.username, name = excluded.name, status = 'active'""",
                         (me.id, uid, token, me.username, me.first_name, time.time()))
            if old:
                for col in ('brand', 'start_text', 'support_link', 'help_title'):
                    conn.execute(f"UPDATE clones SET {col} = COALESCE({col}, ?) WHERE bot_id = ?", (old.get(col), me.id))
                if uid != FOUNDER_ID:
                    conn.execute("UPDATE clones SET token = '' WHERE bot_id = ?", (old['bot_id'],))
            conn.commit()
    if me.id in CLONE_APPS:  # aynı bot yeni token'la: yeniden başlat
        await stop_clone(me.id, 'active')
    err = await start_clone(clone_row(me.id))
    return err, me.username

def _clones_admin_view():
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM clones WHERE status != 'deleted' ORDER BY created_at")]
        pend = [dict(r) for r in conn.execute("SELECT * FROM clone_requests WHERE status = 'pending' AND token IS NOT NULL "
                                              "ORDER BY created_at")]
    icon = {'active': "🟢", 'stopped': "⏸", 'invalid': "⚠️"}
    lines = [f"{icon.get(r['status'], '•')} @{html.escape(r.get('username') or '?')} — sahip <code>{r['owner_id']}</code>"
             for r in rows]
    plines = [f"⏳ @{html.escape(p.get('bot_username') or '?')} — isteyen <code>{p['user_id']}</code>" for p in pend]
    text = (f"🤖 <b>Klon botlar</b> ({len(rows)}) · Onay bekleyen: {len(pend)}\n\n" + ("\n".join(lines) or "Henüz klon yok.")
            + ("\n\n<b>Onay bekleyenler</b>\n" + "\n".join(plines) if plines else ""))
    kb = []
    for p in pend[:10]:
        kb.append([ibtn(f"⏳ @{(p.get('bot_username') or '?')[:18]}", f"cl|ok|{p['id']}|l", GREEN),
                   ibtn("❌", f"cl|no|{p['id']}|l", RED)])
    for r in rows[:30]:
        b = r['bot_id']
        kb.append([ibtn(f"@{(r.get('username') or '?')[:20]}", f"cl|adm|{b}"),
                   ibtn("⏸" if r['status'] == 'active' else "▶️", f"cl|{'stop' if r['status'] == 'active' else 'go'}|{b}",
                        RED if r['status'] == 'active' else GREEN),
                   ibtn("🗑", f"cl|del|{b}", RED)])
    return text, InlineKeyboardMarkup(kb) if kb else None

async def cmd_klonlar(update: Update, context):
    """/klonlar — bot sahibi: tüm klonları listele, durdur/başlat/sil."""
    if update.effective_user.id != FOUNDER_ID:
        return
    text, markup = _clones_admin_view()
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

# ── Veritabanı yedekleme ──
def _make_backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"bot_data_{datetime.now(TZ_TR):%Y%m%d_%H%M}.db")
    src_conn = sqlite3.connect(DB_FILE)
    dst_conn = sqlite3.connect(path)
    try:
        src_conn.backup(dst_conn)  # çalışırken güvenli kopya
    finally:
        dst_conn.close()
        src_conn.close()
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('bot_data_') and f.endswith('.db'))
    for old in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    return path

async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        path = await asyncio.to_thread(_make_backup)
        logger.info(f"Yedek alındı: {path}")
        if BACKUP_CHAT_ID and os.path.getsize(path) < 45 * 1024 * 1024:
            with open(path, 'rb') as f:
                await context.bot.send_document(BACKUP_CHAT_ID, f, filename=os.path.basename(path),
                                                caption="🗄 Günlük veritabanı yedeği")
    except Exception as e:
        logger.error(f"Yedekleme hatası: {e}")

async def cmd_yedek(update: Update, context):
    if update.effective_user.id != FOUNDER_ID:
        return
    await update.effective_message.reply_text("🗄 Yedek alınıyor...")
    await backup_job(context)

def _cleanup_in_memory_caches(now: float) -> None:
    # Süresiz büyüyebilen sözlükler için yaş temelli temizlik
    for dct, ttl in ((_settings_cache, SETTINGS_CACHE_TTL * 5), (_tg_admin_cache, TG_ADMIN_CACHE_TTL * 5), (_title_cache, 3600)):
        for k, v in list(dct.items()):
            ts = v[0] if isinstance(v, tuple) and v else 0
            if ts and now - ts > ttl:
                dct.pop(k, None)

    for dct, ttl in ((_report_last, 6 * 3600), (_filter_last, 600), (_unregistered_checked, 2 * 3600), (_demotion_alerted, 3600)):
        for k, ts in list(dct.items()):
            if isinstance(ts, (int, float)) and now - ts > ttl:
                dct.pop(k, None)

    for dct, ttl in ((_media_attack, 600), (_channel_media_hits, 1200), (_flood_data, 600)):
        for k, vals in list(dct.items()):
            if not isinstance(vals, list):
                continue
            fresh = [x for x in vals if isinstance(x, (int, float)) and now - x < ttl]
            if fresh:
                dct[k] = fresh
            else:
                dct.pop(k, None)

    for k, vals in list(_demotions.items()):
        if not isinstance(vals, list):
            _demotions.pop(k, None)
            continue
        fresh = [(t, u) for t, u in vals if isinstance(t, (int, float)) and now - t < 1200]
        if fresh:
            _demotions[k] = fresh
        else:
            _demotions.pop(k, None)

# ── Periyodik temizlik ──
async def db_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    msg_stats_cutoff = now - (MESSAGE_STATS_RETENTION_DAYS * 86400)
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM flood_history WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM media_flood_history WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM forward_history WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM raid_joins WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM spam_incidents WHERE incident_at < ?", (now - 30 * 86400,))
            conn.execute("DELETE FROM newcomers WHERE joined_at < ?", (now - 2 * 86400,))
            conn.execute("DELETE FROM appeals WHERE created_at < ? AND status != 'pending'", (now - 90 * 86400,))
            conn.execute("DELETE FROM msg_cache WHERE sent_at < ?", (now - 2 * 86400,))
            conn.execute("DELETE FROM reports WHERE created_at < ?", (now - 30 * 86400,))
            conn.execute("DELETE FROM message_stats WHERE sent_at < ?", (msg_stats_cutoff,))
            conn.commit()
    _cleanup_in_memory_caches(now)

# ── Genel hata yakalayıcı ──
_last_error_notify = 0.0

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    global _last_error_notify
    err = context.error
    if isinstance(err, (Forbidden,)):
        logger.info(f"Yetki/erişim hatası (yok sayıldı): {err}")
        return
    if isinstance(err, BadRequest) and any(s in str(err).lower() for s in (
            'message to delete not found', 'message is not modified', 'query is too old', "message can't be deleted")):
        return
    logger.error("İşlenmeyen hata", exc_info=err)
    now = time.time()
    if FOUNDER_ID and now - _last_error_notify > 60:  # dakikada en fazla 1 bildirim
        _last_error_notify = now
        tb = ''.join(traceback.format_exception(type(err), err, err.__traceback__))[-3000:]
        where = ''
        if isinstance(update, Update) and update.effective_chat:
            where = f"Sohbet: {update.effective_chat.id}\n"
        try:
            await (MAIN_BOT or context.bot).send_message(FOUNDER_ID, f"⚠️ Bot hatası\n{where}<pre>{html.escape(tb)}</pre>",
                                           parse_mode=ParseMode.HTML)
        except Exception:
            pass
    await _tell_user_about_error(update)

bad_callbacks: list = []  # testler için: geçersiz veri yüzünden durdurulan buton tıklamaları

def guard_callback(fn):
    """Buton verisi eski/bozuksa (ayrıştırma hatası) çökme yerine 'buton geçersiz' uyarısı verir."""
    @functools.wraps(fn)
    async def wrapper(update, context):
        try:
            return await fn(update, context)
        except (ValueError, IndexError, KeyError, OverflowError) as e:
            data = getattr(update.callback_query, 'data', '')
            bad_callbacks.append((fn.__name__, data, repr(e)))
            logger.warning(f"Geçersiz buton verisi {fn.__name__} {data!r}: {e!r}")
            try:
                await update.callback_query.answer("⚠️ Bu buton eskimiş veya geçersiz. Menüyü yeniden aç.", show_alert=True)
            except TelegramError:
                pass
    return wrapper

async def _tell_user_about_error(update) -> None:
    """Beklenmeyen hatada kullanıcıya kısa ve kibar bir bilgi verir (ham hata metni gösterilmez)."""
    if not isinstance(update, Update):
        return
    text = "⚠️ Bir sorun oluştu, işlem tamamlanamadı. Lütfen tekrar dene."
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message and (update.effective_chat.type == 'private' or (update.message.text or '').startswith('/')):
            await update.message.reply_text(text)
    except TelegramError:
        pass

async def chat_member_cache_handler(update: Update, context):
    """Admin atama/alma olduğunda admin önbelleğini temizler. Telegram'dan elle ya da başka bir botla yönetici yapılan
    kişiye rütbe verilir (yetki alma rütbeyi otomatik silmez: toplu düşürme saldırısında rütbeler kaybolmasın; /reload siler)."""
    cm = update.chat_member
    if not cm:
        return
    chat_id = str(cm.chat.id)
    invalidate_admin_cache(chat_id)
    new = cm.new_chat_member
    if (new.status == 'administrator' and cm.old_chat_member.status != 'administrator' and not new.user.is_bot
            and get_channel_settings(chat_id) and not user_level(chat_id, new.user.id)):
        role = await auto_assign_role(chat_id, new.user.id, new)
        logger.info(f"Telegram'dan yönetici yapıldı, rütbe verildi {chat_id}/{new.user.id}: {role}")

RELOAD_COOLDOWN = 30
_reload_last: dict = {}

async def reload_admins(chat_id: str):
    """Telegram'daki güncel yönetici listesini bot kaydına işler: kurucu eşitlenir, rütbesi olmayan yöneticiye rütbe
    verilir, artık yönetici olmayanın rütbesi alınır (kurucu, botu ekleyen ve acil kilitte yetkisi alınanlar hariç).
    Dönüş: (eklenenler, kaldırılanlar, yönetici sayısı) veya liste alınamazsa None."""
    chat_id = str(chat_id)
    invalidate_admin_cache(chat_id)
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramError as e:
        logger.debug(f"/reload admin listesi alınamadı {chat_id}: {e}")
        return None
    await sync_creator(chat_id, admins)
    people = [a for a in admins if not a.user.is_bot]
    added = []
    for a in people:
        if a.status == 'administrator' and not user_level(chat_id, a.user.id):
            await auto_assign_role(chat_id, a.user.id, a)
            added.append(a.user.id)
    keep = {a.user.id for a in people}
    with get_db() as conn:
        ch = conn.execute("SELECT owner_id, added_by FROM channels WHERE chat_id = ?", (chat_id,)).fetchone()
    if ch:
        keep |= {ch['owner_id'], ch['added_by']}
    try:
        keep |= set(json.loads(get_channel_cfg(chat_id).get('lockdown_saved_admins') or '[]'))
    except (ValueError, TypeError):
        pass
    async with _db_lock:
        with get_db() as conn:
            stale = [r['user_id'] for r in conn.execute(
                "SELECT user_id FROM roles WHERE chat_id = ? AND role != 'kurucu'", (chat_id,)) if r['user_id'] not in keep]
            for uid in stale:
                conn.execute("DELETE FROM roles WHERE chat_id = ? AND user_id = ?", (chat_id, uid))
            conn.commit()
    _invalidate_settings(chat_id)
    invalidate_admin_cache(chat_id)
    _tg_admin_cache[chat_id] = (time.time(), {a.user.id for a in admins})
    return added, stale, len(people)

async def cmd_reload(update: Update, context):
    """/reload — admin listesini Telegram'dan yeniden yükler (elle veya başka botlarla verilen/alınan yetkiler)."""
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile grup seç!")
        return
    uid = update.effective_user.id if update.effective_user else 0
    is_anon = uid == GROUP_ANON_BOT_ID or (msg.sender_chat is not None and str(msg.sender_chat.id) == str(chat_id))
    if not (is_anon or user_level(chat_id, uid) or await _is_real_chat_admin(chat_id, uid)):
        await msg.reply_text("⛔ Bu komutu sadece yöneticiler kullanabilir.")
        return
    now = time.time()
    if now - _reload_last.get(chat_id, 0) < RELOAD_COOLDOWN:
        await msg.reply_text(f"⏳ Admin listesi az önce yenilendi. {RELOAD_COOLDOWN} sn sonra tekrar dene.")
        return
    _reload_last[chat_id] = now
    res = await reload_admins(chat_id)
    if res is None:
        await msg.reply_text("⚠️ Admin listesi alınamadı. Bot bu grupta yönetici mi?")
        return
    added, removed, total = res
    lines = [f"🔄 <b>Admin listesi yenilendi</b> — {total} yönetici"]
    if added:
        lines.append("➕ Rütbe verildi: " + ", ".join(f"<code>{u}</code>" for u in added[:20]))
    if removed:
        lines.append("➖ Rütbesi alındı (artık yönetici değil): " + ", ".join(f"<code>{u}</code>" for u in removed[:20]))
    if not added and not removed:
        lines.append("Kayıt zaten güncel.")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    if added or removed:
        await send_log(chat_id, f"🔄 /reload: +{len(added)} / −{len(removed)} | {mention(update.effective_user)}", ParseMode.HTML)


async def _startup_report(b):
    """Açılışta veritabanı yolunu ve kayıtlı sohbet sayısını loglar ve bot sahibine bildirir."""
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    size = os.path.getsize(DB_FILE) // 1024 if os.path.exists(DB_FILE) else 0
    text = (f"🟢 ULUS başladı\nVeritabanı: <code>{html.escape(DB_FILE)}</code> ({size} KB)\n"
            f"Kayıtlı grup/kanal: <b>{n}</b>")
    if RESTORED_FROM:
        text += f"\n⚠️ Veritabanı boş açıldı, son yedekten geri yüklendi: <code>{html.escape(RESTORED_FROM)}</code>"
    elif n == 0:
        text += ("\n⚠️ Kayıtlı sohbet yok. Bot yeniden başlatılınca bu sayı düşüyorsa veritabanı dosyası "
                 "silinmiş ya da farklı bir klasörden çalıştırılıyor olabilir.")
    logger.info(text.replace("<code>", "").replace("</code>", "").replace("<b>", "").replace("</b>", ""))
    if FOUNDER_ID:
        try:
            await b.send_message(FOUNDER_ID, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug(f"Açılış bildirimi gönderilemedi: {e}")

_unregistered_checked: dict = {}

async def ensure_registered(chat_id: str, chat_type: str | None = None) -> bool:
    """Bot yöneticisi olduğu ama kaydı olmayan sohbeti (ör. veritabanı kaybından sonra) otomatik kaydeder.
    Sahibi: Telegram'daki grup/kanal sahibi. Başarısız denemeler 10 dk boyunca tekrarlanmaz."""
    chat_id = str(chat_id)
    if get_channel_settings(chat_id):
        return True
    if time.time() - _unregistered_checked.get(chat_id, 0) < 600:
        return False
    _unregistered_checked[chat_id] = time.time()
    try:
        me = await bot.get_chat_member(chat_id, bot_id_for(chat_id))
        if me.status != 'administrator':
            return False
        admins = await bot.get_chat_administrators(chat_id)
        if not chat_type:
            chat_type = (await bot.get_chat(chat_id)).type
    except TelegramError as e:
        logger.debug(f"Otomatik kayıt kontrolü başarısız {chat_id}: {e}")
        return False
    creator = next((a.user.id for a in admins if getattr(a, 'status', None) == 'creator'), 0)
    await _register_chat(chat_id, creator, chat_type, added_by=None)
    _unregistered_checked.pop(chat_id, None)
    logger.info(f"Kaydı olmayan sohbet otomatik kaydedildi: {chat_id} (sahip {creator})")
    return True

async def repair_rank(chat_id: str, user_id: int) -> bool:
    """Telegram'da kurucu/yönetici olup bot kaydında rütbesi olmayan kişinin rütbesini onarır."""
    if user_level(chat_id, user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    if member.status == 'creator':
        await sync_creator(chat_id, [member])
        return True
    if member.status == 'administrator':
        await auto_assign_role(chat_id, user_id, member)
        return True
    return False

async def auto_register_handler(update: Update, context):
    """Grup/kanal güncellemesi kaydı olmayan bir sohbetten geldiyse (bot orada yöneticiyse) kaydeder."""
    chat = update.effective_chat
    if chat and chat.type in ('group', 'supergroup', 'channel') and not get_channel_settings(str(chat.id)):
        await ensure_registered(str(chat.id), chat.type)

async def checkpoint_job(context):
    """WAL dosyasındaki değişiklikleri ana veritabanı dosyasına yazar (yalnız .db kopyalansa da veri kaybolmaz)."""
    try:
        with get_db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as e:
        logger.debug(f"WAL checkpoint: {e}")

async def post_shutdown(application):
    await stop_all_clones()
    await checkpoint_job(None)
    global userbot
    if userbot:
        try:
            await userbot.disconnect()
        except Exception as e:
            logger.debug(f"Telethon kapanış hatası: {e}")
        userbot = None
    logger.info("ULUS botu düzgün şekilde kapatıldı.")

async def post_init(application):
    global BOT_ID, userbot
    BOT_ID = application.bot.id
    RUNNING_BOTS[BOT_ID] = application.bot
    await setup_bot_profile(application.bot)
    await _startup_report(application.bot)
    try:
        await start_all_clones()
    except Exception as e:
        logger.error(f"Klonlar başlatılamadı: {e}")

    if not (USERBOT_API_ID and USERBOT_API_HASH):
        logger.info("API_ID / API_HASH boş — Telethon kapalı (kullanıcı adı çözme ve toplu istek onayı Bot API ile yapılır).")
        return
    try:
        userbot = TelegramClient(USERBOT_SESSION, USERBOT_API_ID, USERBOT_API_HASH)
        await userbot.start(bot_token=TOKEN)
        me = await userbot.get_me()
        logger.info(f"Telethon aktif: @{me.username} ({me.id})")
    except Exception as e:
        logger.error(f"Telethon başlatılamadı: {e}")
        userbot = None

def register_handlers(app, main_bot: bool = True):
    """Ana bot ve her klon aynı handler'ları kullanır. Klon yönetimi ve periyodik işler sadece ana bottadır."""
    app.add_handler(TypeHandler(Update, bot_context_handler), group=-1000)
    # Her güncellemeden önce: engelli sohbet/kullanıcı ve global ban kontrolü
    app.add_handler(TypeHandler(Update, blocklist_guard), group=-100)
    app.add_handler(TypeHandler(Update, auto_register_handler), group=-99)
    app.add_error_handler(error_handler)
    # Panelin ForceReply ile istediği metin (sadece bekleyen giriş varsa yakalar)
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL
                                    | filters.Sticker.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE)
                                   & ~filters.COMMAND, settings_input_handler), group=-50)

    app.add_handler(ChatMemberHandler(chat_member_cache_handler, ChatMemberHandler.CHAT_MEMBER), group=-1)
    app.add_handler(ChatMemberHandler(network_ban_handler, ChatMemberHandler.CHAT_MEMBER), group=-2)
    app.add_handler(ChatMemberHandler(demotion_watch_handler, ChatMemberHandler.CHAT_MEMBER), group=-3)
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_chat_member_protection, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler('kanalsettings', cmd_kanal_settings))
    app.add_handler(CallbackQueryHandler(kanal_cfg_callback, pattern=r'^kcfg'))
    app.add_handler(CallbackQueryHandler(lockdown_restore_callback, pattern=r'^lockdown_restore'))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & ~filters.COMMAND, handle_channel_post_protection), group=1)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, kanal_admin_spam_check), group=2)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_bot_added), group=0)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler), group=1)
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, left_member_handler), group=1)
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.ChatType.GROUPS, edit_guard_handler),
                    group=-13)
    app.add_handler(MessageHandler(
        (filters.ChatType.GROUPS | filters.ChatType.CHANNEL)
        & (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Sticker.ALL | filters.Document.ALL
           | filters.VIDEO_NOTE), media_shield_handler), group=-12)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, newbie_guard_handler), group=-11)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.GROUPS, message_cache_handler), group=97)
    app.add_handler(MessageHandler(filters.ALL, anti_forward_handler), group=-10)
    app.add_handler(MessageHandler(filters.ALL, anti_media_flood_handler), group=-9)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anti_spam_flood_handler), group=-8)
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, anti_link_handler), group=-7)
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, check_message), group=-6)
    # Kanal zorunluluğu: korumalardan sonra, not/filtre/sayaçtan önce (engellenen mesaj sonraki adımlara gitmez)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
                                   fsub_guard), group=-4)
    app.add_handler(MessageHandler(filters.Regex(r'^#[\w\-]+') & filters.ChatType.GROUPS, hashtag_note_handler), group=5)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
                                   afk_handler), group=7)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND
                                   & filters.ChatType.GROUPS, filter_reply_handler), group=6)

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('id', id_command))
    app.add_handler(CommandHandler('kanal', kanal, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('setlog', setlog, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('staff', staff))
    app.add_handler(CommandHandler(['reload', 'admincache', 'yenile'], cmd_reload))
    app.add_handler(CommandHandler('yetkiler', yetkiler, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('menu', cmd_menu, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Text(sorted(DM_MENU_TEXTS)), dm_menu_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, chat_shared_handler))

    app.add_handler(CommandHandler('addadmin', add_admin))
    app.add_handler(CommandHandler('remove', remove_admin))
    app.add_handler(CommandHandler('ban', ban))
    app.add_handler(CommandHandler('unban', unban))
    app.add_handler(CommandHandler('kick', kick))
    app.add_handler(CommandHandler('mute', mute))
    app.add_handler(CommandHandler('unmute', unmute))
    app.add_handler(CommandHandler('warn', warn))
    app.add_handler(CommandHandler('unwarn', unwarn))
    app.add_handler(CommandHandler('warns', warns))
    app.add_handler(CommandHandler('klasorcu', klasorcu))
    app.add_handler(CommandHandler('pin', pin))
    app.add_handler(CommandHandler('unpin', unpin))
    app.add_handler(CommandHandler('slowmode', slowmode))
    app.add_handler(CommandHandler('temizle', temizle))
    app.add_handler(CommandHandler('banlist', banlist))
    app.add_handler(CommandHandler('mutelist', mutelist))

    app.add_handler(CommandHandler('settings', cmd_settings))
    app.add_handler(CommandHandler('spamkoruma', spam_koruma))
    app.add_handler(CommandHandler('antispam', cmd_antispam))
    app.add_handler(CommandHandler('antilink', cmd_antilink))
    app.add_handler(CommandHandler('antiforward', cmd_antiforward))
    app.add_handler(CommandHandler('antimedia', cmd_antimedia))
    app.add_handler(CommandHandler('antiraid', cmd_antiraid))
    app.add_handler(CommandHandler('antiraid_ac', cmd_antiraid_ac))
    app.add_handler(CommandHandler('captcha', cmd_captcha))
    app.add_handler(CommandHandler('wordban', word_ban))
    app.add_handler(CommandHandler('wordbanon', word_ban_on))
    app.add_handler(CommandHandler('wordbanoff', word_ban_off))
    app.add_handler(CommandHandler('setautoaccept', set_auto_accept))
    app.add_handler(CommandHandler('setautoreject', set_auto_reject))
    app.add_handler(CommandHandler('setautorejectbot', set_auto_reject_bot))
    app.add_handler(CommandHandler('setwelcome', set_welcome))
    app.add_handler(CommandHandler(['setgoodbye', 'setveda'], cmd_setgoodbye))
    app.add_handler(CommandHandler('welcome', cmd_welcome))
    app.add_handler(CommandHandler(['goodbye', 'veda'], cmd_goodbye))
    app.add_handler(CommandHandler('resetwelcome', cmd_resetwelcome))
    app.add_handler(CommandHandler('zamanla', cmd_zamanla))
    app.add_handler(CommandHandler('zamanlar', cmd_zamanlar))
    app.add_handler(CallbackQueryHandler(sched_callback, pattern=r'^zm\|'))
    app.add_handler(CallbackQueryHandler(rich_action_callback, pattern=r'^ra\|'))
    app.add_handler(CommandHandler('etiket', cmd_etiket))
    app.add_handler(CommandHandler('etiketdur', cmd_etiketdur))
    app.add_handler(CommandHandler('etiketme', cmd_etiketme))
    app.add_handler(CallbackQueryHandler(tag_stop_callback, pattern=r'^et\|'))
    app.add_handler(CommandHandler(['kanalzorunlu', 'fsub', 'forcesub'], cmd_kanalzorunlu))
    app.add_handler(CallbackQueryHandler(fsub_callback, pattern=r'^fs\|'))
    app.add_handler(CommandHandler('afk', cmd_afk))
    app.add_handler(CommandHandler('sicil', cmd_sicil))
    app.add_handler(CommandHandler(['oylama', 'votemute'], cmd_oylama))
    app.add_handler(CallbackQueryHandler(vote_callback, pattern=r'^vm\|'))

    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CommandHandler('invitestats', invite_stats))
    app.add_handler(CommandHandler('yazitura', cmd_yazitura))
    app.add_handler(CommandHandler('zar', cmd_zar))
    app.add_handler(CommandHandler('rules', cmd_rules))
    app.add_handler(CommandHandler('setrules', cmd_setrules))
    app.add_handler(CommandHandler('setwarnlimit', cmd_setwarnlimit))
    app.add_handler(CommandHandler('whitelist', cmd_whitelist))
    app.add_handler(CommandHandler('grupbilgi', cmd_grupbilgi))
    app.add_handler(CommandHandler('leaderboard', leaderboard))
    app.add_handler(CommandHandler('cekilis', cekilis))
    app.add_handler(CommandHandler('cekilis_bitir', giveaway_end))
    app.add_handler(CommandHandler('duyuru', duyuru))
    app.add_handler(CommandHandler('profil', profil))
    app.add_handler(CommandHandler('wordlist', wordlist))
    app.add_handler(CommandHandler('nightmod', cmd_nightmod))
    app.add_handler(CommandHandler('istekonayla', istekonayla))
    app.add_handler(CommandHandler('gunluk', cmd_gunluk))
    app.add_handler(CommandHandler('haftalik', cmd_haftalik))
    app.add_handler(CommandHandler('aylik', cmd_aylik))
    app.add_handler(CommandHandler('toplam', cmd_toplam))
    app.add_handler(CommandHandler('top', cmd_top))
    app.add_handler(CommandHandler('info', cmd_info))
    app.add_handler(CommandHandler('kilit', cmd_kilit))
    app.add_handler(CommandHandler('yetkim', cmd_yetkim))
    app.add_handler(CallbackQueryHandler(top_callback, pattern=r'^top'))

    # Mesaj sayacı: her yeni mesaj anında veritabanına yazılır (yeniden başlatmada kaybolmaz).
    # Düzenlemeler ve katıldı/ayrıldı gibi servis mesajları sayılmaz.
    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & ~filters.StatusUpdate.ALL & filters.ChatType.GROUPS,
        track_message
    ), group=99)

    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex(r'^/temizle'),
        kanal_temizle
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex(r'^/istekonayla'),
        istekonayla
    ))
    app.add_handler(CommandHandler('admin', cmd_admin_with_title))
    app.add_handler(CommandHandler('basadmin', cmd_basadmin))
    app.add_handler(CommandHandler('yardimcikurucu', cmd_yardimci_kurucu))
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex(r'^/admin'),
        cmd_admin_with_title
    ))
    app.add_handler(CommandHandler('uyeetiketi', cmd_uvye_etiketi))
    app.add_handler(CommandHandler('panel', cmd_panel))
    app.add_handler(CommandHandler('engelle', cmd_engelle))
    app.add_handler(CommandHandler('engelkaldir', cmd_engel_kaldir))
    app.add_handler(CommandHandler('gban', cmd_gban))
    app.add_handler(CommandHandler('ungban', cmd_ungban))
    app.add_handler(CommandHandler('gbanlist', cmd_gbanlist))
    app.add_handler(CommandHandler('yedek', cmd_yedek))
    app.add_handler(CommandHandler('setwarnaction', cmd_setwarnaction))
    app.add_handler(CommandHandler('yeniuye', cmd_yeniuye))
    app.add_handler(CommandHandler('linkizin', cmd_linkizin))
    app.add_handler(CommandHandler('captchasure', cmd_captchasure))
    app.add_handler(CommandHandler('itiraz', cmd_itiraz))
    app.add_handler(CommandHandler('save', cmd_save_note))
    app.add_handler(CommandHandler('not', cmd_get_note))
    app.add_handler(CommandHandler('notlar', cmd_notes))
    app.add_handler(CommandHandler('notsil', cmd_delete_note))
    app.add_handler(CommandHandler(['report', 'rapor'], cmd_report))
    app.add_handler(CommandHandler('filter', cmd_filter))
    app.add_handler(CommandHandler('kurulum', cmd_kurulum))
    app.add_handler(CallbackQueryHandler(setup_callback, pattern=r'^ks\|'))
    app.add_handler(CommandHandler('filters', cmd_filters))
    app.add_handler(CommandHandler('stop', cmd_stop_filter))
    app.add_handler(CommandHandler('stopall', cmd_stopall))
    app.add_handler(CallbackQueryHandler(filter_clear_callback, pattern=r'^fall\|'))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^@admins?\b') & filters.ChatType.GROUPS & filters.REPLY, cmd_report))
    app.add_handler(CommandHandler(['medyaengel', 'paketengel', 'gmedyaengel'], cmd_medya_engel))
    app.add_handler(CommandHandler(['medyakilit', 'medyaac'], cmd_medya_kilit))
    app.add_handler(CommandHandler('kurtar', cmd_kurtar, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r'^panel\|'))

    app.add_handler(CallbackQueryHandler(button_callback, pattern='^select_'))
    app.add_handler(CallbackQueryHandler(giveaway_button, pattern=r'^giveaway\|'))
    app.add_handler(CallbackQueryHandler(remove_admin_callback, pattern=r'^removeadmin\|'))
    app.add_handler(CallbackQueryHandler(toggle_permission_callback, pattern=r'^toggleperm\|'))
    app.add_handler(CallbackQueryHandler(permtab_callback, pattern=r'^permtab\|'))
    app.add_handler(CallbackQueryHandler(saveandexit_callback, pattern=r'^saveandexit\|'))
    app.add_handler(CallbackQueryHandler(saveandexit_callback, pattern=r'^cancelperm\|'))
    app.add_handler(CallbackQueryHandler(setrank_callback, pattern=r'^setrank\|'))
    app.add_handler(CallbackQueryHandler(locked_callback, pattern='^locked$'))
    app.add_handler(CallbackQueryHandler(captcha_callback, pattern=r'^captcha\|'))
    app.add_handler(CallbackQueryHandler(settings_panel_callback, pattern=r'^s\|'))
    app.add_handler(CallbackQueryHandler(mod_action_callback, pattern=r'^m\|'))
    app.add_handler(CallbackQueryHandler(join_captcha_callback, pattern=r'^jcap\|'))
    app.add_handler(CallbackQueryHandler(report_callback, pattern=r'^rp\|'))
    app.add_handler(CallbackQueryHandler(recovery_callback, pattern=r'^r[kcs]\|'))
    app.add_handler(CallbackQueryHandler(nightmod_callback, pattern='^nm_'))
    app.add_handler(CallbackQueryHandler(wordlist_callback, pattern='^wdel'))
    app.add_handler(CallbackQueryHandler(appeal_callback, pattern=r'^appeal(pick)?\|'))
    app.add_handler(CallbackQueryHandler(help_settings_callback, pattern=r'^help_settings$'))

    if main_bot:
        app.add_handler(CommandHandler('klon', cmd_klon, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler('klonlar', cmd_klonlar, filters=filters.ChatType.PRIVATE))
        app.add_handler(CallbackQueryHandler(clone_callback, pattern=r'^cl\|'))
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, clone_input_handler),
                        group=-60)
    # Eski/bozuk buton verisi (ör. önceki sürümden kalan butonlar) çökme yerine kibar uyarı versin
    for handlers in app.handlers.values():
        for h in handlers:
            if isinstance(h, CallbackQueryHandler):
                h.callback = guard_callback(h.callback)
    # Düzenlenen komut mesajı komutu tekrar çalıştırmasın (çift ban/uyarı ve update.message=None hataları)
    for handlers in app.handlers.values():
        for h in handlers:
            if isinstance(h, CommandHandler):
                h.filters = h.filters & ~filters.UpdateType.EDITED

def main():
    global MAIN_BOT
    if not FOUNDER_ID_VALID:
        logger.error("FOUNDER_ID ayarı geçersiz veya boş. Lütfen .env dosyasına sayısal bir FOUNDER_ID yazın.")
        raise SystemExit("FOUNDER_ID ayarı zorunlu ve geçerli olmalıdır.")

    app = (
        Application.builder()
        .token(TOKEN)
        .rate_limiter(UlusRateLimiter(max_retries=3))
        .job_queue(UlusJobQueue())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    MAIN_BOT = app.bot  # modüldeki `bot` vekili ana botu ve çalışan klonları buradan seçer

    register_handlers(app, main_bot=True)

    job_queue = app.job_queue
    job_queue.run_repeating(check_captcha_timeouts, interval=15, first=15)
    job_queue.run_repeating(check_join_captcha_timeouts, interval=15, first=20)
    job_queue.run_repeating(check_media_locks, interval=60, first=15)
    job_queue.run_repeating(admin_snapshot_job, interval=6 * 3600, first=120)
    job_queue.run_repeating(check_raid_locks, interval=60, first=10)
    job_queue.run_repeating(check_nightmod, interval=60, first=30)
    job_queue.run_repeating(check_temp_bans, interval=300, first=60)
    job_queue.run_repeating(check_expired_mutes, interval=300, first=120)
    job_queue.run_repeating(db_cleanup_job, interval=3600, first=600)
    job_queue.run_repeating(checkpoint_job, interval=600, first=60)
    job_queue.run_repeating(clone_health_job, interval=600, first=300)
    job_queue.run_repeating(auto_delete_job, interval=20, first=20)
    job_queue.run_repeating(scheduled_msgs_job, interval=60, first=45)
    job_queue.run_repeating(giveaway_job, interval=60, first=50)
    job_queue.run_repeating(spam_memory_cleanup, interval=3600, first=3600)
    job_queue.run_repeating(weekly_log_cleanup, interval=86400, first=3600)
    job_queue.run_daily(backup_job, time=dtime(4, 0, tzinfo=TZ_TR))
    job_queue.run_daily(run_daily_scheduler, time=dtime(0, 0, tzinfo=TZ_TR))

    logger.info("✅ Bot çalışıyor...")
    def _signal_handler(signum, _frame):
        sig_name = signal.Signals(signum).name if signum else str(signum)
        logger.info(f"Kapatma sinyali alındı: {sig_name}. Bot güvenli şekilde durduruluyor...")
        try:
            app.stop_running()
        except Exception as e:
            logger.debug(f"stop_running hatası: {e}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal_handler)
        except Exception as e:
            logger.debug(f"Signal handler atanamadı ({sig}): {e}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
