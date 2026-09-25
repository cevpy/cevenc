"""
ANKA Security Bot — Kanal / Grup koruma botu
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
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, Bot, MessageEntity
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.error import TelegramError, Forbidden, BadRequest
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
import logging
import os
import re

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

FOUNDER_ID = int(os.getenv("FOUNDER_ID", "0") or 0)
DB_FILE = _env_path("DB_FILE", "bot_data.db")
BACKUP_DIR = _env_path("BACKUP_DIR", "backups")
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7") or 7)
BACKUP_CHAT_ID = int(os.getenv("BACKUP_CHAT_ID", "0") or 0) or FOUNDER_ID

# Telethon istemcisi (opsiyonel). my.telegram.org'dan alınır.
# Not: Bot token ile giriş yapar; gerçek kullanıcı hesabı (userbot) değildir.
USERBOT_API_ID = int(os.getenv("API_ID", "0") or 0)
USERBOT_API_HASH = os.getenv("API_HASH", "").strip()
USERBOT_SESSION = _env_path("TELETHON_SESSION", "anka_userbot")

TZ_TR = timezone(timedelta(hours=3))
TELEGRAM_SERVICE_ID = 777000        # Bağlı kanaldan otomatik iletilen gönderiler
GROUP_ANON_BOT_ID = 1087968824      # Anonim adminler
CHANNEL_BOT_ID = 136817688          # "Kanal olarak" yazan kullanıcılar

userbot: TelegramClient = None
BOT_ID: int = 0

async def get_userbot() -> TelegramClient:
    global userbot
    if userbot and userbot.is_connected():
        return userbot
    return None

# Uygulama oluşturulunca main() içinde application.bot ile değiştirilir
# (rate limiter dahil tek bot nesnesi kullanılır).
bot: Bot = None

_db_lock = asyncio.Lock()


class _ClosingConnection(sqlite3.Connection):
    """`with get_db() as conn:` bloğu bitince commit/rollback yapar VE bağlantıyı kapatır."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
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

            CREATE TABLE IF NOT EXISTS bot_given_admins (
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                given_by INTEGER NOT NULL,
                given_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
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

ROLE_LEVELS = {
    'kurucu': 100,
    'yardimci_kurucu': 90,
    'basadmin': 70,
    'admin': 50,
    None: 0
}

def has_permission(chat_id: str, user_id: int, min_level: int = 50) -> bool:
    if user_id == FOUNDER_ID:
        return True
    with get_db() as conn:
        row = conn.execute(
            "SELECT role FROM roles WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()
    return ROLE_LEVELS.get(row['role'] if row else None, 0) >= min_level

def has_specific_permission(chat_id: str, user_id: int, permission: str) -> bool:
    if user_id == FOUNDER_ID:
        return True
    with get_db() as conn:
        row = conn.execute(
            "SELECT role FROM roles WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()
        if not row:
            return False
        role = row['role']
        if role == 'kurucu':
            return True

        user_override = conn.execute(
            f"SELECT {permission} FROM user_permissions WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()
        if user_override and user_override[permission] is not None:
            return bool(user_override[permission])

        perm_row = conn.execute(
            f"SELECT {permission} FROM role_permissions WHERE chat_id = ? AND role = ?",
            (chat_id, role)
        ).fetchone()

        if perm_row is None:
            defaults = {
                'can_ban': 1 if role in ('yardimci_kurucu', 'basadmin', 'admin') else 0,
                'can_kick': 1 if role in ('yardimci_kurucu', 'basadmin', 'admin') else 0,
                'can_mute': 1 if role in ('yardimci_kurucu', 'basadmin', 'admin') else 0,
                'can_warn': 1 if role in ('yardimci_kurucu', 'basadmin', 'admin') else 0,
                'can_delete': 1 if role in ('yardimci_kurucu', 'basadmin', 'admin') else 0,
                'can_pin': 1 if role in ('yardimci_kurucu', 'basadmin') else 0,
                'can_manage_settings': 1 if role == 'yardimci_kurucu' else 0,
                'can_manage_roles': 1 if role in ('kurucu', 'yardimci_kurucu') else 0,
            }
            conn.execute(
                "INSERT OR IGNORE INTO role_permissions (chat_id, role) VALUES (?, ?)",
                (chat_id, role)
            )
            for p, val in defaults.items():
                conn.execute(
                    f"UPDATE role_permissions SET {p} = ? WHERE chat_id = ? AND role = ?",
                    (val, chat_id, role)
                )
            conn.commit()
            return defaults.get(permission, 0) == 1

        return perm_row[permission] == 1

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
            "SELECT owner_id, log_chat_id, chat_type, settings, stats, invites FROM channels WHERE chat_id = ?",
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
        conn.execute("""
            INSERT OR REPLACE INTO channels
            (chat_id, owner_id, log_chat_id, chat_type, settings, stats, invites)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id, data['owner'], data.get('log_chat_id'),
            data.get('chat_type'), settings_json, stats_json, invites_json
        ))
        conn.commit()
    _settings_cache[chat_id] = (time.time(), copy.deepcopy(data))

async def send_log(chat_id: str, message: str, parse_mode: str | None = None):
    channel = get_channel_settings(chat_id)
    log_id = channel.get('log_chat_id') if channel else None
    if log_id:
        try:
            await bot.send_message(log_id, message, parse_mode=parse_mode, disable_web_page_preview=True)
        except Exception as e:
            logger.debug(f"Log gönderilemedi ({chat_id}): {e}")

# ─────────────────────────── ORTAK YARDIMCILAR ───────────────────────────

def mention(user) -> str:
    """Kullanıcı adı olsun olmasın tıklanabilir HTML etiketi."""
    if user is None:
        return "bilinmeyen"
    name = (getattr(user, 'first_name', None) or getattr(user, 'username', None) or str(user.id))
    return mention_html(user.id, name)

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
    if user_id in (FOUNDER_ID, TELEGRAM_SERVICE_ID, GROUP_ANON_BOT_ID, BOT_ID):
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
    }

async def _register_chat(chat_id: str, owner_id: int, chat_type: str):
    
    default_settings = _default_channel_settings()
    default_stats = {'bans': 0, 'kicks': 0, 'spams': 0, 'joins': 0, 'requests': 0, 'mutes': 0}
    async with _db_lock:
        with get_db() as conn:
            existing = conn.execute("SELECT owner_id FROM channels WHERE chat_id = ?", (chat_id,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO channels
                    (chat_id, owner_id, log_chat_id, chat_type, settings, stats, invites)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    chat_id, owner_id, None,
                    chat_type,
                    json.dumps(default_settings, ensure_ascii=False),
                    json.dumps(default_stats),
                    json.dumps({})
                ))
                conn.execute(
                    "INSERT OR IGNORE INTO roles (chat_id, user_id, role) VALUES (?, ?, 'kurucu')",
                    (chat_id, owner_id)
                )
                if owner_id != FOUNDER_ID:
                    conn.execute(
                        "INSERT OR IGNORE INTO roles (chat_id, user_id, role) VALUES (?, ?, 'kurucu')",
                        (chat_id, FOUNDER_ID)
                    )
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

    if new_status in ['member', 'administrator']:
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

async def apply_punishment(chat_id: str, user_id: int, username: str, reason: str, channel: dict, user=None):
    """Uyarı verir; limit dolunca ayarlanan cezayı (ban/tempban/kick/mute) uygular."""
    if await is_staff_user(chat_id, user_id, channel):
        return
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
    warn_limit = int(settings.get('warn_limit', 5) or 5)
    who = mention(user) if user else html.escape(f"@{username}" if username else str(user_id))
    reason_h = html.escape(reason)

    if warn_count < warn_limit:
        try:
            await bot.send_message(chat_id, f"⚠️ {who} {reason_h} → <b>{warn_count}/{warn_limit}</b> uyarı",
                                   parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug(f"Uyarı mesajı gönderilemedi: {e}")
        await send_log(chat_id, f"⚠️ {who} {reason_h} | Uyarı: {warn_count}/{warn_limit} | {chat_id}", ParseMode.HTML)
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
                    """, (chat_id, user_id, username, reason, now, BOT_ID))
                if action == 'tempban':
                    conn.execute("INSERT OR REPLACE INTO temp_bans (chat_id, user_id, username, unban_at) VALUES (?, ?, ?, ?)",
                                 (chat_id, user_id, username, int(now + duration)))
                if action == 'mute':
                    conn.execute("""
                        INSERT OR REPLACE INTO mute_list (chat_id, user_id, username, until_date, muted_at, muted_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (chat_id, user_id, username, now + duration, now, BOT_ID))
                conn.commit()

        extra = "\n📨 İtiraz için bota özelden /itiraz yazabilir." if action in ('ban', 'tempban') else ""
        await bot.send_message(chat_id, f"🚫 {who} {warn_limit} uyarıya ulaştı → <b>{label}</b> ({reason_h}){extra}",
                               parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"🚫 {who} {label} | Sebep: {reason_h} | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, label, user_id, username or '', BOT_ID, 'bot', reason)
    except Exception as e:
        logger.error(f"Ceza uygulanamadı ({chat_id}/{user_id}): {e}")

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
    keyboard = [[InlineKeyboardButton(str(opt), callback_data=f"captcha|{chat_id}|{user_id}|{opt}") for opt in options]]

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
            await query.edit_message_text(f"✅ {who} doğrulandı! Gruba hoş geldin.", parse_mode=ParseMode.HTML)
            await send_log(chat_id, f"✅ Captcha geçti: {who} | {chat_id}", ParseMode.HTML)
            if context.job_queue:
                context.job_queue.run_once(_delete_message_job, 15,
                                           data={'chat_id': chat_id, 'message_id': query.message.message_id})
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
            parse_mode=ParseMode.HTML
        )
        if context.job_queue:
            context.job_queue.run_once(_delete_message_job, 20, data={'chat_id': chat_id, 'message_id': notice.message_id})
    except Exception as e:
        logger.debug(f"Newbie guard hata: {e}")
    raise ApplicationHandlerStop

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

    mute_minutes = [10, 30, 300][min(count, 2)]
    try:
        await msg.delete()
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id,
            permissions=ChatPermissions.no_permissions(),
            until_date=int(now + mute_minutes * 60)
        )
        await send_log(chat_id, f"🚫 Forward ({count+1}. kez) → {mention(msg.from_user)} {mute_minutes} dk mute", ParseMode.HTML)
    except Exception as e:
        logger.error(f"Anti-forward hata: {e}")

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
        mute_minutes = [10, 30, 300][level]
        try:
            await msg.delete()
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions.no_permissions(),
                until_date=int(now + mute_minutes * 60)
            )
            await send_log(chat_id, f"📸 Media flood ({count+1}) → {mention(msg.from_user)} {mute_minutes} dk mute", ParseMode.HTML)
        except Exception as e:
            logger.error(f"Anti-media hata: {e}")

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
        mute_minutes = [10, 30, 300][level]
        try:
            await msg.delete()
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions.no_permissions(),
                until_date=int(now + mute_minutes * 60)
            )
            # Aynı flood için tek duyuru
            key = f"flood_notice_{chat_id}_{user_id}"
            if now - context.bot_data.get(key, 0) > 30:
                context.bot_data[key] = now
                await bot.send_message(chat_id, f"🌊 {mention(msg.from_user)} flood yaptı → {mute_minutes} dk mute",
                                       parse_mode=ParseMode.HTML)
            await send_log(chat_id, f"🌊 Flood ({count+1} mesaj/{timeframe}sn) → {mention(msg.from_user)} {mute_minutes} dk mute", ParseMode.HTML)
        except Exception as e:
            logger.error(f"Anti-flood hata: {e}")

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

    user = msg.from_user
    try:
        await msg.delete()
        await send_log(chat_id, f"🔗 Link silindi → {mention(user)} | {chat_id}", ParseMode.HTML)
        await apply_punishment(chat_id, user.id, user.username or user.first_name, "link gönderme", channel, user=user)
    except Exception as e:
        logger.error(f"Anti-link hata: {e}")
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

    user = message.from_user
    user_id = user.id
    username = user.username or user.first_name

    if settings.get('word_ban_enabled'):
        hit = find_banned_word(text_raw, settings.get('banned_words', []))
        if hit:
            try:
                await message.delete()
            except Exception:
                pass
            if message.sender_chat:
                await _sender_chat_violation(message, chat_id, "Yasaklı kelime")
                return
            channel['stats']['spams'] = channel['stats'].get('spams', 0) + 1
            save_channel_settings(chat_id, channel)
            await apply_punishment(chat_id, user_id, username, "yasaklı kelime", channel, user=user)
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
                except Exception:
                    pass
                channel['stats']['spams'] = channel['stats'].get('spams', 0) + 1
                save_channel_settings(chat_id, channel)
                await apply_punishment(chat_id, user_id, username, "spam", channel, user=user)

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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok.")
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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok.")
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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok.")
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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok.")
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
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok. (Baş Admin ve üstü gerekli)")
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
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 100):
        await update.message.reply_text("Sadece owner kullanabilir!")
        return

    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı veya grupta değil.")
        return

    try:
        await bot.promote_chat_member(
            chat_id=chat_id, user_id=user_id,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=False,
        )
        async with _db_lock:
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO roles (chat_id, user_id, role) VALUES (?, ?, 'admin')",
                    (chat_id, user_id)
                )
                conn.commit()

        who = mention(member.user)
        keyboard = [[
            InlineKeyboardButton("Yetkiler", url=f"https://t.me/{context.bot.username}?start=aup_{chat_id}_{user_id}"),
            InlineKeyboardButton("Kaldir", callback_data=f"removeadmin|{chat_id}|{user_id}")
        ]]
        await update.message.reply_text(f"✅ {who} admin yapıldı!", reply_markup=InlineKeyboardMarkup(keyboard),
                                        parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"👑 {who} admin yapıldı → {chat_id}", ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"Hata: {e.message}")

async def remove_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("removeadmin|"):
        return
    _, chat_id, target_str = query.data.split("|", 2)
    target_user_id = int(target_str)
    caller_id = query.from_user.id
    if not has_specific_permission(chat_id, caller_id, "can_manage_roles"):
        await query.answer("Bu işlemi yapmaya yetkiniz yok.", show_alert=True)
        return
    try:
        await bot.promote_chat_member(
            chat_id=chat_id, user_id=target_user_id,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False,
        )
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM roles WHERE chat_id = ? AND user_id = ?", (chat_id, target_user_id))
                conn.commit()
        member = await bot.get_chat_member(chat_id, target_user_id)
        who = mention(member.user)
        await query.edit_message_text(f"✅ {who} adminlikten alındı.", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"🗑 {who} adminlikten çıkarıldı | {chat_id}", ParseMode.HTML)
    except TelegramError as e:
        await query.edit_message_text(f"Hata: {e.message}")

async def remove_admin(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 100):
        await update.message.reply_text("Sadece owner kullanabilir!")
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı.")
        return
    if user_id == update.effective_user.id:
        await update.message.reply_text("Kendini çıkaramazsın.")
        return
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM roles WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            conn.execute("DELETE FROM bot_given_admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            conn.commit()
    try:
        await bot.promote_chat_member(
            chat_id=chat_id, user_id=user_id,
            can_delete_messages=False, can_invite_users=False,
            can_restrict_members=False, can_pin_messages=False,
            can_promote_members=False, can_manage_chat=False,
        )
    except Exception as e:
        logger.debug(f"remove_admin: {e}")
    who = mention(member.user)
    await update.message.reply_text(f"✅ {who} artık admin değil.", parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"🗑 {who} adminlikten çıkarıldı | {chat_id}", ParseMode.HTML)

async def klasorcu(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    channel = get_channel_settings(chat_id)
    if update.effective_user.id != channel['owner']:
        await update.message.reply_text("Sadece owner kullanabilir!")
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
        await update.message.reply_text(f"Hata: {e}")

async def ban(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
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
            parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"🚫 {who} {ban_type} | Sebep: {reason_h} | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, ban_type, user_id, username, update.effective_user.id, update.effective_user.username or '', reason)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def unban(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
        await update.message.reply_text(f"Hata: {e}")

async def kick(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
        await update.message.reply_text(f"Hata: {e}")

async def mute(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
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
        await update.message.reply_text(f"🔇 {who} {duration_str} mute edildi!", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"🔇 {who} {duration_str} mute | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, 'mute', user_id, username, update.effective_user.id, update.effective_user.username or '', duration_str)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def unmute(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
                can_send_other_messages=True,
                can_add_web_page_previews=True, can_invite_users=True,
            )
        )
        async with _db_lock:
            with get_db() as conn:
                conn.execute("DELETE FROM mute_list WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                conn.commit()
        who = mention(member.user)
        await update.message.reply_text(f"🔊 {who} unmute edildi!", parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"🔊 {who} unmute edildi | {chat_id}", ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def warn(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return

    if update.message.reply_to_message:
        reason = ' '.join(context.args) if context.args else "Sebep belirtilmedi"
    else:
        reason = ' '.join(context.args[1:]) if len(context.args) > 1 else "Sebep belirtilmedi"

    username = member.user.username or member.user.first_name
    who = mention(member.user)
    reason_h = html.escape(reason)
    channel = get_channel_settings(chat_id)

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

    warn_limit = channel['settings'].get('warn_limit', 5)
    if warn_count >= warn_limit:
        try:
            await bot.ban_chat_member(chat_id, user_id)
            channel['stats']['bans'] = channel['stats'].get('bans', 0) + 1
            save_channel_settings(chat_id, channel)
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                    conn.commit()
            await update.message.reply_text(f"🚫 {who} {warn_limit} uyarıya ulaştı → banlandı!\nSebep: {reason_h}",
                                            parse_mode=ParseMode.HTML)
            await send_log(chat_id, f"🚫 {who} {warn_limit} uyarı → ban | Sebep: {reason_h} | {chat_id}", ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"Hata: {e}")
    else:
        await update.message.reply_text(f"⚠️ {who} uyarıldı! Sebep: {reason_h} | Uyarı: {warn_count}/{warn_limit}",
                                        parse_mode=ParseMode.HTML)
        await send_log(chat_id, f"⚠️ {who} uyarıldı ({warn_count}/{warn_limit}) | Sebep: {reason_h} | {chat_id}", ParseMode.HTML)
        await log_mod_action(chat_id, 'warn', user_id, username, update.effective_user.id, update.effective_user.username or '', reason)

async def unwarn(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    user_id, member = await resolve_user(
        chat_id,
        context.args[0] if context.args else None,
        update.message.reply_to_message.from_user if update.message.reply_to_message else None
    )
    if not member:
        await update.message.reply_text("Kullanıcı bulunamadı!")
        return
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            conn.commit()
    who = mention(member.user)
    await update.message.reply_text(f"✅ {who} uyarıları temizlendi!", parse_mode=ParseMode.HTML)
    await send_log(chat_id, f"🧹 {who} uyarıları temizlendi | {chat_id}", ParseMode.HTML)

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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
        await update.message.reply_text(f"Hata: {e}")

async def unpin(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
        await update.message.reply_text(f"Hata: {e}")

async def slowmode(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok! (Baş Admin ve üstü gerekli)")
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
        await update.message.reply_text(f"Hata: {e}")

async def temizle(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok!")
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /temizle <sayi> veya /temizle all")
        return

    arg = context.args[0].lower()
    delete_all = (arg == 'all')

    if not delete_all and not arg.isdigit():
        await update.message.reply_text("Kullanim: /temizle <sayi> veya /temizle all")
        return

    cmd_msg_id = update.message.message_id
    by_who = mention(update.effective_user)

    try:
        await update.message.delete()
    except Exception as e:
        logger.debug(f"temizle: {e}")

    if delete_all:
        status_msg = await bot.send_message(chat_id, "Tum mesajlar siliniyor, lutfen bekle...")
        deleted_users = 0
        skipped = 0

        try:
            admins = await bot.get_chat_administrators(chat_id)
            admin_ids = {a.user.id for a in admins}
            admin_ids.add(BOT_ID or 0)

            members_to_clean = []
            async for member in bot.get_chat_members(chat_id):
                if member.user.id not in admin_ids and not member.user.is_bot:
                    members_to_clean.append(member.user.id)

            total = len(members_to_clean)
            for i, user_id in enumerate(members_to_clean):
                try:
                    await bot.ban_chat_member(chat_id, user_id, revoke_messages=True)
                    await asyncio.sleep(0.1)
                    await bot.unban_chat_member(chat_id, user_id)
                    deleted_users += 1
                except Exception:
                    skipped += 1
                await asyncio.sleep(0.2)

                if (i + 1) % 20 == 0:
                    try:
                        await status_msg.edit_text(
                            f"Siliniyor... {i+1}/{total} uye islendi."
                        )
                    except Exception as e:
                        logger.debug(f"temizle: {e}")

        except Exception as e:
            logger.error(f"temizle all hata: {e}")
            deleted = 0
            consecutive_errors = 0
            i_id = cmd_msg_id - 1
            batch = []
            while i_id > max(1, cmd_msg_id - 20000):
                batch.append(i_id)
                i_id -= 1
                if len(batch) == 100:
                    results = await asyncio.gather(
                        *[bot.delete_message(chat_id, mid) for mid in batch],
                        return_exceptions=True
                    )
                    ok = sum(1 for r in results if not isinstance(r, Exception))
                    err = len(results) - ok
                    deleted += ok
                    consecutive_errors = 0 if ok > 0 else consecutive_errors + err
                    batch = []
                    if consecutive_errors > 200:
                        break
                    await asyncio.sleep(0.3)
            if batch:
                results = await asyncio.gather(
                    *[bot.delete_message(chat_id, mid) for mid in batch],
                    return_exceptions=True
                )
                deleted += sum(1 for r in results if not isinstance(r, Exception))
            try:
                await status_msg.edit_text(f"{deleted} mesaj silindi.")
            except Exception as e2:
                logger.debug(f"temizle: {e2}")
            await asyncio.sleep(4)
            try:
                await status_msg.delete()
            except Exception as e2:
                logger.debug(f"temizle: {e2}")
            await send_log(chat_id, f"Temizle all (fallback): {deleted} mesaj | {by_who}", ParseMode.HTML)
            return

        try:
            await status_msg.edit_text(
                f"Tamamlandi! {deleted_users} uyenin mesajlari silindi."
                + (f" ({skipped} uye atlandi)" if skipped else "")
            )
        except Exception as e:
            logger.debug(f"temizle: {e}")
        await asyncio.sleep(5)
        try:
            await status_msg.delete()
        except Exception as e:
            logger.debug(f"temizle: {e}")
        await send_log(chat_id, f"Temizle all: {deleted_users} uye | {by_who}", ParseMode.HTML)

    else:
        count = min(int(arg), 500)
        deleted = 0
        consecutive_errors = 0
        i_id = cmd_msg_id - 1
        batch = []

        while i_id > max(1, cmd_msg_id - count * 5) and deleted < count:
            batch.append(i_id)
            i_id -= 1
            if len(batch) == 100:
                results = await asyncio.gather(
                    *[bot.delete_message(chat_id, mid) for mid in batch],
                    return_exceptions=True
                )
                ok = sum(1 for r in results if not isinstance(r, Exception))
                err = len(results) - ok
                deleted += ok
                consecutive_errors = 0 if ok > 0 else consecutive_errors + err
                batch = []
                if consecutive_errors > 100:
                    break
                await asyncio.sleep(0.3)

        if batch and deleted < count:
            results = await asyncio.gather(
                *[bot.delete_message(chat_id, mid) for mid in batch],
                return_exceptions=True
            )
            deleted += sum(1 for r in results if not isinstance(r, Exception))

        confirm = await bot.send_message(chat_id, f"{deleted} mesaj silindi.")
        await asyncio.sleep(4)
        try:
            await confirm.delete()
        except Exception as e:
            logger.debug(f"temizle: {e}")
        await send_log(chat_id, f"Temizle {count}: {deleted} mesaj | {by_who}", ParseMode.HTML)

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
    msg = "🚫 <b>Ban Listesi</b> (son 20):\n\n"
    for row in rows:
        who = mention_html(row['user_id'], row['username'] or str(row['user_id']))
        dt = datetime.fromtimestamp(row['banned_at'], TZ_TR).strftime('%d.%m.%Y')
        msg += f"• {who} — {html.escape(row['reason'] or '?')} ({dt})\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

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
    msg = "🔇 <b>Mute Listesi</b> (son 20):\n\n"
    for row in rows:
        who = mention_html(row['user_id'], row['username'] or str(row['user_id']))
        if row['until_date'] and row['until_date'] > now:
            remaining = int((row['until_date'] - now) / 60)
            time_str = f"{remaining} dk kaldı"
        else:
            time_str = "süresi dolmuş"
        msg += f"• {who} — {time_str}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def spam_koruma(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
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
    if update.effective_user.id != channel['owner']:
        await update.message.reply_text("Sadece owner kullanabilir!")
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
    if update.effective_user.id != channel['owner']:
        await update.message.reply_text("Sadece owner kullanabilir!")
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
    if update.effective_user.id != channel['owner']:
        await update.message.reply_text("Sadece owner kullanabilir!")
        return
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Kullanım: /setautorejectbot on|off")
        return
    state = context.args[0].lower() == 'on'
    channel['settings']['auto_reject_bot'] = state
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Bot/sahte hesap reddi {'açıldı' if state else 'kapatıldı'}!")

async def set_welcome(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    channel = get_channel_settings(chat_id)
    if channel['chat_type'] == 'channel':
        await update.message.reply_text("Kanallarda hosgeldin mesaji kullanilamaz!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    if not context.args:
        current = channel['settings'].get('welcome_msg', '')
        await update.message.reply_text(
            "Kullanim: /setwelcome <mesaj>\n\n"
            "Degiskenler:\n"
            "{kullanici} — Ad\n"
            "{username} — @kullanici adi\n"
            "{id} — Kullanici ID\n"
            "{kanal} — Grup adi\n"
            "{uye_sayisi} — Toplam uye sayisi\n\n"
            f"Mevcut: {current or 'Ayarlanmamis'}"
        )
        return
    welcome_msg = ' '.join(context.args)
    channel['settings']['welcome_msg'] = welcome_msg
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Hosgeldin mesaji ayarlandi:\n{welcome_msg}")

async def stats(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    channel = get_channel_settings(chat_id)
    s = channel['stats']
    await update.message.reply_text(
        f"📊 İstatistik: {chat_id}\n"
        f"Banlar: {s.get('bans', 0)}\n"
        f"Muteler: {s.get('mutes', 0)}\n"
        f"Kickler: {s.get('kicks', 0)}\n"
        f"Spam Tespit: {s.get('spams', 0)}\n"
        f"Yeni Üye: {s.get('joins', 0)}\n"
        f"İstekler: {s.get('requests', 0)}"
    )

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

async def cekilis(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    keyboard = [[InlineKeyboardButton("🎉 Katil", callback_data=f"giveaway|{chat_id}")]]
    message = await bot.send_message(chat_id, "🎉 Çekiliş başladı! Katılımcı: 0", reply_markup=InlineKeyboardMarkup(keyboard))
    async with _db_lock:
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO giveaways (chat_id, message_id, participants, created_at)
                VALUES (?, ?, ?, ?)
            """, (chat_id, message.message_id, json.dumps([]), time.time()))
            conn.commit()
    await update.message.reply_text("Çekiliş başladı! Katılmak için butona bas.")
    await send_log(chat_id, f"🎉 {mention(update.effective_user)} çekiliş başlattı | {chat_id}", ParseMode.HTML)

async def giveaway_button(update: Update, context):
    query = update.callback_query
    await query.answer()
    chat_id = query.data.split("|", 1)[1]
    if not get_channel_settings(chat_id):
        await query.message.edit_text("Bu çekiliş geçersiz!")
        return
    user_id = query.from_user.id
    async with _db_lock:
        with get_db() as conn:
            row = conn.execute("SELECT participants FROM giveaways WHERE chat_id = ?", (chat_id,)).fetchone()
            if not row:
                await query.message.edit_text("Bu çekiliş sona erdi!")
                return
            participants = json.loads(row['participants'])
            if user_id not in participants:
                participants.append(user_id)
                conn.execute("UPDATE giveaways SET participants = ? WHERE chat_id = ?", (json.dumps(participants), chat_id))
                conn.commit()
                await query.message.edit_text(
                    f"🎉 Çekiliş devam ediyor! Katılımcı: {len(participants)}",
                    reply_markup=query.message.reply_markup
                )
                await send_log(chat_id, f"🙋 {mention(query.from_user)} çekilişe katıldı | {chat_id}", ParseMode.HTML)

async def giveaway_end(update: Update, context):
    """/cekilis_bitir — aktif çekilişi bitirir ve kazananı seçer. (/cekilis_bitir 3 → 3 kazanan)"""
    message = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await message.reply_text("Yetkin yok!")
        return
    winners_count = 1
    if context.args and context.args[0].isdigit():
        winners_count = max(1, min(20, int(context.args[0])))
    with get_db() as conn:
        row = conn.execute("SELECT message_id, participants FROM giveaways WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row:
        await message.reply_text("Aktif çekiliş yok.")
        return
    participants = json.loads(row['participants'])
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM giveaways WHERE chat_id = ?", (chat_id,))
            conn.commit()
    try:
        await bot.edit_message_reply_markup(chat_id, row['message_id'], reply_markup=None)
    except Exception:
        pass
    if not participants:
        await bot.send_message(chat_id, "🎉 Çekiliş sona erdi! Katılımcı yoktu.")
        return
    winners = random.sample(participants, min(winners_count, len(participants)))
    names = []
    for wid in winners:
        try:
            m = await bot.get_chat_member(chat_id, wid)
            names.append(mention(m.user))
        except Exception:
            names.append(mention_html(wid, str(wid)))
    text = "🎉 Çekiliş sona erdi!\n🏆 Kazanan" + ("lar" if len(names) > 1 else "") + ": " + ", ".join(names)
    text += f"\n👥 Katılımcı: {len(participants)}"
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    if message.chat.type == 'private':
        await message.reply_text("✅ Çekiliş bitirildi.")
    await send_log(chat_id, f"🏆 Çekiliş bitti: {', '.join(names)} | {chat_id}", ParseMode.HTML)

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
            _raid_locked.add(chat_id)
            saved = None
            try:
                chat = await bot.get_chat(chat_id)
                saved = chat.permissions.to_dict() if chat.permissions else None
            except Exception as e:
                logger.debug(f"Raid öncesi izinler alınamadı: {e}")
            try:
                await bot.set_chat_permissions(chat_id=chat_id, permissions=ChatPermissions.no_permissions())
            except Exception as e:
                logger.error(f"Raid kilitleme hata: {e}")
            # Kilit bilgisi DB'ye yazılır → bot yeniden başlasa da 10 dk sonra açılır
            ch = get_channel_settings(chat_id)
            if ch:
                ch['settings']['raid_lock'] = {'until': now + RAID_LOCK_SECONDS, 'saved_perms': saved}
                save_channel_settings(chat_id, ch)

            await send_log(
                chat_id,
                f"🚨 RAİD TESPİT EDİLDİ!\n"
                f"{count} üye / {timeframe} saniye — grup kilitlendi!\n"
                f"Kilidi açmak için: /antiraid_ac\n"
                f"Kanal: {chat_id}"
            )
            await notify_managers(chat_id, f"🚨 Raid tespit edildi, grup {RAID_LOCK_SECONDS // 60} dk kilitlendi: {chat_id}")

        return True
    return False

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
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok! (Baş Admin ve üstü gerekli)")
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
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok!")
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
    last_warn = datetime.fromtimestamp(warn_row['last_warn_at']).strftime('%d.%m.%Y') if warn_row and warn_row['last_warn_at'] else "-"

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
            if row['by_user_id'] and row['by_user_id'] != BOT_ID:
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
        keyboard.append([InlineKeyboardButton(
            f"Sil: {word}",
            callback_data=f"wdel|{chat_id}|{i}"
        )])
    keyboard.append([InlineKeyboardButton("Tumunu Sil", callback_data=f"wdel_all|{chat_id}")])
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
    await query.answer()
    data = query.data

    if data.startswith("wdel_close|"):
        await query.edit_message_text("Kelime listesi kapatildi.")
        return

    if data.startswith("wdel_all|"):
        chat_id = data.split("|")[1]
        if not has_permission(chat_id, query.from_user.id, 50):
            await query.answer("Yetkin yok!", show_alert=True)
            return
        channel = get_channel_settings(chat_id)
        channel['settings']['banned_words'] = []
        save_channel_settings(chat_id, channel)
        await query.edit_message_text("Tum yasakli kelimeler silindi.")
        await send_log(chat_id, f"Tum yasakli kelimeler silindi | {mention(query.from_user)}", ParseMode.HTML)
        return

    if data.startswith("wdel|"):
        parts = data.split("|")
        chat_id = parts[1]
        idx = int(parts[2])
        if not has_permission(chat_id, query.from_user.id, 50):
            await query.answer("Yetkin yok!", show_alert=True)
            return
        channel = get_channel_settings(chat_id)
        words = channel['settings'].get('banned_words', [])
        if idx < len(words):
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
            keyboard.append([InlineKeyboardButton(f"Sil: {word}", callback_data=f"wdel|{chat_id}|{i}")])
        keyboard.append([InlineKeyboardButton("Tumunu Sil", callback_data=f"wdel_all|{chat_id}")])
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
            json.dumps(data.get('saved_permissions')) if data.get('saved_permissions') else None,
            data.get('is_active', 0),
            data.get('configured', 0),
        ))
        conn.commit()

def _nightmod_keyboard(chat_id: str, restrictions: dict) -> InlineKeyboardMarkup:
    def btn(label, key):
        state = "ON" if restrictions.get(key, False) else "OFF"
        icon = "✅" if restrictions.get(key, False) else "❌"
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"nm_toggle|{chat_id}|{key}")
    keyboard = [
        [btn("Mesaj Gonderme", "block_messages"),
         btn("Medya Gonderme", "block_media")],
        [btn("Ses/Video Not", "block_voice"),
         btn("Sticker/GIF", "block_sticker")],
        [btn("Link Gonderme", "block_links"),
         btn("Dosya Gonderme", "block_files")],
        [InlineKeyboardButton("Kaydet", callback_data=f"nm_save|{chat_id}"),
         InlineKeyboardButton("Iptal", callback_data=f"nm_cancel|{chat_id}")]
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
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok! (Bas Admin ve ustu gerekli)")
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
    
    nm = get_nightmod(chat_id)
    if not nm:
        return

    try:
        chat = await bot.get_chat(chat_id)
        perms = chat.permissions
        if perms:
            saved = {
                'can_send_messages': getattr(perms, 'can_send_messages', True),
                'can_send_other_messages': getattr(perms, 'can_send_other_messages', True),
                'can_add_web_page_previews': getattr(perms, 'can_add_web_page_previews', True),
                'can_invite_users': getattr(perms, 'can_invite_users', True),
            }
        else:
            saved = {
                'can_send_messages': True,
                'can_send_other_messages': True,
                'can_add_web_page_previews': True,
                'can_invite_users': True,
            }

        restrictions = json.loads(nm['restrictions']) if isinstance(nm['restrictions'], str) else nm['restrictions']

        new_perms = ChatPermissions(
            can_send_messages=not restrictions.get('block_messages', False),
            can_send_other_messages=not restrictions.get('block_sticker', False),
            can_add_web_page_previews=not restrictions.get('block_links', False),
            can_invite_users=perms.can_invite_users,
        )

        await bot.set_chat_permissions(chat_id, new_perms)

        nm_data = dict(nm)
        nm_data['is_active'] = 1
        nm_data['saved_permissions'] = saved
        nm_data['restrictions'] = restrictions
        save_nightmod(chat_id, nm_data)

        sh = nm['start_hour']
        sm = nm['start_minute']
        eh = nm['end_hour']
        em = nm['end_minute']
        await bot.send_message(
            chat_id,
            f"Night mode basladi. Sabah {eh:02d}:{em:02d}'e kadar kisitlamalar aktif."
        )
        await send_log(chat_id, f"Night mode aktif oldu | {chat_id}")
    except Exception as e:
        logger.error(f"Night mode activate hata: {e}")

async def nightmod_deactivate(chat_id: str):
    
    nm = get_nightmod(chat_id)
    if not nm or not nm['is_active']:
        return

    try:
        saved_raw = nm['saved_permissions']
        if saved_raw:
            saved = json.loads(saved_raw) if isinstance(saved_raw, str) else saved_raw
            restore_perms = ChatPermissions(
                can_send_messages=saved.get('can_send_messages', True),
                can_send_other_messages=saved.get('can_send_other_messages', True),
                can_add_web_page_previews=saved.get('can_add_web_page_previews', True),
                can_invite_users=saved.get('can_invite_users', True),
            )
            await bot.set_chat_permissions(chat_id, restore_perms)

        nm_data = dict(nm)
        nm_data['is_active'] = 0
        nm_data['saved_permissions'] = None
        nm_data['restrictions'] = json.loads(nm_data['restrictions']) if isinstance(nm_data['restrictions'], str) else nm_data['restrictions']
        save_nightmod(chat_id, nm_data)

        eh = nm['end_hour']
        em = nm['end_minute']
        await bot.send_message(chat_id, f"Night mode sona erdi. Normal izinler geri yuklendi.")
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
    await query.answer()
    data = query.data

    if data.startswith("nm_cancel|"):
        await query.edit_message_text("Night mode yapilandirmasi iptal edildi.")
        return

    if data.startswith("nm_save|"):
        chat_id = data.split("|")[1]
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
        parts = data.split("|")
        chat_id = parts[1]
        key = parts[2]

        if not has_permission(chat_id, query.from_user.id, 70):
            await query.answer("Yetkin yok!", show_alert=True)
            return

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

    async with _db_lock:
        with get_db() as conn:
            for row in rows:
                conn.execute(
                    "DELETE FROM temp_bans WHERE chat_id = ? AND user_id = ?",
                    (row['chat_id'], row['user_id'])
                )
                conn.execute(
                    "DELETE FROM ban_list WHERE chat_id = ? AND user_id = ?",
                    (row['chat_id'], row['user_id'])
                )
            conn.commit()

    for row in rows:
        who = mention_html(row['user_id'], row['username'] or str(row['user_id']))
        await send_log(row['chat_id'], f"⏰ Geçici ban sona erdi → {who} unban edildi", ParseMode.HTML)

    logger.info(f"[TEMP BAN] {len(rows)} geçici ban temizlendi.")

def _build_settings_keyboard(chat_id: str, settings: dict) -> InlineKeyboardMarkup:
    
    def btn(label: str, key: str, page: str) -> InlineKeyboardButton:
        state = "✅" if settings.get(key, False) else "❌"
        return InlineKeyboardButton(f"{state} {label}", callback_data=f"stg_{page}|{key}|{chat_id}")

    nm = get_nightmod(chat_id)
    nm_enabled = nm['enabled'] if nm else False
    nm_icon = "✅" if nm_enabled else "❌"

    keyboard = [
        [btn("Spam Koruma", "spam_protection", "toggle"),
         btn("Anti-Flood", "anti_spam_flood", "toggle")],
        [btn("Anti-Link", "anti_link", "toggle"),
         btn("Anti-Forward", "anti_forward", "toggle")],
        [btn("Anti-Media Flood", "anti_media_flood", "toggle"),
         btn("Anti-Raid", "anti_raid", "toggle")],
        [btn("Kelime Yasaklama", "word_ban_enabled", "toggle"),
         btn("Captcha", "captcha_enabled", "toggle")],
        [btn("Oto Kabul", "auto_accept", "toggle"),
         btn("Oto Red", "auto_reject", "toggle")],
        [btn("Bot Red", "auto_reject_bot", "toggle"),
         InlineKeyboardButton(f"{nm_icon} Night Mode", callback_data=f"nm_settings|{chat_id}")],
        [InlineKeyboardButton("ℹ️ Limitler", callback_data=f"stg_limits|{chat_id}"),
         InlineKeyboardButton("❌ Kapat", callback_data=f"stg_close|{chat_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def _build_settings_text(chat_id: str, settings: dict) -> str:
    def s(key): return "✅" if settings.get(key, False) else "❌"

    flood_lim = settings.get('flood_limit', 7)
    flood_tf = settings.get('flood_timeframe', 5)
    media_lim = settings.get('media_flood_limit', 5)
    raid_lim = settings.get('raid_limit', 10)
    raid_tf = settings.get('raid_timeframe', 30)

    return (
        f"⚙️ Ayarlar Paneli\n"
        f"Kanal: {chat_id}\n\n"
        f"Butona basarak ac/kapat:\n\n"
        f"{s('spam_protection')} Spam Koruma\n"
        f"{s('anti_spam_flood')} Anti-Flood ({flood_lim} mesaj/{flood_tf}sn)\n"
        f"{s('anti_link')} Anti-Link\n"
        f"{s('anti_forward')} Anti-Forward\n"
        f"{s('anti_media_flood')} Anti-Media Flood ({media_lim} medya)\n"
        f"{s('anti_raid')} Anti-Raid ({raid_lim} üye/{raid_tf}sn)\n"
        f"{s('word_ban_enabled')} Kelime Yasaklama ({len(settings.get('banned_words', []))} kelime)\n"
        f"{s('captcha_enabled')} Captcha\n"
        f"{s('auto_accept')} Otomatik Kabul\n"
        f"{s('auto_reject')} Otomatik Red\n"
        f"{s('auto_reject_bot')} Bot Red\n"
        + (f"Night Mode: ACIK\n" if get_nightmod(chat_id) and get_nightmod(chat_id)['enabled'] else f"Night Mode: KAPALI\n")
    )

async def cmd_settings(update: Update, context):
    
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await update.message.reply_text("Yetkin yok!")
        return
    channel = get_channel_settings(chat_id)
    settings = channel['settings']
    text = _build_settings_text(chat_id, settings)
    keyboard = _build_settings_keyboard(chat_id, settings)
    await update.message.reply_text(text, reply_markup=keyboard)

async def settings_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    query = update.callback_query
    data = query.data

    if data.startswith("nm_settings|"):
        await query.answer()
        chat_id = data.split("|")[1]
        nm = get_nightmod(chat_id)
        restrictions = json.loads(nm['restrictions']) if nm and isinstance(nm['restrictions'], str) else (nm['restrictions'] if nm else {})
        nm_enabled = nm['enabled'] if nm else False
        sh = nm['start_hour'] if nm else 23
        sm = nm['start_minute'] if nm else 0
        eh = nm['end_hour'] if nm else 7
        em = nm['end_minute'] if nm else 0
        keyboard = _nightmod_keyboard(chat_id, restrictions)
        await query.message.reply_text(
            f"Night Mode: {'ACIK' if nm_enabled else 'KAPALI'}\n"
            f"Saat: {sh:02d}:{sm:02d} - {eh:02d}:{em:02d} (UTC+3)\n\n"
            f"Kisitlamaları sec:",
            reply_markup=keyboard
        )
        return

    if data.startswith("stg_close|"):
        await query.answer()
        await query.edit_message_text("Ayarlar paneli kapatildi.")
        return

    if data.startswith("stg_limits|"):
        chat_id = data.split("|", 1)[1]
        channel = get_channel_settings(chat_id)
        if not channel:
            await query.answer("Kanal bulunamadi.", show_alert=True)
            return
        s = channel['settings']
        await query.answer(
            f"Flood: {s.get('flood_limit',7)}msg/{s.get('flood_timeframe',5)}sn | "
            f"Media: {s.get('media_flood_limit',5)}medya | "
            f"Raid: {s.get('raid_limit',10)}uye/{s.get('raid_timeframe',30)}sn",
            show_alert=True
        )
        return

    if not data.startswith("stg_toggle|"):
        await query.answer()
        return

    parts = data.split("|")
    if len(parts) != 3:
        await query.answer("Hata.", show_alert=True)
        return

    _, key, chat_id = parts

    caller_id = query.from_user.id
    if not has_permission(chat_id, caller_id, 50):
        await query.answer("Yetkin yok!", show_alert=True)
        return

    channel = get_channel_settings(chat_id)
    if not channel:
        await query.answer("Kanal bulunamadı.", show_alert=True)
        return

    current = channel['settings'].get(key, False)
    channel['settings'][key] = not current

    if key == 'auto_accept' and channel['settings'][key]:
        channel['settings']['auto_reject'] = False
    elif key == 'auto_reject' and channel['settings'][key]:
        channel['settings']['auto_accept'] = False

    save_channel_settings(chat_id, channel)

    new_state = channel['settings'][key]
    emoji = "✅" if new_state else "❌"
    await query.answer(f"{key.replace('_', ' ').title()} → {emoji}", show_alert=False)

    settings = channel['settings']
    text = _build_settings_text(chat_id, settings)
    keyboard = _build_settings_keyboard(chat_id, settings)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Settings panel yenileme hata: {e}")

    await send_log(chat_id, f"⚙️ {html.escape(key)} → {'açıldı' if new_state else 'kapatıldı'} | {mention(query.from_user)}", ParseMode.HTML)

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
    await query.message.reply_text(_build_settings_text(chat_id, channel['settings']),
                                   reply_markup=_build_settings_keyboard(chat_id, channel['settings']))

async def duyuru(update: Update, context):
    if update.effective_user.id != FOUNDER_ID:
        await update.message.reply_text("Bu komut sadece botun kurucusu tarafından kullanılabilir!")
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /duyuru <mesaj>")
        return
    parts = (update.message.text or '').split(maxsplit=1)
    duyuru_msg = parts[1] if len(parts) > 1 else ' '.join(context.args)  # satır sonları korunur
    sent_count = 0
    failed_count = 0
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM channels").fetchall()
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
                last_seen  = excluded.last_seen
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

    for user in update.message.new_chat_members:
        if not user.is_bot:
            upsert_user(chat_id, user)
            async with _db_lock:
                with get_db() as conn:
                    conn.execute("INSERT OR REPLACE INTO newcomers (chat_id, user_id, joined_at) VALUES (?, ?, ?)",
                                 (chat_id, user.id, time.time()))
                    conn.commit()
        username = user.username or user.first_name

        if is_gbanned(user.id) and user.id != FOUNDER_ID:
            try:
                await bot.ban_chat_member(chat_id, user.id)
                await send_log(chat_id, f"🌐 Global banlı kullanıcı katıldı ve banlandı: {mention(user)}", ParseMode.HTML)
            except Exception as e:
                logger.debug(f"Gban uygulanamadı: {e}")
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

        if channel['settings'].get('captcha_enabled', False) and not user.is_bot:
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
            welcome_msg = html.escape(channel['settings'].get('welcome_msg') or 'Merhaba {kullanıcı}, {kanal} grubuna hoş geldin!', quote=False)
            try:
                member_count = await bot.get_chat_member_count(chat_id)
            except Exception:
                member_count = '?'
            welcome_msg = (welcome_msg
                .replace('{kullanıcı}', mention(user))
                .replace('{kullanici}', mention(user))
                .replace('{username}', html.escape(f"@{user.username}" if user.username else username))
                .replace('{id}', str(user.id))
                .replace('{kanal}', html.escape(update.message.chat.title or chat_id))
                .replace('{uye_sayisi}', str(member_count))
            )
            try:
                await update.message.reply_text(welcome_msg, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.debug(f"new_member_handler: {e}")
            await send_log(chat_id, f"👋 {mention(user)} katıldı → {chat_id}", ParseMode.HTML)

    await track_invite(update, context)

async def track_invite(update: Update, context):
    chat_id = str(update.message.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return
    inviter_id = update.message.from_user.id
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
    if not get_channel_settings(chat_id):
        return

    upsert_user(chat_id, user)

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
    start_ts = (day_start - timedelta(hours=3)).timestamp()
    end_ts = (day_end - timedelta(hours=3)).timestamp()
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
    start_utc = (start3 - timedelta(hours=3)).timestamp()
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
    
    chat_id = str(update.effective_chat.id)
    if not get_channel_settings(chat_id):
        await update.message.reply_text("Bu grup kayıtlı değil!")
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

    since_ts = (last_monday3 - timedelta(hours=3)).timestamp()
    until_ts = (this_monday3 - timedelta(hours=3)).timestamp()

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
    
    chat_id = str(update.effective_chat.id)
    if not get_channel_settings(chat_id):
        await update.message.reply_text("Bu grup kayıtlı değil!")
        return
    keyboard = [
        [
            InlineKeyboardButton("📅 Günlük", callback_data=f"top|{chat_id}|gunluk"),
            InlineKeyboardButton("📅 Haftalık", callback_data=f"top|{chat_id}|haftalik"),
            InlineKeyboardButton("📅 Aylık", callback_data=f"top|{chat_id}|aylik"),
        ],
        [InlineKeyboardButton("📊 Bütün zamanlarda", callback_data=f"top|{chat_id}|toplam")],
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
                InlineKeyboardButton("📅 Günlük", callback_data=f"top|{chat_id}|gunluk"),
                InlineKeyboardButton("📅 Haftalık", callback_data=f"top|{chat_id}|haftalik"),
                InlineKeyboardButton("📅 Aylık", callback_data=f"top|{chat_id}|aylik"),
            ],
            [InlineKeyboardButton("📊 Bütün zamanlarda", callback_data=f"top|{chat_id}|toplam")],
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
    
    chat_id = str(update.effective_chat.id)
    if not get_channel_settings(chat_id):
        await update.message.reply_text("Bu grup kayıtlı değil!")
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

    with get_db() as conn:
        def u_msgs(since):
            if since > 0:
                r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id = ? AND user_id = ? AND sent_at >= ?", (chat_id, user_id, since)).fetchone()
            else:
                r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)).fetchone()
            return r['c'] if r else 0

        def u_type(mtype):
            r = conn.execute("SELECT COUNT(*) as c FROM message_stats WHERE chat_id = ? AND user_id = ? AND msg_type = ?", (chat_id, user_id, mtype)).fetchone()
            return r['c'] if r else 0

        rank_row = conn.execute("""
            SELECT COUNT(*) + 1 as rank FROM (
                SELECT user_id, COUNT(*) as cnt FROM message_stats WHERE chat_id = ? GROUP BY user_id
            ) WHERE cnt > (SELECT COUNT(*) FROM message_stats WHERE chat_id = ? AND user_id = ?)
        """, (chat_id, chat_id, user_id)).fetchone()
        rank = rank_row['rank'] if rank_row else '-'

    g = _get_period_start('gunluk')
    h = _get_period_start('haftalik')
    a = _get_period_start('aylik')

    text = (
        f"📊 {mention(target)} istatistikleri:\n\n"
        f"💬 Mesaj sayısı:\n"
        f"┌📆 Günlük: {u_msgs(g)}\n"
        f"├📆 Haftalık: {u_msgs(h)}\n"
        f"├📆 Aylık: {u_msgs(a)}\n"
        f"└Total: {u_msgs(0)}\n\n"
        f"📊 Etkileşim detayı:\n"
        f"┌🃏 Çıkartma: {u_type('sticker')}\n"
        f"├🀄️ Gif: {u_type('gif')}\n"
        f"├🙃 Emoji: {u_type('emoji')}\n"
        f"├📷 Fotoğraf: {u_type('photo')}\n"
        f"├🎥 Video: {u_type('video')}\n"
        f"├💾 Dosya: {u_type('document')}\n"
        f"├🎙 Ses kaydı: {u_type('voice')}\n"
        f"└📼 Müzik: {u_type('audio')}\n\n"
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
    if FOUNDER_ID not in managers:
        managers.append(FOUNDER_ID)
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
    protected = set(managers + [BOT_ID or 0])
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
                InlineKeyboardButton("Tum adminleri geri yukle", callback_data=f"lockdown_restore|{chat_id}|all"),
                InlineKeyboardButton("Spam yapan haric geri yukle", callback_data=f"lockdown_restore|{chat_id}|{spam_admin_id}"),
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

async def lockdown_restore_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    chat_id = parts[1]
    mode = parts[2]

    managers = get_channel_managers(chat_id)
    if query.from_user.id not in managers:
        await query.answer("Yetkisiz!", show_alert=True)
        return

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

    for row in msg_ids:
        try:
            await bot.delete_message(chat_id, row['msg_id'])
        except Exception as e:
            logger.debug(f"check_admin_spam: {e}")

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
        if not new.user.is_bot:
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

        if not new.user.is_bot and by_user.id not in managers and by_user.id != BOT_ID:
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
            conn.execute("DELETE FROM mod_log WHERE timestamp < ?", (week_ago,))
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
        lines = [f"📋 <b>Haftalik Kanal Log Raporu</b>\n<code>{chat_id}</code>\n"]
        for l in logs:
            dt = datetime.fromtimestamp(l['timestamp'], TZ_TR).strftime('%d.%m %H:%M')
            who = mention_html(l['user_id'], l['username'] or str(l['user_id'])) if l['user_id'] else "-"
            lines.append(f"{dt} | {html.escape(l['action'])} | {who} | {html.escape((l['detail'] or '')[:50])}")
        report = "\n".join(lines)
        await notify_managers(chat_id, report, parse_mode=ParseMode.HTML)

def _kanal_settings_keyboard(chat_id: str, cfg: dict) -> InlineKeyboardMarkup:
    def tog(val): return "✅" if val else "❌"
    action_spam = "Ban+Yetki Al" if cfg.get('admin_spam_action') == 'demote_ban' else "Sadece Yetki Al"
    action_media = "Ban+Yetki Al" if cfg.get('admin_media_action') == 'demote_ban' else "Sadece Yetki Al"
    keyboard = [
        [InlineKeyboardButton(f"{tog(cfg.get('admin_spam_enabled'))} Admin Spam Koruma", callback_data=f"kcfg|{chat_id}|admin_spam_enabled")],
        [InlineKeyboardButton(f"Spam Aksiyon: {action_spam}", callback_data=f"kcfg|{chat_id}|admin_spam_action")],
        [InlineKeyboardButton(f"{tog(cfg.get('admin_media_enabled'))} Admin Medya Flood", callback_data=f"kcfg|{chat_id}|admin_media_enabled")],
        [InlineKeyboardButton(f"Medya Aksiyon: {action_media}", callback_data=f"kcfg|{chat_id}|admin_media_action")],
        [InlineKeyboardButton(f"{tog(cfg.get('link_protection'))} Link Koruması", callback_data=f"kcfg|{chat_id}|link_protection")],
        [InlineKeyboardButton(f"{tog(cfg.get('clone_protection'))} Klonlama Koruması", callback_data=f"kcfg|{chat_id}|clone_protection")],
        [InlineKeyboardButton(f"{tog(cfg.get('bot_add_protection'))} Bot Ekleme Koruması", callback_data=f"kcfg|{chat_id}|bot_add_protection")],
        [InlineKeyboardButton(f"{tog(cfg.get('bulk_ban_protection'))} Toplu Ban Koruması", callback_data=f"kcfg|{chat_id}|bulk_ban_protection")],
        [InlineKeyboardButton("👥 Güvenli Adminler", callback_data=f"kcfg|{chat_id}|safe_admins_panel")],
        [InlineKeyboardButton("💾 Kanal Başlık/Açıklama Kaydet", callback_data=f"kcfg|{chat_id}|save_info")],
        [InlineKeyboardButton("🔙 Kapat", callback_data=f"kcfg|{chat_id}|close")],
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
    await query.answer()
    parts = query.data.split("|")
    chat_id = parts[1]
    key = parts[2]

    managers = get_channel_managers(chat_id)
    if query.from_user.id not in managers:
        await query.answer("Yetkisiz!", show_alert=True)
        return

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
            await query.answer(f"Hata: {e}", show_alert=True)
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
                buttons.append([InlineKeyboardButton(
                    f"{'✅' if is_safe else '❌'} {name}",
                    callback_data=f"kcfg|{chat_id}|safe_toggle|{a.user.id}"
                )])
            buttons.append([InlineKeyboardButton("🔙 Geri", callback_data=f"kcfg|{chat_id}|back")])
            await query.message.edit_text("Guvenli admin listesi:\n(Link atmasina izin verilenler)", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await query.answer(f"Hata: {e}", show_alert=True)
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
                buttons.append([InlineKeyboardButton(
                    f"{'✅' if is_safe else '❌'} {name}",
                    callback_data=f"kcfg|{chat_id}|safe_toggle|{a.user.id}"
                )])
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
    
    msg = update.channel_post
    if not msg:
        return

    chat_id = str(msg.chat_id)
    channel = get_channel_settings(chat_id)
    if not channel:
        return

    cmd_msg_id = msg.message_id

    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"kanal_temizle: {e}")

    status = await bot.send_message(chat_id, "Kanal temizleniyor...")

    deleted = 0
    consecutive_errors = 0
    batch = []
    i = cmd_msg_id - 1

    while i > max(1, cmd_msg_id - 50000):
        batch.append(i)
        i -= 1
        if len(batch) == 100:
            results = await asyncio.gather(
                *[bot.delete_message(chat_id, mid) for mid in batch],
                return_exceptions=True
            )
            ok = sum(1 for r in results if not isinstance(r, Exception))
            err = len(results) - ok
            deleted += ok
            consecutive_errors = 0 if ok > 0 else consecutive_errors + err
            batch = []
            if consecutive_errors > 200:
                break
            await asyncio.sleep(0.2)

    if batch:
        results = await asyncio.gather(
            *[bot.delete_message(chat_id, mid) for mid in batch],
            return_exceptions=True
        )
        deleted += sum(1 for r in results if not isinstance(r, Exception))

    try:
        await status.edit_text(f"{deleted} gönderi silindi.")
    except Exception as e:
        logger.debug(f"kanal_temizle: {e}")
    await asyncio.sleep(5)
    try:
        await status.delete()
    except Exception as e:
        logger.debug(f"kanal_temizle: {e}")
    await send_log(chat_id, f"Kanal temizlendi: {deleted} gönderi silindi")

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
        if not has_permission(chat_id, update.effective_user.id, 70):
            await reply_func("Yetkin yok!")
            return

    limit = None
    if context.args and context.args[0].isdigit():
        limit = int(context.args[0])

    await reply_func("İstekler onaylanıyor, lütfen bekle...")

    approved = 0
    failed = 0

    ub = await get_userbot()
    if ub:
        try:
            from telethon.tl.functions.messages import GetChatInviteImportersRequest
            from telethon.tl.types import ChannelParticipantsKicked
            entity = await ub.get_entity(int(chat_id))
            result = await ub(GetChatInviteImportersRequest(
                peer=entity,
                requested=True,
                offset_date=0,
                offset_user=InputPeerUser(0, 0),
                limit=limit or 200
            ))
            for u in result.importers:
                try:
                    await bot.approve_chat_join_request(chat_id, u.user_id)
                    approved += 1
                    await asyncio.sleep(0.1)
                except Exception:
                    failed += 1
        except Exception as e:
            logger.warning(f"Telethon istekonayla hata: {e}")

            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{TOKEN}/approveAllChatJoinRequests",
                        json={"chat_id": chat_id}
                    )
                result = resp.json()
                if result.get("ok"):
                    approved = -1
                else:
                    await reply_func(f"Hata: {result.get('description', 'Bilinmeyen')}")
                    return
            except Exception as e2:
                await reply_func(f"Hata: {e2}")
                return
    else:

        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TOKEN}/approveAllChatJoinRequests",
                    json={"chat_id": chat_id}
                )
            result = resp.json()
            if result.get("ok"):
                approved = -1
            else:
                await reply_func(f"Hata: {result.get('description', 'Bilinmeyen')}")
                return
        except Exception as e:
            await reply_func(f"Hata: {e}")
            return

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "UPDATE join_requests SET status = 'approved' WHERE chat_id = ? AND status = 'pending'",
                (chat_id,)
            )
            conn.commit()

    if approved == -1:
        await reply_func("Tüm bekleyen istekler onaylandı!")
    else:
        msg = f"✅ {approved} istek onaylandı."
        if failed:
            msg += f" ({failed} başarısız)"
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

def is_gbanned(user_id) -> bool:
    if _gban_cache is None:
        _load_block_caches()
    try:
        return int(user_id) in _gban_cache
    except (TypeError, ValueError):
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
    if user and chat and chat.type in ('group', 'supergroup') and is_gbanned(user.id) and update.effective_message:
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
    if getattr(member, 'can_change_info', False):
        role = 'yardimci_kurucu'
    elif getattr(member, 'can_promote_members', False):
        role = 'basadmin'
    else:
        role = 'admin'
    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO roles (chat_id, user_id, role) VALUES (?,?,?)",
                (chat_id, user_id, role)
            )
            conn.commit()
    return role

async def cmd_uvye_etiketi(update: Update, context):
    msg = update.effective_message
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    if not has_permission(chat_id, user_id, min_level=50):
        await msg.reply_text("Yetkin yok!")
        return

    target_id = None
    tag = None

    if msg.reply_to_message:
        target_id = msg.reply_to_message.from_user.id
        tag = ' '.join(context.args) if context.args else None
    elif context.args and len(context.args) >= 2:
        ref = context.args[0].lstrip('@')
        tag = ' '.join(context.args[1:])
        if ref.isdigit():
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

    with get_db() as conn:
        caller_row = conn.execute("SELECT role FROM roles WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        target_row = conn.execute("SELECT role FROM roles WHERE chat_id=? AND user_id=?", (chat_id, target_id)).fetchone()
    caller_level = ROLE_LEVELS.get(caller_row['role'] if caller_row else None, 0)
    target_level = ROLE_LEVELS.get(target_row['role'] if target_row else None, 0)

    if target_level >= caller_level and target_id != user_id:
        await msg.reply_text("Ust yetkiye etiket veremezsin!")
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
        await msg.reply_text(f"Hata: {e}")

async def _cmd_set_role(update, context, role: str):
    
    msg = update.channel_post or update.message
    if not msg:
        return

    chat_id = str(msg.chat_id)
    caller_id = update.effective_user.id if update.effective_user else None

    is_channel_post = update.channel_post is not None
    if not caller_id and not is_channel_post:
        return

    if caller_id:
        required_level = 100 if role == 'yardimci_kurucu' else 90
        if not has_permission(chat_id, caller_id, required_level) and caller_id not in get_channel_managers(chat_id):
            await msg.reply_text("Yetkin yok!")
            return

    text = msg.text or ''
    parts = text.strip().split()
    args = parts[1:] if len(parts) > 1 else (context.args or [])

    role_labels = {
        'admin': 'Admin',
        'basadmin': 'Baş Admin',
        'yardimci_kurucu': 'Yardımcı Kurucu'
    }
    label = role_labels.get(role, role)

    if msg.reply_to_message and msg.reply_to_message.from_user:
        user_id = msg.reply_to_message.from_user.id
        tag = ' '.join(args) if args else None
    elif args:
        user_ref = args[0].strip().lstrip('@')
        tag = ' '.join(args[1:]) if len(args) > 1 else None
        if user_ref.isdigit():
            user_id = int(user_ref)
        else:
            resolved_id, _ = await resolve_user(chat_id, user_ref)
            if not resolved_id:
                await msg.reply_text("Kullanici bulunamadi! ID, @username veya reply ile kullan.")
                return
            user_id = resolved_id
    else:
        await msg.reply_text(f"Kullanim: /{role.replace('_','')} @kullanici [etiket]")
        return

    tg_perms = {
        'admin': dict(can_invite_users=True, can_delete_messages=True, can_restrict_members=True, can_pin_messages=True, can_promote_members=False, can_manage_chat=True),
        'basadmin': dict(can_invite_users=True, can_delete_messages=True, can_restrict_members=True, can_pin_messages=True, can_promote_members=True, can_manage_chat=True, can_change_info=False),
        'yardimci_kurucu': dict(can_invite_users=True, can_delete_messages=True, can_restrict_members=True, can_pin_messages=True, can_promote_members=True, can_manage_chat=True, can_change_info=True),
    }

    promote_chat_id = chat_id
    if is_channel_post:
        try:
            chat_info = await context.bot.get_chat(chat_id)
            linked = getattr(chat_info, 'linked_chat_id', None)
            if linked:
                promote_chat_id = str(linked)
        except Exception as e:
            logger.debug(f"_cmd_set_role: {e}")

    try:
        await context.bot.promote_chat_member(chat_id=promote_chat_id, user_id=user_id, **tg_perms[role])
    except Exception as e:
        err = str(e).lower()
        if 'already' not in err:
            if 'chat_admin_required' in err:
                await msg.reply_text("❌ Botun yetkisi yok!")
            elif 'not enough rights' in err:
                await msg.reply_text("❌ Botun admin atama yetkisi yok!")
            elif 'user not found' in err or 'bad request' in err:
                await msg.reply_text("❌ Kullanıcı grupta bulunamadı!")
            else:
                await msg.reply_text(f"❌ Hata: {e}")
            return

    if tag:
        try:
            await context.bot.set_chat_administrator_custom_title(chat_id, user_id, tag)
        except Exception as e:
            logger.debug(f"_cmd_set_role: {e}")

    async with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO roles (chat_id, user_id, role) VALUES (?, ?, ?)",
                (chat_id, user_id, role)
            )
            if caller_id:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_given_admins (chat_id, user_id, given_by, given_at) VALUES (?,?,?,?)",
                    (chat_id, user_id, caller_id, time.time())
                )
            conn.commit()

    result = f"✅ {label} eklendi!"
    if tag:
        result += f"\nEtiket: {tag}"
    who = mention_html(user_id, str(user_id))
    try:
        target_member = await context.bot.get_chat_member(promote_chat_id, user_id)
        who = mention(target_member.user)
        keyboard = [[
            InlineKeyboardButton("⚙️ Yetkiler", callback_data=f"permtab|{promote_chat_id}|{user_id}|bot"),
            InlineKeyboardButton("❌ Kaldır", callback_data=f"removeadmin|{promote_chat_id}|{user_id}")
        ]]
        await msg.reply_text(
            f"✅ {who} {label} yapıldı!" + (f"\nEtiket: {html.escape(tag)}" if tag else ""),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        await msg.reply_text(result)
    await send_log(chat_id, f"{label} eklendi: {who}" + (f" [{html.escape(tag)}]" if tag else ""), ParseMode.HTML)

async def cmd_admin_with_title(update: Update, context):
    await _cmd_set_role(update, context, 'admin')

async def cmd_basadmin(update: Update, context):
    await _cmd_set_role(update, context, 'basadmin')

async def cmd_yardimci_kurucu(update: Update, context):
    await _cmd_set_role(update, context, 'yardimci_kurucu')

async def cmd_panel(update: Update, context):
    if update.effective_user.id != FOUNDER_ID:
        return
    if update.effective_chat.type != 'private':
        await update.message.reply_text("Bu komut sadece DM'de calisir!")
        return

    with get_db() as conn:
        total_groups = conn.execute("SELECT COUNT(*) as c FROM channels WHERE chat_type IN ('group','supergroup')").fetchone()['c']
        total_channels = conn.execute("SELECT COUNT(*) as c FROM channels WHERE chat_type='channel'").fetchone()['c']
        total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats").fetchone()['c']

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Kanallar", callback_data="panel|channels"),
            InlineKeyboardButton("👥 Gruplar", callback_data="panel|groups"),
        ],
        [
            InlineKeyboardButton("🚫 Engelliler", callback_data="panel|blocked"),
            InlineKeyboardButton("📊 İstatistikler", callback_data="panel|stats"),
        ],
    ])
    await update.message.reply_text(
        f"🤖 ANKA Security Bot Paneli\n\n"
        f"📊 İstatistikler:\n"
        f"├ Toplam Grup: {total_groups}\n"
        f"├ Toplam Kanal: {total_channels}\n"
        f"└ Toplam Kullanici: {total_users}",
        reply_markup=keyboard
    )

async def panel_callback(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != FOUNDER_ID:
        await query.answer("Yetkisiz!", show_alert=True)
        return

    data = query.data
    parts = data.split("|")
    action = parts[1] if len(parts) > 1 else ''

    if action == 'channels':
        with get_db() as conn:
            rows = conn.execute("SELECT chat_id, settings FROM channels WHERE chat_type='channel'").fetchall()
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
            rows = conn.execute("SELECT chat_id FROM channels WHERE chat_type IN ('group','supergroup')").fetchall()
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
            total_groups = conn.execute("SELECT COUNT(*) as c FROM channels WHERE chat_type IN ('group','supergroup')").fetchone()['c']
            total_channels = conn.execute("SELECT COUNT(*) as c FROM channels WHERE chat_type='channel'").fetchone()['c']
            total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats").fetchone()['c']
            total_msgs = conn.execute("SELECT COUNT(*) as c FROM message_stats").fetchone()['c']
            total_bans = conn.execute("SELECT COUNT(*) as c FROM ban_list").fetchone()['c']
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
            total_groups = conn.execute("SELECT COUNT(*) as c FROM channels WHERE chat_type IN ('group','supergroup')").fetchone()['c']
            total_channels = conn.execute("SELECT COUNT(*) as c FROM channels WHERE chat_type='channel'").fetchone()['c']
            total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM message_stats").fetchone()['c']
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 Kanallar", callback_data="panel|channels"),
                InlineKeyboardButton("👥 Gruplar", callback_data="panel|groups"),
            ],
            [
                InlineKeyboardButton("🚫 Engelliler", callback_data="panel|blocked"),
                InlineKeyboardButton("📊 İstatistikler", callback_data="panel|stats"),
            ],
        ])
        await query.message.edit_text(
            f"🤖 ANKA Security Bot Paneli\n\n"
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

async def cmd_rules(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    channel = get_channel_settings(chat_id)
    rules = channel['settings'].get('rules', '').strip()
    if not rules:
        msg = "Bu grupta henuz kural belirlenmemis."
        if has_permission(chat_id, update.effective_user.id, 70):
            msg += "\n/setrules <kurallar> ile ekleyebilirsin."
        await update.message.reply_text(msg)
        return
    try:
        chat_info = await bot.get_chat(chat_id)
        title = chat_info.title or chat_id
    except Exception:
        title = chat_id
    await update.message.reply_text(
        f"📜 {title} Kurallari\n\n{rules}\n\n"
        "Bu kurallara uymayan kullanicilar yaptiriimla karsilasabilir."
    )

async def cmd_setrules(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok!")
        return
    if not context.args:
        channel = get_channel_settings(chat_id)
        rules = channel['settings'].get('rules', '').strip()
        if rules:
            await update.message.reply_text(f"Mevcut kurallar:\n\n{rules}\n\nGuncellemek icin: /setrules <yeni kurallar>")
        else:
            await update.message.reply_text("Kullanim: /setrules <kurallar>")
        return
    rules_text = ' '.join(context.args)
    channel = get_channel_settings(chat_id)
    channel['settings']['rules'] = rules_text
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"Kurallar ayarlandi:\n\n{rules_text}")

async def cmd_setwarnlimit(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await update.message.reply_text("Once /kanal ile sec!")
        return
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok!")
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
    if not has_permission(chat_id, update.effective_user.id, 70):
        await update.message.reply_text("Yetkin yok!")
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
        await update.message.reply_text(f"Bilgi alinamadi: {e}")
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
        if user.id == FOUNDER_ID:
            await update.message.reply_text(
                "🤖 Bot Sahibi Komutlari\n\n"
                "/panel — Yonetim paneli\n"
                "/engelle <id> [sebep] — Engelle\n"
                "/engelkaldir <id> — Engel kaldir\n"
                "/gban <id|@kullanici> [sebep] — Tum gruplarda banla\n"
                "/ungban <id|@kullanici> — Global bani kaldir\n"
                "/gbanlist — Global ban listesi\n"
                "/yedek — Veritabani yedegi al\n"
                "/duyuru <mesaj> — Tum gruplara duyuru\n"
                "/kanal — Kanal sec\n"
                "/kanalsettings — Kanal ayarlari\n"
            )
        else:
            await update.message.reply_text(
                "ANKA Security Bot\n\n"
                "/start — Baslat\n"
                "/help — Yardim\n"
                "/id — ID goster\n"
                "/kanal — Kanal baglantisi\n"
                "/itiraz <aciklama> — Ban itirazi gonder\n"
            )
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
        "/rules — Grup kurallarini gor\n\n"
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
    )

    if not is_admin:
        await update.message.reply_text(user_section)
        return

    admin_section = (
        "👮 Admin Komutlari\n\n"
        "🔨 Yonetim\n"
        "/ban @kullanici [sure] [sebep] — Banla\n"
        "/unban @kullanici — Ban kaldir\n"
        "/mute @kullanici [sure] — Sustur\n"
        "/unmute @kullanici — Sustуrmayı kaldir\n"
        "/kick @kullanici — Gruptan at\n"
        "/warn @kullanici [sebep] — Uyari ver\n"
        "/unwarn @kullanici — Uyari kaldir\n"
        "/warns @kullanici — Uyarilari gor\n"
        "/temizle <sayi/all> — Mesajlari temizle\n"
        "/pin — Mesaji sabitle\n"
        "/unpin — Sabitlemeyi kaldir\n"
        "/slowmode <sn> — Yavas mod\n"
        "/banlist — Ban listesi\n"
        "/mutelist — Mute listesi\n"
        "/cekilis — Cekilis baslat\n"
        "/cekilis_bitir [kazanan sayisi] — Cekilisi bitir\n\n"
        "⚙️ Grup Yonetimi\n"
        "/settings — Grup ayarlari paneli\n"
        "/nightmod 23:00 07:00 — Gece modu\n"
        "/wordlist — Yasakli kelimeler\n"
        "/wordban <kelime> — Kelime engelle\n"
        "/setrules <kurallar> — Kural belirle\n"
        "/setwarnlimit <2-20> — Uyari limitini ayarla\n"
        "/setwarnaction ban|tempban 1d|kick|mute 2h — Limit dolunca ceza\n"
        "/setwelcome <mesaj> — Hosgeldin mesaji\n"
        "/whitelist @kullanici — Spam muaf listesi\n"
        "/istekonayla — Katilim isteklerini onayla\n"
        "/kanal — Kanal baglantisi\n"
        "/kanalsettings — Kanal ayarlari\n\n"
        "🛡 Koruma\n"
        "/antispam on/off [limit] [sn]\n"
        "/antilink on/off\n"
        "/antiforward on/off\n"
        "/antimedia on/off\n"
        "/antiraid on/off [limit] [sn]\n"
        "/antiraid_ac — Raid kilidini ac\n"
        "/captcha on/off\n"
        "/captchasure <30s-60m> — Captcha suresi\n"
        "/yeniuye <dakika|off> — Yeni uye link/medya kisiti\n"
        "/linkizin ekle|sil <alan adi> — Link muaf listesi\n\n"
        "📝 Notlar\n"
        "/save <isim> <metin> — Not kaydet (veya mesaja yanit)\n"
        "/notsil <isim> — Notu sil\n\n"
        "🏷 Etiket & Yetki\n"
        "/admin <id> [etiket] — Admin yap + etiket\n"
        "/addadmin @kullanici — Admin yap (yetki paneli ile)\n"
        "/remove @kullanici — Adminligi al\n"
        "/uyeetiketi @kullanici <etiket> — Uye etiketi ver\n\n"
        "📊 Bilgi\n"
        "/stats — Grup istatistikleri\n"
        "/grupbilgi — Detayli grup bilgisi\n"
        "/leaderboard — Siralama\n"
        "/staff — Personel listesi\n"
        "/setlog — Log kanal ayarla\n\n"
        "Sure formati: 30m, 2h, 7d"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Ayarlar Paneli", callback_data="help_settings")]
    ])
    await update.message.reply_text(
        admin_section + "\n\n━━━━━━━━━━━━━━━\n\n" + user_section,
        reply_markup=keyboard
    )

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

    if settings.get('auto_reject_bot', False) and (user.is_bot or not user.username):
        try:
            await bot.decline_chat_join_request(chat_id, user_id)
            await send_log(chat_id, f"❌ Bot/sahte reddedildi: {mention(user)} → {chat_id}", ParseMode.HTML)
        except Exception as e:
            logger.debug(f"handle_join_request: {e}")
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

BOT_PERMS = [
    ('can_ban',             '🔨 Ban komutu'),
    ('can_kick',            '👢 Kick komutu'),
    ('can_mute',            '🔇 Susturma komutu'),
    ('can_warn',            '⚠️ Uyarı komutu'),
    ('can_delete',          '🗑 Mesaj silme komutu'),
    ('can_pin',             '📌 Sabitleme komutu'),
    ('can_manage_settings', '⚙️ Ayarları yönetme'),
    ('can_manage_roles',    '👑 Rol yönetimi'),
]

TG_PERMS = [
    ('tg_manage_chat',          '📋 Genel yönetim'),
    ('tg_delete_messages',      '🗑 Mesaj silme (TG)'),
    ('tg_manage_video_chats',   '🎙 Sesli sohbet'),
    ('tg_restrict_members',     '🚫 Ban/kısıtlama'),
    ('tg_promote_members',      '⭐ Admin atama'),
    ('tg_change_info',          'ℹ️ Grup bilgisi'),
    ('tg_invite_users',         '🔗 Davet linki'),
    ('tg_pin_messages',         '📌 Mesaj sabitleme (TG)'),
    ('tg_post_stories',         '📖 Hikaye paylaşma'),
    ('tg_edit_stories',         '✏️ Hikaye düzenleme'),
    ('tg_delete_stories',       '🗑 Hikaye silme'),
    ('tg_manage_topics',        '💬 Konu yönetimi'),
]

TG_PERM_MAP = {
    'tg_manage_chat':        'can_manage_chat',
    'tg_delete_messages':    'can_delete_messages',
    'tg_manage_video_chats': 'can_manage_video_chats',
    'tg_restrict_members':   'can_restrict_members',
    'tg_promote_members':    'can_promote_members',
    'tg_change_info':        'can_change_info',
    'tg_invite_users':       'can_invite_users',
    'tg_pin_messages':       'can_pin_messages',
    'tg_post_stories':       'can_post_stories',
    'tg_edit_stories':       'can_edit_stories',
    'tg_delete_stories':     'can_delete_stories',
    'tg_manage_topics':      'can_manage_topics',
}

ALL_PERMS_LIST = [p for p, _ in BOT_PERMS] + [p for p, _ in TG_PERMS]

def _build_perm_keyboard(chat_id, target_uid, perm_row, target_role, section='bot'):
    
    keyboard = []
    perms = BOT_PERMS if section == 'bot' else TG_PERMS
    for p_key, p_name in perms:
        is_on = bool(perm_row[p_key]) if perm_row and p_key in perm_row.keys() and perm_row[p_key] is not None else False
        btn_emoji = "✅" if is_on else "❌"
        keyboard.append([InlineKeyboardButton(f"{btn_emoji} {p_name}", callback_data=f"toggleperm|{chat_id}|{target_uid}|{p_key}")])

    other = 'tg' if section == 'bot' else 'bot'
    other_label = '🤖 Bot Komutları' if section == 'tg' else '📡 Telegram Yetkileri'
    keyboard.append([InlineKeyboardButton(f"→ {other_label}", callback_data=f"permtab|{chat_id}|{target_uid}|{other}")])
    keyboard.append([
        InlineKeyboardButton("✅ Kaydet ve Çık", callback_data=f"saveandexit|{chat_id}|{target_uid}"),
        InlineKeyboardButton("❌ İptal", callback_data=f"cancelperm|{chat_id}|{target_uid}")
    ])
    return keyboard

async def toggle_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("toggleperm|"):
        return
    try:
        _, chat_id, target_uid_str, perm = query.data.split("|")
        target_uid = int(target_uid_str)
    except Exception:
        return

    caller_id = query.from_user.id
    channel = get_channel_settings(chat_id)
    if not channel or not has_permission(chat_id, caller_id, 90):
        await query.answer("Yetkisiz.", show_alert=True)
        return

    if perm not in ALL_PERMS_LIST:
        return

    _ensure_tg_perm_columns()

    with get_db() as conn:
        row = conn.execute("SELECT role FROM roles WHERE chat_id=? AND user_id=?", (chat_id, target_uid)).fetchone()
        target_role = row['role'] if row else 'admin'

        if perm.startswith('tg_'):

            perm_row = conn.execute(
                f"SELECT {', '.join(ALL_PERMS_LIST)} FROM role_permissions WHERE chat_id=? AND role=?",
                (chat_id, target_role)
            ).fetchone()
            current_val = bool(perm_row[perm]) if perm_row and perm_row[perm] is not None else False
            new_val = not current_val
            conn.execute(
                f"INSERT INTO role_permissions (chat_id, role, {perm}) VALUES (?,?,?) "
                f"ON CONFLICT(chat_id, role) DO UPDATE SET {perm}=excluded.{perm}",
                (chat_id, target_role, int(new_val))
            )
        else:

            user_perm_row = conn.execute(
                f"SELECT {perm} FROM user_permissions WHERE chat_id=? AND user_id=?",
                (chat_id, target_uid)
            ).fetchone()
            if user_perm_row and user_perm_row[perm] is not None:
                current_val = bool(user_perm_row[perm])
            else:

                role_row = conn.execute(
                    f"SELECT {perm} FROM role_permissions WHERE chat_id=? AND role=?",
                    (chat_id, target_role)
                ).fetchone()
                current_val = bool(role_row[perm]) if role_row and role_row[perm] is not None else True
            new_val = not current_val
            conn.execute(
                f"INSERT INTO user_permissions (chat_id, user_id, {perm}) VALUES (?,?,?) "
                f"ON CONFLICT(chat_id, user_id) DO UPDATE SET {perm}=excluded.{perm}",
                (chat_id, target_uid, int(new_val))
            )

        conn.commit()

        perm_row2 = conn.execute(
            f"SELECT {', '.join(ALL_PERMS_LIST)} FROM role_permissions WHERE chat_id=? AND role=?",
            (chat_id, target_role)
        ).fetchone()
        user_overrides = conn.execute(
            f"SELECT {', '.join(ALL_PERMS_LIST)} FROM user_permissions WHERE chat_id=? AND user_id=?",
            (chat_id, target_uid)
        ).fetchone()

    if perm in TG_PERM_MAP:
        try:
            tg_kwargs = {TG_PERM_MAP[perm]: new_val}
            await context.bot.promote_chat_member(chat_id=chat_id, user_id=target_uid, **tg_kwargs)
        except Exception as e:
            logger.error(f"TG promote hata: {e}")

    await query.answer(f"{'✅' if new_val else '❌'} Değiştirildi", show_alert=False)

    merged_row = dict(perm_row2) if perm_row2 else {}
    if user_overrides:
        for col in ALL_PERMS_LIST:
            if not col.startswith('tg_') and user_overrides[col] is not None:
                merged_row[col] = user_overrides[col]

    section = 'tg' if perm.startswith('tg_') else 'bot'
    keyboard = _build_perm_keyboard(chat_id, target_uid, merged_row, target_role, section)
    section_label = '📡 Telegram Yetkileri' if section == 'tg' else '🤖 Bot Komutları'
    try:
        target_member = await bot.get_chat_member(chat_id, target_uid)
        await query.edit_message_text(
            text=f"Yetki düzenleme: {mention(target_member.user)} ({target_role})\n{section_label}\n\n⚡ Bot komutları bu kullanıcıya özel.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Panel yenileme hata: {e}")

async def permtab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id, target_uid_str, section = query.data.split("|")
        target_uid = int(target_uid_str)
    except Exception:
        return

    caller_id = query.from_user.id
    channel = get_channel_settings(chat_id)
    if not channel or channel['owner'] != caller_id:
        await query.answer("Yetkisiz.", show_alert=True)
        return

    _ensure_tg_perm_columns()
    with get_db() as conn:
        row = conn.execute("SELECT role FROM roles WHERE chat_id=? AND user_id=?", (chat_id, target_uid)).fetchone()
        target_role = row['role'] if row else 'admin'
        perm_row = conn.execute(
            f"SELECT {', '.join(ALL_PERMS_LIST)} FROM role_permissions WHERE chat_id=? AND role=?",
            (chat_id, target_role)
        ).fetchone()
        user_overrides = conn.execute(
            f"SELECT {', '.join(ALL_PERMS_LIST)} FROM user_permissions WHERE chat_id=? AND user_id=?",
            (chat_id, target_uid)
        ).fetchone()

    merged_row = dict(perm_row) if perm_row else {}
    if user_overrides:
        for col in ALL_PERMS_LIST:
            if not col.startswith('tg_') and user_overrides[col] is not None:
                merged_row[col] = user_overrides[col]

    keyboard = _build_perm_keyboard(chat_id, target_uid, merged_row, target_role, section)
    section_label = '📡 Telegram Yetkileri' if section == 'tg' else '🤖 Bot Komutları'
    try:
        target_member = await bot.get_chat_member(chat_id, target_uid)
        await query.edit_message_text(
            text=f"Yetki düzenleme: {mention(target_member.user)} ({target_role})\n{section_label}\n\n⚡ Bot komutları bu kullanıcıya özel.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"permtab hata: {e}")

def _ensure_tg_perm_columns():
    
    tg_cols = list(TG_PERM_MAP.keys())
    with get_db() as conn:
        existing = [row[1] for row in conn.execute("PRAGMA table_info(role_permissions)").fetchall()]
        for col in tg_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE role_permissions ADD COLUMN {col} INTEGER DEFAULT 0")
        conn.commit()

async def cancelperm_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("cancelperm|"):
        return
    parts = query.data.split("|")
    chat_id, target_uid_str = parts[1], parts[2]
    target_uid = int(target_uid_str)
    key = f"perm_edit_{chat_id}_{target_uid}"
    if key in context.bot_data:
        del context.bot_data[key]
    await query.edit_message_text("❌ Değişiklikler iptal edildi.")

async def saveandexit_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("saveandexit|"):
        return
    parts = query.data.split("|")
    chat_id, target_uid_str = parts[1], parts[2]
    caller_id = query.from_user.id
    channel = get_channel_settings(chat_id)
    if not channel or channel['owner'] != caller_id:
        await query.answer("Yetkisiz.", show_alert=True)
        return

    await query.edit_message_text("✅ Değişiklikler kaydedildi.")

async def locked_callback(update: Update, context):
    query = update.callback_query
    await query.answer("Baş admin için bu yetki düzenlenemez (kilitli).", show_alert=True)

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

    if not args or not args[0].startswith("aup_"):
        await update.message.reply_text(
            "Merhaba! Kanal/grup koruma botuna hos geldin.\n"
            "/help ile komutlari gorebilirsin."
        )
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

    caller_id = update.effective_user.id
    channel = get_channel_settings(chat_id)
    if not channel or channel['owner'] != caller_id:
        await update.message.reply_text("Bu paneli sadece kanal/grup sahibi açabilir.")
        return

    _ensure_tg_perm_columns()
    with get_db() as conn:
        row = conn.execute("SELECT role FROM roles WHERE chat_id = ? AND user_id = ?", (chat_id, target_uid)).fetchone()
        if not row:
            await update.message.reply_text("Bu kullanıcı uygun rolde değil.")
            return
        target_role = row['role']
        perm_row = conn.execute(
            f"SELECT {', '.join(ALL_PERMS_LIST)} FROM role_permissions WHERE chat_id=? AND role=?",
            (chat_id, target_role)
        ).fetchone()

    keyboard = _build_perm_keyboard(chat_id, target_uid, perm_row, target_role, 'bot')
    try:
        target_member = await bot.get_chat_member(chat_id, target_uid)
        await update.message.reply_text(
            f"Yetki düzenleme: {mention(target_member.user)} ({target_role})\n🤖 Bot Komutları",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text("Panel açılamadı, tekrar deneyin.")

async def kanal(update: Update, context):
    user_id = update.effective_user.id

    if update.effective_chat and update.effective_chat.type in ['group', 'supergroup', 'channel']:
        chat_id = str(update.effective_chat.id)
        existing = get_channel_settings(chat_id)
        if not existing:
            await _register_chat(chat_id, user_id, update.effective_chat.type)
            await update.message.reply_text(
                "Grup kaydedildi! Artık /kanal ile secebilirsin.\n"
                f"Grup ID: {chat_id}"
            )
        else:
            await update.message.reply_text(
                f"Bu grup kayitli (ID: {chat_id})\n"
                "DM'de /kanal yazarak yonetim paneline gec."
            )
        return

    owned = []
    with get_db() as conn:
        all_chats = conn.execute(
            "SELECT chat_id, chat_type, owner_id FROM channels"
        ).fetchall()
        for row in all_chats:
            cid = row['chat_id']
            ctype = row['chat_type'] or 'group'
            is_owner = (row['owner_id'] == user_id)
            has_role = conn.execute(
                "SELECT 1 FROM roles WHERE chat_id = ? AND user_id = ?", (cid, user_id)
            ).fetchone()
            is_founder = (user_id == FOUNDER_ID)
            if is_owner or has_role or is_founder:
                owned.append((cid, ctype))

    if not owned:
        await update.message.reply_text(
            "Hicbir kanal/grupta yetkin yok!\n\n"
            "Once botu gruba/kanala ekle ve o gruptan /kanal yaz.\n"
            f"Senin ID'n: {user_id}"
        )
        return

    keyboard = []
    for cid, ctype in owned:
        icon = "📢" if ctype == "channel" else "👥"
        try:
            chat_info = await bot.get_chat(cid)
            label = f"{icon} {chat_info.title or cid}"
        except Exception:
            label = f"{icon} {cid}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"select_{cid}")])

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
    if update.message.from_user.id != channel['owner']:
        await update.message.reply_text("Sadece owner kullanabilir!")
        return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await update.message.reply_text(f"Mevcut log: {channel.get('log_chat_id', 'Ayarlanmamış')}\nKullanım: /setlog -1001234567890")
        return
    new_log_id = context.args[0]
    channel['log_chat_id'] = new_log_id
    save_channel_settings(chat_id, channel)
    await update.message.reply_text(f"✅ Log kanalı güncellendi: {new_log_id}")

async def staff(update: Update, context):
    chat_id = context.user_data.get('selected_channel') or str(update.effective_chat.id)
    channel = get_channel_settings(chat_id)
    if not channel:
        await update.message.reply_text("Kanal bulunamadı!")
        return

    role_order = ['kurucu', 'yardimci_kurucu', 'basadmin', 'admin']
    role_config = {
        'kurucu':          ('👑', 'Kurucu'),
        'yardimci_kurucu': ('⚜️', 'Yardımcı Kurucu'),
        'basadmin':        ('🌟', 'Baş Admin'),
        'admin':           ('👮🏼', 'Admin'),
    }

    with get_db() as conn:
        db_roles = conn.execute(
            "SELECT user_id, role FROM roles WHERE chat_id=?", (chat_id,)
        ).fetchall()

    db_role_map = {r['user_id']: r['role'] for r in db_roles}

    try:
        tg_admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        await update.message.reply_text(f"Admin listesi alınamadı: {e}")
        return

    grouped = {r: [] for r in role_order}
    seen = set()

    for member in tg_admins:
        u = member.user
        if u.is_bot:
            continue
        uid = u.id
        seen.add(uid)
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

async def yetkiler(update: Update, context):
    if update.effective_chat.type != 'private':
        await update.message.reply_text("Bu komut sadece DM'de kullanılır.")
        return
    chat_id = context.user_data.get('selected_channel')
    if not chat_id:
        await update.message.reply_text("Önce /kanal ile seç!")
        return
    channel = get_channel_settings(chat_id)
    if not channel:
        await update.message.reply_text("Kanal bulunamadı!")
        return
    if update.effective_user.id != channel['owner']:
        await update.message.reply_text("Sadece owner yönetebilir!")
        return
    with get_db() as conn:
        admins = conn.execute("SELECT user_id FROM roles WHERE chat_id = ? AND role = 'admin'", (chat_id,)).fetchall()
    if not admins:
        await update.message.reply_text("Bu kanalda henüz admin yok.")
        return
    msg = "👑 <b>Yetki Yönetimi Paneli</b>\n\n"
    keyboard = []
    for row in admins:
        admin_id = row['user_id']
        try:
            member = await bot.get_chat_member(chat_id, admin_id)
            label = f"@{member.user.username}" if member.user.username else (member.user.first_name or str(admin_id))
            msg += f"• {mention(member.user)}\n"
            keyboard.append([InlineKeyboardButton(f"{label} - Düzenle", callback_data=f"yetki_{chat_id}_{admin_id}")])
        except Exception:
            msg += f"• ID: <code>{admin_id}</code>\n"
    await update.message.reply_text(msg + "\nAdmin seç:", reply_markup=InlineKeyboardMarkup(keyboard),
                                    parse_mode=ParseMode.HTML)

async def yetki_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not (data.startswith('yetki_set|') or data.startswith('yetkiler|') or data.startswith('yetki_')):
        return

    if data.startswith('yetki_set|'):
        parts = data.split('|')
        chat_id = parts[1]
        admin_id = int(parts[2])
        perm = parts[3] if len(parts) > 3 else None
    elif data.startswith('yetkiler|'):
        parts = data.split('|')
        chat_id = parts[1]
        admin_id = None
        perm = None
    else:
        parts = data.split('_')
        if len(parts) < 3:
            return
        chat_id = parts[1]
        admin_id = int(parts[2]) if len(parts) > 2 else None
        perm = None
    keyboard = [
        [InlineKeyboardButton("📝 Mesaj Gönderme", callback_data=f"yetki_set|{chat_id}|{admin_id}|can_post")],
        [InlineKeyboardButton("🗑️ Mesaj Silme", callback_data=f"yetki_set|{chat_id}|{admin_id}|can_delete")],
        [InlineKeyboardButton("🚫 Üye Kısıtlama", callback_data=f"yetki_set|{chat_id}|{admin_id}|can_restrict")],
        [InlineKeyboardButton("📌 Pin Atma", callback_data=f"yetki_set|{chat_id}|{admin_id}|can_pin")],
        [InlineKeyboardButton("🔙 Geri", callback_data=f"yetkiler|{chat_id}")]
    ]
    await query.message.edit_text(
        f"Yetkileri düzenleniyor: Admin ID {admin_id}\nAçık/Kapalı yapmak için butona bas:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
            return int(ref), reason
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
    if update.effective_user.id != FOUNDER_ID:
        return
    target_id, reason = await _resolve_target_id(update, context)
    if not target_id:
        await update.effective_message.reply_text("Kullanım: /gban <id|@kullanıcı> [sebep] (veya mesaja yanıt)")
        return
    if target_id == FOUNDER_ID:
        await update.effective_message.reply_text("Kendini banlayamazsın.")
        return
    async with _db_lock:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO global_bans (user_id, reason, banned_by, banned_at) VALUES (?, ?, ?, ?)",
                         (target_id, reason, update.effective_user.id, time.time()))
            conn.commit()
    invalidate_block_caches()
    status = await update.effective_message.reply_text(f"🌐 {target_id} global banlanıyor...")
    with get_db() as conn:
        chats = [r['chat_id'] for r in conn.execute(
            "SELECT chat_id FROM channels WHERE chat_type IN ('group','supergroup','channel')").fetchall()]
    ok = fail = 0
    for cid in chats:
        try:
            await bot.ban_chat_member(cid, target_id)
            ok += 1
        except Exception:
            fail += 1
    await status.edit_text(f"🌐 {target_id} global banlandı.\n✅ {ok} sohbet | ❌ {fail} (yetki yok/üye değil)\nSebep: {reason or '-'}")

async def cmd_ungban(update: Update, context):
    if update.effective_user.id != FOUNDER_ID:
        return
    target_id, _ = await _resolve_target_id(update, context)
    if not target_id:
        await update.effective_message.reply_text("Kullanım: /ungban <id|@kullanıcı>")
        return
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM global_bans WHERE user_id = ?", (target_id,))
            conn.commit()
    invalidate_block_caches()
    with get_db() as conn:
        chats = [r['chat_id'] for r in conn.execute("SELECT chat_id FROM channels").fetchall()]
    for cid in chats:
        try:
            await bot.unban_chat_member(cid, target_id, only_if_banned=True)
        except Exception:
            pass
    await update.effective_message.reply_text(f"✅ {target_id} global banı kaldırıldı.")

async def cmd_gbanlist(update: Update, context):
    if update.effective_user.id != FOUNDER_ID:
        return
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, reason, banned_at FROM global_bans ORDER BY banned_at DESC LIMIT 50").fetchall()
    if not rows:
        await update.effective_message.reply_text("Global ban listesi boş.")
        return
    lines = ["🌐 Global Ban Listesi (son 50):"]
    for r in rows:
        lines.append(f"• {r['user_id']} — {r['reason'] or '-'} ({datetime.fromtimestamp(r['banned_at'], TZ_TR):%d.%m.%Y})")
    await update.effective_message.reply_text("\n".join(lines))

# ── Ayar komutları ──
async def _require_settings_admin(update: Update, context, level: int = 70):
    chat_id = _get_effective_chat_id(update, context)
    channel = get_channel_settings(chat_id) if chat_id else None
    if not channel:
        await update.effective_message.reply_text("Önce /kanal ile seç!")
        return None, None
    if not has_permission(chat_id, update.effective_user.id, level):
        await update.effective_message.reply_text("Yetkin yok!")
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
        InlineKeyboardButton("✅ Banı kaldır", callback_data=f"appeal|ok|{appeal_id}"),
        InlineKeyboardButton("❌ Reddet", callback_data=f"appeal|no|{appeal_id}"),
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
    if not has_permission(chat_id, query.from_user.id, 70) and query.from_user.id not in get_channel_managers(chat_id):
        await query.answer("Yetkin yok!", show_alert=True)
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

# ── Notlar (/save, /not, #isim) ──
_NOTE_NAME = re.compile(r'^[\w\-]{1,32}$')

async def cmd_save_note(update: Update, context):
    """/save isim metin  veya bir mesaja yanıt olarak /save isim"""
    msg = update.effective_message
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not get_channel_settings(chat_id):
        await msg.reply_text("Önce /kanal ile seç!")
        return
    if not has_permission(chat_id, update.effective_user.id, 50):
        await msg.reply_text("Yetkin yok!")
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
    try:
        if row['file_id']:
            sender = {
                'photo': reply_to.reply_photo, 'video': reply_to.reply_video, 'animation': reply_to.reply_animation,
                'document': reply_to.reply_document, 'voice': reply_to.reply_voice, 'audio': reply_to.reply_audio,
            }.get(row['file_type'])
            if row['file_type'] == 'sticker':
                await reply_to.reply_sticker(row['file_id'])
            elif sender:
                await sender(row['file_id'], caption=row['content'], parse_mode=ParseMode.HTML)
        else:
            await reply_to.reply_text(row['content'], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest:
        await reply_to.reply_text(html.unescape(re.sub(r'<[^>]+>', '', row['content'] or '')))
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
    await update.effective_message.reply_text("📝 Notlar:\n" + "\n".join(f"• #{r['name']}" for r in rows) +
                                              "\n\nGörmek için #isim yaz.")

async def cmd_delete_note(update: Update, context):
    chat_id = _get_effective_chat_id(update, context)
    if not chat_id or not has_permission(chat_id, update.effective_user.id, 50):
        await update.effective_message.reply_text("Yetkin yok!")
        return
    if not context.args:
        await update.effective_message.reply_text("Kullanım: /notsil <isim>")
        return
    async with _db_lock:
        with get_db() as conn:
            n = conn.execute("DELETE FROM notes WHERE chat_id = ? AND name = ?", (chat_id, context.args[0].lower())).rowcount
            conn.commit()
    await update.effective_message.reply_text("✅ Not silindi." if n else "Böyle bir not yok.")

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

# ── Periyodik temizlik ──
async def db_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    async with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM flood_history WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM media_flood_history WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM forward_history WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM raid_joins WHERE timestamp < ?", (now - 86400,))
            conn.execute("DELETE FROM spam_incidents WHERE incident_at < ?", (now - 30 * 86400,))
            conn.execute("DELETE FROM newcomers WHERE joined_at < ?", (now - 2 * 86400,))
            conn.execute("DELETE FROM appeals WHERE created_at < ? AND status != 'pending'", (now - 90 * 86400,))
            conn.commit()

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
            await context.bot.send_message(FOUNDER_ID, f"⚠️ Bot hatası\n{where}<pre>{html.escape(tb)}</pre>",
                                           parse_mode=ParseMode.HTML)
        except Exception:
            pass

async def chat_member_cache_handler(update: Update, context):
    """Admin atama/alma olduğunda admin önbelleğini temizler."""
    if update.chat_member:
        invalidate_admin_cache(str(update.chat_member.chat.id))


async def post_init(application):
    global BOT_ID, userbot
    BOT_ID = application.bot.id

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

def main():
    global bot
    app = (
        Application.builder()
        .token(TOKEN)
        .rate_limiter(AIORateLimiter(max_retries=3))
        .post_init(post_init)
        .build()
    )
    bot = app.bot  # tüm modül tek (rate limiter'lı) bot nesnesini kullanır

    # Her güncellemeden önce: engelli sohbet/kullanıcı ve global ban kontrolü
    app.add_handler(TypeHandler(Update, blocklist_guard), group=-100)
    app.add_error_handler(error_handler)

    app.add_handler(ChatMemberHandler(chat_member_cache_handler, ChatMemberHandler.CHAT_MEMBER), group=-1)
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_chat_member_protection, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler('kanalsettings', cmd_kanal_settings))
    app.add_handler(CallbackQueryHandler(kanal_cfg_callback, pattern=r'^kcfg'))
    app.add_handler(CallbackQueryHandler(lockdown_restore_callback, pattern=r'^lockdown_restore'))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & ~filters.COMMAND, handle_channel_post_protection), group=1)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, kanal_admin_spam_check), group=2)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_bot_added), group=0)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler), group=1)
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, newbie_guard_handler), group=-11)
    app.add_handler(MessageHandler(filters.ALL, anti_forward_handler), group=-10)
    app.add_handler(MessageHandler(filters.ALL, anti_media_flood_handler), group=-9)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anti_spam_flood_handler), group=-8)
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, anti_link_handler), group=-7)
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, check_message), group=-6)
    app.add_handler(MessageHandler(filters.Regex(r'^#[\w\-]+') & filters.ChatType.GROUPS, hashtag_note_handler), group=5)

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('id', id_command))
    app.add_handler(CommandHandler('kanal', kanal, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('setlog', setlog, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('staff', staff))
    app.add_handler(CommandHandler('yetkiler', yetkiler, filters=filters.ChatType.PRIVATE))

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
    app.add_handler(CallbackQueryHandler(top_callback, pattern=r'^top'))

    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & filters.ChatType.GROUPS,
        track_message
    ), group=99)
    app.add_handler(MessageHandler(
        filters.COMMAND & filters.ChatType.GROUPS,
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
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r'^panel\|'))

    app.add_handler(CallbackQueryHandler(button_callback, pattern='^select_'))
    app.add_handler(CallbackQueryHandler(giveaway_button, pattern=r'^giveaway\|'))
    app.add_handler(CallbackQueryHandler(remove_admin_callback, pattern=r'^removeadmin\|'))
    app.add_handler(CallbackQueryHandler(toggle_permission_callback, pattern=r'^toggleperm\|'))
    app.add_handler(CallbackQueryHandler(permtab_callback, pattern=r'^permtab\|'))
    app.add_handler(CallbackQueryHandler(saveandexit_callback, pattern=r'^saveandexit\|'))
    app.add_handler(CallbackQueryHandler(cancelperm_callback, pattern=r'^cancelperm\|'))
    app.add_handler(CallbackQueryHandler(locked_callback, pattern='^locked$'))
    app.add_handler(CallbackQueryHandler(yetki_callback, pattern=r'^(yetki_set\||yetkiler\||yetki_)'))
    app.add_handler(CallbackQueryHandler(captcha_callback, pattern=r'^captcha\|'))
    app.add_handler(CallbackQueryHandler(settings_toggle_callback, pattern=r'^(stg_|nm_settings\|)'))
    app.add_handler(CallbackQueryHandler(nightmod_callback, pattern='^nm_'))
    app.add_handler(CallbackQueryHandler(wordlist_callback, pattern='^wdel'))
    app.add_handler(CallbackQueryHandler(appeal_callback, pattern=r'^appeal(pick)?\|'))
    app.add_handler(CallbackQueryHandler(help_settings_callback, pattern=r'^help_settings$'))

    job_queue = app.job_queue
    job_queue.run_repeating(check_captcha_timeouts, interval=15, first=15)
    job_queue.run_repeating(check_raid_locks, interval=60, first=10)
    job_queue.run_repeating(check_nightmod, interval=60, first=30)
    job_queue.run_repeating(check_temp_bans, interval=300, first=60)
    job_queue.run_repeating(check_expired_mutes, interval=300, first=120)
    job_queue.run_repeating(db_cleanup_job, interval=3600, first=600)
    job_queue.run_repeating(spam_memory_cleanup, interval=3600, first=3600)
    job_queue.run_repeating(weekly_log_cleanup, interval=86400, first=3600)
    job_queue.run_daily(backup_job, time=dtime(4, 0, tzinfo=TZ_TR))
    job_queue.run_daily(run_daily_scheduler, time=dtime(0, 0, tzinfo=TZ_TR))

    logger.info("✅ Bot çalışıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
