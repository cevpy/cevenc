#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ULUS Haber Botu
===============
Türk haber sitelerinden canlı haber toplayıp kullanıcılara (özelden), kanallara ve gruplara gönderen bot.

• Türk haber sitelerinin kendi RSS kaynakları (80+ akış): TRT Haber, Anadolu Ajansı, NTV, Hürriyet, Sözcü,
  Habertürk, CNN Türk, Milliyet, Sabah, Cumhuriyet, BBC Türkçe, DW Türkçe, Euronews, Bloomberg HT ve dahası.
• Aynı haber farklı sitelerden gelirse tek haber olarak birleşir; kaç kaynağın verdiği görünür.
• Son dakika tespiti, gündem/trend konuları, anahtar kelime takibi, sabah/akşam bülteni.
• Butonlu ayar paneli; kanal/grup ekleme; satır içi (inline) paylaşım.

Çalıştırma: .env dosyasını doldur →  python main.py
"""

import os, re, html, json, time, math, asyncio, logging, sqlite3, calendar, unicodedata, secrets
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import httpx
import feedparser
from dotenv import load_dotenv
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton,
                      ForceReply, LinkPreviewOptions,
                      CopyTextButton, InlineQueryResultArticle, InputTextMessageContent, BotCommand,
                      BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeChat)
from telegram.constants import ParseMode, KeyboardButtonStyle, ChatMemberStatus
from telegram.error import Forbidden, BadRequest, TimedOut, NetworkError, RetryAfter
from telegram.ext import (Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
                          InlineQueryHandler, ChatMemberHandler, ContextTypes, AIORateLimiter, ApplicationHandlerStop, filters)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

def _env(key: str, default: str = "") -> str:
    v = os.getenv(key, "")
    return v.strip() if v and v.strip() else default

def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default

BOT_TOKEN = _env("BOT_TOKEN")
FOUNDER_ID = _env_int("FOUNDER_ID", 0)
DB_FILE = _env("DB_FILE", "haber_bot.db")
if not os.path.isabs(DB_FILE):
    DB_FILE = os.path.join(BASE_DIR, DB_FILE)
FETCH_INTERVAL = max(60, _env_int("FETCH_INTERVAL", 300))  # normal kaynakların yenilenme aralığı (sn)
KEEP_DAYS = max(2, _env_int("KEEP_DAYS", 7))
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    level=getattr(logging, LOG_LEVEL, logging.INFO))
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("ulus_haber")

TR = timezone(timedelta(hours=3))  # Türkiye saati (yaz saati uygulaması yok)
GREEN = KeyboardButtonStyle.SUCCESS
RED = KeyboardButtonStyle.DANGER
BLUE = KeyboardButtonStyle.PRIMARY
BOT_ID = 0
BOT_USERNAME = ""

# ───────────────────────────── Kategoriler ve kaynaklar ─────────────────────────────

# anahtar: (etiket, emoji)
CATEGORIES = {
    'gundem':    ("Gündem", "📰"),
    'ekonomi':   ("Ekonomi", "💰"),
    'dunya':     ("Dünya", "🌍"),
    'spor':      ("Spor", "⚽"),
    'teknoloji': ("Teknoloji", "💻"),
    'saglik':    ("Sağlık", "🩺"),
    'bilim':     ("Bilim", "🔬"),
    'magazin':   ("Magazin", "🎬"),
    'kultur':    ("Kültür-Sanat", "🎭"),
}
CAT_ORDER = list(CATEGORIES)

def cat_label(cat: str, emoji: bool = True) -> str:
    if cat not in CATEGORIES:
        return "Haber"
    label, em = CATEGORIES[cat]
    return f"{em} {label}" if emoji else label

def cat_emoji(cats) -> str:
    for c in (cats if isinstance(cats, (list, tuple, set)) else [cats]):
        if c in CATEGORIES:
            return CATEGORIES[c][1]
    return "🗞"

# (kaynak adı, kategori, adres, son dakika akışı mı)
DIRECT_FEEDS = [
    ("TRT Haber", 'gundem', "https://www.trthaber.com/manset_articles.rss", False),
    ("TRT Haber", 'gundem', "https://www.trthaber.com/sondakika_articles.rss", True),
    ("TRT Haber", 'ekonomi', "https://www.trthaber.com/ekonomi_articles.rss", False),
    ("TRT Haber", 'spor', "https://www.trthaber.com/spor_articles.rss", False),
    ("TRT Haber", 'dunya', "https://www.trthaber.com/dunya_articles.rss", False),
    ("TRT Haber", 'saglik', "https://www.trthaber.com/saglik_articles.rss", False),
    ("TRT Haber", 'bilim', "https://www.trthaber.com/bilim_teknoloji_articles.rss", False),
    ("TRT Haber", 'kultur', "https://www.trthaber.com/kultur_sanat_articles.rss", False),
    ("Anadolu Ajansı", 'gundem', "https://www.aa.com.tr/tr/rss/default?cat=guncel", False),
    ("Anadolu Ajansı", 'ekonomi', "https://www.aa.com.tr/tr/rss/default?cat=ekonomi", False),
    ("Anadolu Ajansı", 'spor', "https://www.aa.com.tr/tr/rss/default?cat=spor", False),
    ("Anadolu Ajansı", 'dunya', "https://www.aa.com.tr/tr/rss/default?cat=dunya", False),
    ("Anadolu Ajansı", 'teknoloji', "https://www.aa.com.tr/tr/rss/default?cat=bilim-teknoloji", False),
    ("Anadolu Ajansı", 'saglik', "https://www.aa.com.tr/tr/rss/default?cat=saglik", False),
    ("Anadolu Ajansı", 'kultur', "https://www.aa.com.tr/tr/rss/default?cat=kultur", False),
    ("NTV", 'gundem', "https://www.ntv.com.tr/son-dakika.rss", True),
    ("NTV", 'gundem', "https://www.ntv.com.tr/turkiye.rss", False),
    ("NTV", 'ekonomi', "https://www.ntv.com.tr/ekonomi.rss", False),
    ("NTV", 'spor', "https://www.ntv.com.tr/sporskor.rss", False),
    ("NTV", 'dunya', "https://www.ntv.com.tr/dunya.rss", False),
    ("NTV", 'teknoloji', "https://www.ntv.com.tr/teknoloji.rss", False),
    ("NTV", 'saglik', "https://www.ntv.com.tr/saglik.rss", False),
    ("Hürriyet", 'gundem', "https://www.hurriyet.com.tr/rss/gundem", False),
    ("Hürriyet", 'ekonomi', "https://www.hurriyet.com.tr/rss/ekonomi", False),
    ("Hürriyet", 'spor', "https://www.hurriyet.com.tr/rss/spor", False),
    ("Hürriyet", 'dunya', "https://www.hurriyet.com.tr/rss/dunya", False),
    ("Hürriyet", 'teknoloji', "https://www.hurriyet.com.tr/rss/teknoloji", False),
    ("Hürriyet", 'magazin', "https://www.hurriyet.com.tr/rss/magazin", False),
    ("Sözcü", 'gundem', "https://www.sozcu.com.tr/feeds-son-dakika", True),
    ("Sözcü", 'gundem', "https://www.sozcu.com.tr/feeds-rss-category-gundem", False),
    ("Sözcü", 'ekonomi', "https://www.sozcu.com.tr/feeds-rss-category-ekonomi", False),
    ("Sözcü", 'spor', "https://www.sozcu.com.tr/feeds-rss-category-spor", False),
    ("Sözcü", 'dunya', "https://www.sozcu.com.tr/feeds-rss-category-dunya", False),
    ("Habertürk", 'gundem', "https://www.haberturk.com/rss", False),
    ("Habertürk", 'ekonomi', "https://www.haberturk.com/rss/ekonomi.xml", False),
    ("Habertürk", 'spor', "https://www.haberturk.com/rss/spor.xml", False),
    ("Habertürk", 'dunya', "https://www.haberturk.com/rss/dunya.xml", False),
    ("Habertürk", 'saglik', "https://www.haberturk.com/rss/saglik.xml", False),
    ("Habertürk", 'magazin', "https://www.haberturk.com/rss/magazin.xml", False),
    ("CNN Türk", 'gundem', "https://www.cnnturk.com/feed/rss/turkiye/news", False),
    ("CNN Türk", 'ekonomi', "https://www.cnnturk.com/feed/rss/ekonomi/news", False),
    ("CNN Türk", 'spor', "https://www.cnnturk.com/feed/rss/spor/news", False),
    ("CNN Türk", 'dunya', "https://www.cnnturk.com/feed/rss/dunya/news", False),
    ("CNN Türk", 'saglik', "https://www.cnnturk.com/feed/rss/saglik/news", False),
    ("CNN Türk", 'magazin', "https://www.cnnturk.com/feed/rss/magazin/news", False),
    ("Milliyet", 'gundem', "https://www.milliyet.com.tr/rss/rssnew/gundemrss.xml", False),
    ("Milliyet", 'ekonomi', "https://www.milliyet.com.tr/rss/rssnew/ekonomi.xml", False),
    ("Sabah", 'gundem', "https://www.sabah.com.tr/rss/gundem.xml", False),
    ("Sabah", 'ekonomi', "https://www.sabah.com.tr/rss/ekonomi.xml", False),
    ("Sabah", 'spor', "https://www.sabah.com.tr/rss/spor.xml", False),
    ("Sabah", 'dunya', "https://www.sabah.com.tr/rss/dunya.xml", False),
    ("Sabah", 'saglik', "https://www.sabah.com.tr/rss/saglik.xml", False),
    ("Cumhuriyet", 'gundem', "https://www.cumhuriyet.com.tr/rss/son_dakika.xml", True),
    ("Yeni Şafak", 'gundem', "https://www.yenisafak.com/rss?xml=gundem", False),
    ("BirGün", 'gundem', "https://www.birgun.net/rss/home", False),
    ("Evrensel", 'gundem', "https://www.evrensel.net/rss/haber.xml", False),
    ("Halk TV", 'gundem', "https://halktv.com.tr/service/rss.php", False),
    ("Independent Türkçe", 'gundem', "https://www.indyturk.com/rss.xml", False),
    ("Mynet", 'gundem', "https://www.mynet.com/haber/rss/sondakika", True),
    ("En Son Haber", 'gundem', "https://www.ensonhaber.com/rss/ensonhaber.xml", False),
    ("Akşam", 'gundem', "https://www.aksam.com.tr/rss/rss.asp", False),
    ("BBC Türkçe", 'dunya', "https://feeds.bbci.co.uk/turkce/rss.xml", False),
    ("DW Türkçe", 'dunya', "https://rss.dw.com/rdf/rss-tur-all", False),
    ("Euronews", 'dunya', "https://tr.euronews.com/rss", False),
    ("Bloomberg HT", 'ekonomi', "https://www.bloomberght.com/rss", False),
    ("Dünya", 'ekonomi', "https://www.dunya.com/rss", False),
    ("Ekonomim", 'ekonomi', "https://www.ekonomim.com/export/rss", False),
    ("Fotomaç", 'spor', "https://www.fotomac.com.tr/rss/anasayfa.xml", False),
    ("Sözcü", 'saglik', "https://www.sozcu.com.tr/feeds-rss-category-saglik", False),
    ("Sözcü", 'magazin', "https://www.sozcu.com.tr/feeds-rss-category-magazin", False),
    ("Sözcü", 'bilim', "https://www.sozcu.com.tr/feeds-rss-category-bilim-teknoloji", False),
    ("Milliyet", 'dunya', "https://www.milliyet.com.tr/rss/rssnew/dunyarss.xml", False),
    ("Milliyet", 'teknoloji', "https://www.milliyet.com.tr/rss/rssnew/teknolojirss.xml", False),
    ("Milliyet", 'magazin', "https://www.milliyet.com.tr/rss/rssnew/magazinrss.xml", False),
    ("Sabah", 'magazin', "https://www.sabah.com.tr/rss/magazin.xml", False),
    ("Sabah", 'kultur', "https://www.sabah.com.tr/rss/kultur-sanat.xml", False),
    ("Habertürk", 'teknoloji', "https://www.haberturk.com/rss/teknoloji.xml", False),
    ("Habertürk", 'kultur', "https://www.haberturk.com/rss/kultur-sanat.xml", False),
    ("Evrim Ağacı", 'bilim', "https://www.evrimagaci.org/rss.xml", False),
    ("Webtekno", 'teknoloji', "https://www.webtekno.com/rss.xml", False),
    ("ShiftDelete", 'teknoloji', "https://shiftdelete.net/feed", False),
    ("DonanımHaber", 'teknoloji', "https://www.donanimhaber.com/rss/tum/", False),
]
class Feed:
    __slots__ = ('fid', 'source', 'cat', 'url', 'breaking', 'interval',
                 'etag', 'modified', 'next_at', 'errors', 'last_ok', 'last_error', 'seeded', 'count')

    def __init__(self, source, cat, url, breaking=False, interval=None):
        self.source, self.cat, self.url, self.breaking = source, cat, url, breaking
        self.fid = url
        self.interval = interval or (120 if breaking else FETCH_INTERVAL)
        self.etag = self.modified = None
        self.next_at = 0.0
        self.errors = 0
        self.last_ok = 0.0
        self.last_error = ""
        self.seeded = False   # ilk çekimde eski haberler "yeni" sayılmaz
        self.count = 0


def build_feeds() -> list[Feed]:
    return [Feed(s, c, u, b) for s, c, u, b in DIRECT_FEEDS]

# ───────────────────────────── Metin araçları ─────────────────────────────

_FOLD = str.maketrans("çğıöşüâîûÇĞİIÖŞÜÂÎÛ", "cgiosuaiucgiiosuaiu")
STOPWORDS = set("""
ve veya ile ama fakat ancak ki de da mi mı mu mü bu şu o bir iki üç için gibi kadar daha en çok az her hiç
ne neden nasıl nerede ise olan olarak oldu olduğu olacak etti eden ettiği yaptı yapan yapıldı dedi diyor
açıklama açıkladı son dakika sondakika flaş haber haberi haberleri video foto galeri canlı izle günü bugün
yarın dün sonra önce karşı göre yeni ilk büyük tüm şok önemli işte peki artık bile hem ya yine diye den dan
saat kaçta hangi kanal kanalda zaman izle full hd şifresiz şifreli nedir kimdir oldu olur verildi alındı edildi yapıldı
geldi açıklandı açıklaması belli sürüyor devam ediyor hakkında ilişkin yönelik üzerine tv rehberi başlıyor bölüm bölümü fragman fragmanı yayınlandı
the and of to in for on with at by from
""".split())
AD_MARKERS = ("(ilandır)", "(reklam)", "reklam içeriği", "sponsorlu", "advertorial", "(i̇landır)")


def tr_lower(s: str) -> str:
    return (s or "").replace("I", "ı").replace("İ", "i").lower()

def fold(s: str) -> str:
    """Türkçe harfleri sadeleştirip küçük harfe çevirir (karşılaştırma ve arama için)."""
    s = unicodedata.normalize("NFC", tr_lower(s)).translate(_FOLD)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

_WORD = re.compile(r"[a-z0-9]+")
_STOP_F = None

def _stop() -> set:
    global _STOP_F
    if _STOP_F is None:
        _STOP_F = {fold(w) for w in STOPWORDS} | {fold(w) for w in TR_MONTHS + TR_DAYS}
    return _STOP_F

def tokens(title: str) -> set[str]:
    """Başlığın anlamlı kelime kökleri (ilk 5 harf; Türkçede basit ama etkili kök bulma). Sayılar atlanır."""
    t = fold(re.sub(r"(?<=\w)[’'`´]\w*", " ", title or ""))  # İran'ın → İran (açılış tırnağı kelimeyi silmez)
    stop = _stop()
    return {w[:5] for w in _WORD.findall(t) if w not in stop and len(w) >= 2 and not w.isdigit()}

def name_tokens(title: str) -> set[str]:
    """Özel isim kökleri: cümle ortasında büyük harfle başlayan ya da kesme işareti alan kelimeler
    (Fenerbahçe'ye, SPK'dan). Başlığın çoğu Büyük Harfle Yazılmışsa ayırt edilemez → boş küme."""
    words = [w.strip(".,:;!?\"“”‘()[]«»…|-–") for w in (title or "").split()]
    words = [w for w in words if w]
    if not words:
        return set()
    caps = sum(1 for w in words if w[:1].isupper())
    if caps > 0.6 * len(words) and len(words) >= 4:
        return set()
    out = set()
    for i, w in enumerate(words):
        has_apos = bool(re.search(r"\w[’'`´]\w", w))
        if w[:1].isupper() and (i > 0 or has_apos or (w.isupper() and 2 <= len(w) <= 5)):
            out |= tokens(w)
    return out

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>", " ", s)
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", s))).strip()

def shorten(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-–") + "…"

def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)

def clean_link(url: str) -> str:
    """utm_* gibi izleme parametrelerini ve #parçayı atar (aynı haberin iki kez girmesini önler)."""
    try:
        p = urlsplit((url or "").strip())
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))]
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))
    except ValueError:
        return url or ""

def now() -> float:
    return time.time()

def tr_time(ts: float, fmt: str = "%H:%M") -> str:
    return datetime.fromtimestamp(ts, TR).strftime(fmt)

TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]

def tr_date(ts: float) -> str:
    d = datetime.fromtimestamp(ts, TR)
    return f"{d.day} {TR_MONTHS[d.month - 1]} {TR_DAYS[d.weekday()]}"

def ago(ts: float) -> str:
    d = max(0, now() - ts)
    if d < 60:
        return "az önce"
    if d < 3600:
        return f"{int(d // 60)} dk önce"
    if d < 86400:
        return f"{int(d // 3600)} sa önce"
    return tr_time(ts, "%d.%m %H:%M")

def ibtn(text: str, data: str | None = None, style=None, url: str | None = None, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, url=url, style=style, **kw)

def toggle_btn(label: str, on: bool, data: str) -> InlineKeyboardButton:
    return ibtn(f"{'✅' if on else '⬜'} {label}", data, GREEN if on else None)

# ───────────────────────────── Veritabanı ─────────────────────────────

@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, summary TEXT, link TEXT, image TEXT, source TEXT,
            cats TEXT NOT NULL DEFAULT ',',       -- ",gundem,ekonomi,"
            sources TEXT NOT NULL DEFAULT '[]',   -- JSON: kaynak adları
            source_count INTEGER NOT NULL DEFAULT 1,
            tokens TEXT NOT NULL DEFAULT '',
            ftitle TEXT NOT NULL DEFAULT '',      -- aramada kullanılan sade başlık
            published REAL, first_seen REAL NOT NULL, updated REAL NOT NULL,
            breaking_at REAL, from_breaking_feed INTEGER NOT NULL DEFAULT 0, fresh INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_st_updated ON stories(updated);
        CREATE INDEX IF NOT EXISTS idx_st_first ON stories(first_seen);
        CREATE INDEX IF NOT EXISTS idx_st_breaking ON stories(breaking_at);
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id INTEGER NOT NULL, source TEXT, cat TEXT, title TEXT, link TEXT UNIQUE,
            summary TEXT, image TEXT, published REAL, fetched REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_items_story ON items(story_id);
        CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched);
        CREATE TABLE IF NOT EXISTS subs (
            chat_id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,                    -- private | channel | group
            owner_id INTEGER, title TEXT, settings TEXT NOT NULL DEFAULT '{}',
            cursor REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, paused INTEGER NOT NULL DEFAULT 0,
            thread_id INTEGER, last_digest TEXT, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sent (
            chat_id INTEGER NOT NULL, story_id INTEGER NOT NULL, at REAL NOT NULL, kind TEXT,
            PRIMARY KEY (chat_id, story_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sent_at ON sent(at);
        CREATE TABLE IF NOT EXISTS feed_state (
            fid TEXT PRIMARY KEY, etag TEXT, modified TEXT, last_ok REAL, errors INTEGER, last_error TEXT, count INTEGER
        );
        """)

# ───────────────────────────── Haber birleştirme (aynı haber, farklı kaynak) ─────────────────────────────

INDEX_WINDOW = 18 * 3600

class StoryIndex:
    """Son 18 saatteki haberlerin başlık kökleri. Yeni başlık, bir haberin kayıtlı başlıklarından biriyle
    yeterince örtüşürse aynı haber sayılır. Karşılaştırma başlık başlık yapılır (zincirleme birleşme olmaz)
    ve nadir kelimeler (özel isimler) sık kelimelerden (operasyon, maç, İstanbul) daha ağır basar."""

    MAX_MEMBERS = 8

    def __init__(self):
        self.members: dict[int, list[tuple[frozenset, frozenset]]] = {}
        self.seen: dict[int, float] = {}
        self.inv: dict[str, set[int]] = {}

    def add(self, sid: int, title: str, first_seen: float):
        self.members[sid] = []
        self.seen[sid] = first_seen
        self.extend(sid, title)

    def extend(self, sid: int, title: str):
        toks, names = frozenset(tokens(title)), frozenset(name_tokens(title))
        mem = self.members.get(sid)
        if mem is None or not toks or any(m[0] == toks for m in mem) or len(mem) >= self.MAX_MEMBERS:
            return
        mem.append((toks, names))
        for t in toks:
            self.inv.setdefault(t, set()).add(sid)

    def prune(self):
        limit = now() - INDEX_WINDOW
        for sid in [s for s, ts in self.seen.items() if ts < limit]:
            for t in set().union(*(m[0] for m in self.members.pop(sid, []))):
                ids = self.inv.get(t)
                if ids:
                    ids.discard(sid)
                    if not ids:
                        del self.inv[t]
            self.seen.pop(sid, None)

    def _idf(self, t: str) -> float:
        return math.log(1 + (len(self.members) + 1) / (1 + len(self.inv.get(t, ()))))

    def similar(self, a: frozenset, b: frozenset, na: frozenset = frozenset(), nb: frozenset = frozenset()) -> bool:
        inter = a & b
        if len(inter) < 2:
            return False
        # İki başlıkta da diğerinde geçmeyen özel isim var ve ortak isim yoksa farklı haberdir
        # ("Boksör Emrah Yaşar altın aldı" ≠ "Boksör Hatice Akbaş altın aldı")
        if na and nb and not (na & nb) and (na - b) and (nb - a):
            return False
        wi = sum(self._idf(t) for t in inter)
        wa, wb = sum(self._idf(t) for t in a), sum(self._idf(t) for t in b)
        wjac = wi / (wa + wb - wi)
        wov = wi / min(wa, wb)
        small = min(len(a), len(b))
        if len(inter) >= 3:
            return wjac >= 0.42 or (wov >= 0.72 and len(inter) >= 4)
        return small <= 3 and wjac >= 0.6

    def find(self, title: str) -> int | None:
        a, na = frozenset(tokens(title)), frozenset(name_tokens(title))
        if len(a) < 2:
            return None
        hits = Counter()
        for t in a:
            for sid in self.inv.get(t, ()):
                hits[sid] += 1
        for sid, inter in hits.most_common(30):
            if inter < 2:
                break
            if any(self.similar(a, m, na, mn) for m, mn in self.members[sid]):
                return sid
        return None

    def load(self):
        with db() as c:
            rows = c.execute("SELECT s.id, s.first_seen, i.title FROM stories s JOIN items i ON i.story_id = s.id "
                             "WHERE s.first_seen > ? ORDER BY s.id, i.id", (now() - INDEX_WINDOW,)).fetchall()
        for r in rows:
            if r['id'] not in self.members:
                self.add(r['id'], r['title'], r['first_seen'])
            else:
                self.extend(r['id'], r['title'])


index = StoryIndex()

# ───────────────────────────── RSS çekme ─────────────────────────────

FEEDS: list[Feed] = []
_fetch_lock = asyncio.Lock()
_http: httpx.AsyncClient | None = None
UA = "Mozilla/5.0 (compatible; UlusHaberBot/1.0; +https://t.me)"
AD_FOLDED = tuple(fold(x) for x in AD_MARKERS)
MAX_AGE = 48 * 3600          # bundan eski haber hiç alınmaz
FRESH_AGE = 3 * 3600         # anlık gönderim için haber en fazla bu kadar eski olabilir

def http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        # trust_env: PythonAnywhere gibi ortamlarda HTTP(S)_PROXY otomatik kullanılır
        _http = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=True,
                                  headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
                                  limits=httpx.Limits(max_connections=12, max_keepalive_connections=6))
    return _http

def _entry_time(e) -> float | None:
    for k in ('published_parsed', 'updated_parsed'):
        v = e.get(k)
        if v:
            try:
                return float(calendar.timegm(v))
            except (TypeError, ValueError, OverflowError):
                pass
    return None

_IMG_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)

def _entry_image(e) -> str | None:
    for key in ('media_content', 'media_thumbnail'):
        for m in e.get(key) or ():
            u = m.get('url')
            if u and (m.get('medium') in (None, 'image') and 'video' not in (m.get('type') or '')):
                return u
    for l in e.get('links') or ():
        if 'image' in (l.get('type') or '') and l.get('href'):
            return l['href']
    for e2 in e.get('enclosures') or ():
        if 'image' in (e2.get('type') or '') and e2.get('href'):
            return e2['href']
    blobs = [e.get('summary') or ''] + [c.get('value') or '' for c in e.get('content') or ()]
    for b in blobs:
        m = _IMG_SRC.search(b)
        if m:
            return html.unescape(m.group(1))
    return None

def parse_entry(feed: Feed, e) -> dict | None:
    """RSS girdisini sade bir sözlüğe çevirir; reklam/boş/çok eski girdiler için None."""
    title = strip_html(e.get('title') or '')
    link = clean_link(e.get('link') or '')
    if not title or not link.startswith("http"):
        return None
    source = feed.source
    raw = e.get('summary') or ''
    if not raw and e.get('content'):
        raw = e['content'][0].get('value') or ''
    summary = strip_html(raw)
    if fold(summary).startswith(fold(title)):
        summary = summary[len(title):].lstrip(" :.-–")
    if any(m in fold(title + " " + summary) for m in AD_FOLDED):
        return None
    published = _entry_time(e)
    if published:
        if published > now() + 600:
            published = now()
        if now() - published > MAX_AGE:
            return None
    return {'title': shorten(title, 300), 'link': link, 'source': source, 'summary': shorten(summary, 700),
            'image': _entry_image(e), 'published': published}

def _is_fresh(published: float | None) -> bool:
    return published is None or now() - published <= FRESH_AGE

def _check_breaking(c, sid: int):
    r = c.execute("SELECT source_count, published, first_seen, breaking_at, from_breaking_feed, fresh FROM stories WHERE id = ?",
                  (sid,)).fetchone()
    if not r or r['breaking_at'] or not r['fresh']:
        return
    age = now() - (r['published'] or r['first_seen'])
    n = r['source_count']
    if (n >= 4 and age <= 90 * 60) or (r['from_breaking_feed'] and n >= 3 and age <= 60 * 60):
        c.execute("UPDATE stories SET breaking_at = ? WHERE id = ?", (now(), sid))

def ingest(feed: Feed, entries: list[dict], seeding: bool) -> int:
    """Girdileri veritabanına işler; yeni ya da yeni kaynak eklenen haber sayısını döndürür."""
    changed = 0
    with db() as c:
        for it in entries:
            changed += _ingest_one(c, feed, it, seeding)
    return changed

def _ingest_one(c, feed: Feed, it: dict, seeding: bool) -> int:
    if c.execute("SELECT 1 FROM items WHERE link = ?", (it['link'],)).fetchone():
        return 0
    toks = tokens(it['title'])
    fresh_item = (not seeding) and _is_fresh(it['published'])
    sid = index.find(it['title'])
    ts = now()
    if sid:
        st = c.execute("SELECT * FROM stories WHERE id = ?", (sid,)).fetchone()
        if not st:
            sid = None
    if sid:
        sources = json.loads(st['sources'])
        new_source = it['source'] not in sources
        if new_source:
            sources.append(it['source'])
        cats = st['cats'] if f",{feed.cat}," in st['cats'] or not feed.cat else st['cats'] + f"{feed.cat},"
        upd = {'sources': json.dumps(sources, ensure_ascii=False), 'source_count': len(sources), 'cats': cats}
        if new_source:
            upd['updated'] = ts
        if fresh_item and not st['fresh']:
            upd['fresh'] = 1
        # İlk kaynakta görsel yoksa görseli olan kaynağın başlığı/bağlantısı/görseli kullanılır
        if it['image'] and not st['image']:
            upd.update(title=it['title'], link=it['link'], source=it['source'])
            if it['summary']:
                upd['summary'] = it['summary']
            if it['image']:
                upd['image'] = it['image']
        elif it['summary'] and not st['summary']:
            upd['summary'] = it['summary']
        if feed.breaking and not st['from_breaking_feed']:
            upd['from_breaking_feed'] = 1
        if it['published'] and (not st['published'] or it['published'] < st['published']):
            upd['published'] = it['published']
        c.execute(f"UPDATE stories SET {', '.join(k + ' = ?' for k in upd)} WHERE id = ?", (*upd.values(), sid))
        index.extend(sid, it['title'])
        changed = 1 if new_source else 0
    else:
        cur = c.execute(
            "INSERT INTO stories (title, summary, link, image, source, cats, sources, source_count, tokens, ftitle, "
            "published, first_seen, updated, from_breaking_feed, fresh) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (it['title'], it['summary'], it['link'], it['image'], it['source'], f",{feed.cat}," if feed.cat else ",",
             json.dumps([it['source']], ensure_ascii=False), " ".join(sorted(toks)), fold(it['title'] + " " + it['summary'][:200]),
             it['published'], ts, ts, 1 if feed.breaking else 0, 1 if fresh_item else 0))
        sid = cur.lastrowid
        index.add(sid, it['title'], ts)
        changed = 1
    c.execute("INSERT OR IGNORE INTO items (story_id, source, cat, title, link, summary, image, published, fetched) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (sid, it['source'], feed.cat, it['title'], it['link'], it['summary'][:300], it['image'], it['published'], ts,
               ))
    if changed:
        _check_breaking(c, sid)
    return changed


async def fetch_feed(feed: Feed, sem: asyncio.Semaphore) -> int:
    async with sem:
        headers = {}
        if feed.etag:
            headers['If-None-Match'] = feed.etag
        if feed.modified:
            headers['If-Modified-Since'] = feed.modified
        try:
            r = await http().get(feed.url, headers=headers)
            if r.status_code == 304:
                _feed_ok(feed)
                return 0
            r.raise_for_status()
            parsed = await asyncio.to_thread(feedparser.parse, r.content)
            if not parsed.entries and parsed.bozo:
                raise ValueError(f"RSS okunamadı: {type(parsed.bozo_exception).__name__}")
        except (httpx.HTTPError, ValueError, OSError) as e:
            feed.errors += 1
            feed.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            feed.next_at = now() + min(3600, feed.interval * (2 ** min(feed.errors, 5)))
            logger.debug("Kaynak hatası %s: %s", feed.url, feed.last_error)
            return 0
        feed.etag = r.headers.get('etag')
        feed.modified = r.headers.get('last-modified')
        entries = [p for p in (parse_entry(feed, e) for e in parsed.entries[:100]) if p]
        entries.sort(key=lambda p: p['published'] or 0)   # eskiden yeniye: ilk gelen haberin başlığı korunur
        seeding = not feed.seeded
        try:
            changed = ingest(feed, entries, seeding)
        except sqlite3.Error:
            logger.exception("Veritabanı hatası (%s)", feed.url)
            return 0
        feed.seeded = True
        feed.count += len(entries)
        _feed_ok(feed)
        return changed

def _feed_ok(feed: Feed):
    feed.errors, feed.last_error, feed.last_ok = 0, "", now()
    feed.next_at = now() + feed.interval

def save_feed_states():
    with db() as c:
        c.executemany("INSERT OR REPLACE INTO feed_state (fid, etag, modified, last_ok, errors, last_error, count) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      [(f.fid, f.etag, f.modified, f.last_ok, f.errors, f.last_error, f.count)
                       for f in FEEDS])

def load_feed_states(feeds: list[Feed]):
    with db() as c:
        rows = {r['fid']: r for r in c.execute("SELECT * FROM feed_state").fetchall()}
    for f in feeds:
        r = rows.get(f.fid)
        if r and r['last_ok']:
            f.seeded = True            # daha önce çekilmiş: yeni gelenler gerçekten yeni
            f.count = r['count'] or 0
            f.last_ok = r['last_ok']

def setup_feeds():
    global FEEDS
    FEEDS = build_feeds()
    load_feed_states(FEEDS)
    logger.info("%d haber akışı yüklendi", len(FEEDS))

async def fetch_cycle() -> int:
    """Vakti gelen tüm akışları çeker. Aynı anda iki tur çalışmaz."""
    if _fetch_lock.locked():
        return 0
    async with _fetch_lock:
        due = [f for f in FEEDS if f.next_at <= now()]
        if not due:
            return 0
        sem = asyncio.Semaphore(6)
        results = await asyncio.gather(*(fetch_feed(f, sem) for f in due), return_exceptions=True)
        changed = 0
        for f, res in zip(due, results):
            if isinstance(res, Exception):
                logger.warning("Akış işlenemedi %s: %r", f.url, res)
                f.next_at = now() + f.interval
            else:
                changed += res
        index.prune()
        save_feed_states()
        return changed

# ───────────────────────────── Haber sorguları ─────────────────────────────

def story_row(sid: int):
    with db() as c:
        return c.execute("SELECT * FROM stories WHERE id = ?", (sid,)).fetchone()

def story_items(sid: int) -> list:
    with db() as c:
        return c.execute("SELECT source, title, link, published, fetched FROM items WHERE story_id = ? "
                         "GROUP BY source ORDER BY COALESCE(published, fetched)", (sid,)).fetchall()

def latest_stories(cat: str | None = None, limit: int = 40, offset: int = 0, hours: float = 24) -> list:
    """cat: None → hepsi, 'top' → çok kaynaklı öne çıkanlar, 'bd' → son dakika, diğerleri kategori."""
    since = now() - hours * 3600
    with db() as c:
        if cat == 'top':
            return c.execute("SELECT * FROM stories WHERE updated > ? AND first_seen > ? ORDER BY "
                             "source_count * 1.0 / (1 + (? - first_seen) / 21600.0) DESC LIMIT ? OFFSET ?",
                             (since, now() - 12 * 3600, now(), limit, offset)).fetchall()
        if cat == 'bd':
            return c.execute("SELECT * FROM stories WHERE breaking_at > ? ORDER BY breaking_at DESC LIMIT ? OFFSET ?",
                             (now() - 48 * 3600, limit, offset)).fetchall()
        if cat:
            return c.execute("SELECT * FROM stories WHERE first_seen > ? AND cats LIKE ? "
                             "ORDER BY COALESCE(published, first_seen) DESC LIMIT ? OFFSET ?",
                             (since, f"%,{cat},%", limit, offset)).fetchall()
        return c.execute("SELECT * FROM stories WHERE first_seen > ? AND cats != ',' "
                         "ORDER BY COALESCE(published, first_seen) DESC LIMIT ? OFFSET ?",
                         (since, limit, offset)).fetchall()

def search_stories(query: str, limit: int = 40, days: float = KEEP_DAYS) -> list:
    words = [w for w in _WORD.findall(fold(query)) if len(w) >= 2][:6]
    if not words:
        return []
    where = " AND ".join("ftitle LIKE ?" for _ in words)
    with db() as c:
        return c.execute(f"SELECT * FROM stories WHERE first_seen > ? AND {where} "
                         "ORDER BY COALESCE(published, first_seen) DESC LIMIT ?",
                         (now() - days * 86400, *[f"%{w}%" for w in words], limit)).fetchall()

# ───────────────────────────── Gündem / trend konular ─────────────────────────────

TRENDS: list[tuple[str, int]] = []
_TREND_SKIP = {fold(x) for x in TR_MONTHS + TR_DAYS + ["Son", "Dakika", "Flaş", "Canlı", "Video", "Galeri", "Bakan",
                                                       "Başkan", "Cumhurbaşkanı", "Türkiye", "TL", "Euro", "Dolar"]}
_CAP_WORD = re.compile(r"^[A-ZÇĞİÖŞÜ][a-zçğıöşüâîû]+$|^[A-ZÇĞİÖŞÜ]{2,5}$")

def title_phrases(title: str) -> set[str]:
    """Başlıktaki özel isim gruplarını çıkarır: 'Ali Tarakçı', 'Fenerbahçe', 'NATO'."""
    words = [re.sub(r"[’'`´].*$", "", w.strip(".,:;!?\"“”‘’'`()[]«»…|-–")) for w in (title or "").split()]
    if sum(1 for w in words if w.isupper() and len(w) > 3) > len(words) / 2:
        return set()   # TAMAMI BÜYÜK HARF başlık: özel isim ayırt edilemez
    out, run = set(), []
    for i, w in enumerate(words + [""]):
        if w and _CAP_WORD.match(w) and fold(w) not in _TREND_SKIP and fold(w) not in {fold(s) for s in STOPWORDS}:
            run.append((i, w))
            continue
        if run:
            if not (len(run) == 1 and run[0][0] == 0):   # tek kelimelik cümle başı büyük harfi sayılmaz
                out.add(" ".join(x for _, x in run[:3]))
            run = []
    return out

def compute_trends(limit: int = 12, window_h: float = 3) -> list[tuple[str, int]]:
    """Son {window_h} saatte öne çıkan özel isimler; önceki saatlerde zaten çok geçenler bastırılır.
    Gece gibi sakin saatlerde yeterli konu çıkmazsa pencere 6 saate genişler."""
    t = now()
    with db() as c:
        rows = c.execute("SELECT title, source_count, COALESCE(published, first_seen) AS ts FROM stories "
                         "WHERE COALESCE(published, first_seen) > ?", (t - 24 * 3600,)).fetchall()
    recent, prior, shown = Counter(), Counter(), {}
    for r in rows:
        for p in title_phrases(r['title']):
            key = fold(p)
            shown.setdefault(key, p)
            if r['ts'] > t - window_h * 3600:
                recent[key] += min(r['source_count'], 6)
            else:
                prior[key] += min(r['source_count'], 6)
    scored = []
    for key, w in recent.items():
        if w < 3:
            continue
        scored.append((w / math.sqrt(1 + prior[key] / 7), key, w))
    scored.sort(reverse=True)
    out = []
    for _, key, w in scored:
        # "Tarakçı" ile "Ali Tarakçı" gibi iç içe olanlardan birini tut
        if any(key in k2 or k2 in key for k2, _ in ((fold(p), 0) for p, _ in out)):
            continue
        out.append((shown[key], w))
        if len(out) >= limit:
            break
    if len(out) < 5 and window_h < 6:
        return compute_trends(limit, 6)
    return out

# ───────────────────────────── Abonelikler (kullanıcı / kanal / grup) ─────────────────────────────

DIGEST_PRESETS = ["07:00", "08:00", "09:00", "12:00", "15:00", "18:00", "19:00", "21:00", "23:00"]
QUIET_PRESETS = [None, [23, 7], [0, 8], [22, 8]]
MAX_KEYWORDS = 20
MAX_DIGESTS = 4
BREAKING_DAILY_CAP = 10

def default_settings(kind: str) -> dict:
    base = {'cats': ['gundem', 'ekonomi', 'dunya', 'spor'], 'src_off': [], 'kw': [], 'instant': False,
            'digest': ['09:00', '19:00'], 'breaking': 'all', 'quiet': None, 'per_hour': 6, 'fmt': 'card',
            'min_src': 1, 'buttons': True, 'sign': '', 'onboarded': False}
    if kind == 'channel':
        base.update(cats=['gundem', 'dunya', 'ekonomi'], instant=True, digest=[], breaking='cats', onboarded=True)
    elif kind == 'group':
        base.update(instant=False, breaking='off', per_hour=4, onboarded=True)
    return base

def _sub_from_row(r) -> dict:
    d = dict(r)
    s = default_settings(d['kind'])
    try:
        s.update(json.loads(d['settings'] or '{}'))
    except (TypeError, ValueError):
        pass
    d['settings'] = s
    return d

def get_sub(chat_id: int) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM subs WHERE chat_id = ?", (int(chat_id),)).fetchone()
    return _sub_from_row(r) if r else None

def all_subs(active_only: bool = True) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM subs" + (" WHERE active = 1" if active_only else "")).fetchall()
    return [_sub_from_row(r) for r in rows]

def ensure_sub(chat_id: int, kind: str, owner_id: int | None = None, title: str | None = None) -> tuple[dict, bool]:
    """Abonelik yoksa oluşturur (varsayılan ayarlarla); pasifse yeniden etkinleştirir. (abonelik, yeni_mi)"""
    chat_id = int(chat_id)
    sub = get_sub(chat_id)
    with db() as c:
        if sub is None:
            c.execute("INSERT INTO subs (chat_id, kind, owner_id, title, settings, cursor, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (chat_id, kind, owner_id, title, json.dumps(default_settings(kind), ensure_ascii=False), now(), now()))
            created = True
        else:
            c.execute("UPDATE subs SET active = 1, title = COALESCE(?, title), owner_id = COALESCE(owner_id, ?), "
                      "cursor = CASE WHEN active = 0 THEN ? ELSE cursor END WHERE chat_id = ?",
                      (title, owner_id, now(), chat_id))
            created = False
    return get_sub(chat_id), created

def save_settings(chat_id: int, settings: dict):
    with db() as c:
        c.execute("UPDATE subs SET settings = ? WHERE chat_id = ?", (json.dumps(settings, ensure_ascii=False), int(chat_id)))

def set_sub(chat_id: int, **fields):
    with db() as c:
        c.execute(f"UPDATE subs SET {', '.join(k + ' = ?' for k in fields)} WHERE chat_id = ?", (*fields.values(), int(chat_id)))

def owned_subs(user_id: int) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM subs WHERE owner_id = ? AND kind != 'private' ORDER BY created", (user_id,)).fetchall()
    return [_sub_from_row(r) for r in rows]

# ───────────────────────────── Eşleştirme ─────────────────────────────

def story_cats(st) -> list[str]:
    return [c for c in (st['cats'] or '').strip(',').split(',') if c]

def story_sources(st) -> list[str]:
    try:
        return json.loads(st['sources'] or '[]')
    except ValueError:
        return []

def kw_match(kw_folded: str, text_folded: str) -> bool:
    """Kelime başından eşleşir: 'dolar' → 'dolara', 'dolar kuru' ✓; 'dolarlık' ✓, 'sandolar' ✗."""
    return bool(kw_folded) and re.search(r"(?<![a-z0-9])" + re.escape(kw_folded), text_folded) is not None

def source_allowed(st, s: dict) -> bool:
    off = s.get('src_off') or []
    return not off or any(x not in off for x in story_sources(st))

def match_reason(st, s: dict) -> str | None:
    if not source_allowed(st, s):
        return None
    text = st['ftitle'] or fold(st['title'])
    for kw in s.get('kw') or []:
        if kw_match(fold(kw), text):
            return f"kw:{kw}"
    if st['source_count'] < s.get('min_src', 1):
        return None
    if set(story_cats(st)) & set(s.get('cats') or []):
        return 'cat'
    return None

def in_quiet(s: dict, ts: float | None = None) -> bool:
    q = s.get('quiet')
    if not q:
        return False
    h = datetime.fromtimestamp(ts or now(), TR).hour
    start, end = q
    return start <= h < end if start < end else (h >= start or h < end)

# ───────────────────────────── Haber mesajı biçimi ─────────────────────────────

def story_meta(st) -> str:
    n = st['source_count']
    extra = f" +{n - 1} kaynak" if n > 1 else ""
    return f"📰 {esc(st['source'])}{extra} · 🕒 {tr_time(st['published'] or st['first_seen'])}"

def reason_header(reason: str | None) -> str:
    if reason == 'breaking':
        return "🔴 <b>SON DAKİKA</b>\n"
    if reason and reason.startswith('kw:'):
        return f"🔔 <b>Takip:</b> {esc(reason[3:])}\n"
    return ""

def story_text(st, reason: str | None = None, sign: str = "", compact: bool = False, limit: int = 4096) -> str:
    em = cat_emoji(story_cats(st))
    head = reason_header(reason)
    tail = f"\n{esc(sign)}" if sign else ""
    if compact:
        n = st['source_count']
        return (f"{head}{em} <a href=\"{esc(st['link'])}\">{esc(st['title'])}</a>\n"
                f"<i>{esc(st['source'])}{f' +{n - 1}' if n > 1 else ''} · {tr_time(st['published'] or st['first_seen'])}</i>{tail}")
    base = f"{head}{em} <b>{esc(st['title'])}</b>"
    meta = f"\n\n{story_meta(st)}{tail}"
    summary, body = st['summary'] or "", ""
    room = limit - len(base) - len(meta) - 4
    n = min(400, room)
    while summary and n >= 60:
        body = esc(shorten(summary, n))
        if len(body) <= room:
            break
        body, n = "", int(n * 0.8)
    return base + (f"\n\n{body}" if body else "") + meta

def story_markup(st, kind: str, s: dict, reason: str | None = None, back: str | None = None) -> InlineKeyboardMarkup | None:
    if kind != 'private' and not s.get('buttons', True):
        return None
    row = [ibtn("📖 Haberi oku", url=st['link'], style=BLUE)]
    if st['source_count'] > 1:
        row.append(ibtn(f"📚 {st['source_count']} kaynak", f"k|{st['id']}"))
    rows = [row]
    if kind == 'private':
        extra = [ibtn("📤 Paylaş", switch_inline_query=f"#{st['id']}")]
        if reason and reason.startswith('kw:'):
            extra.append(ibtn("🔔 Takip listem", "me|k"))
        if back:
            extra.append(ibtn("⬅️ Listeye dön", back))
        rows.append(extra)
    return InlineKeyboardMarkup(rows)

def preview_opts(st, compact: bool) -> LinkPreviewOptions:
    if compact:
        return LinkPreviewOptions(is_disabled=True)
    return LinkPreviewOptions(url=st['link'], prefer_large_media=True)

def bulk_text(stories: list, title: str = "Yeni haberler", limit: int = 4000) -> str:
    out = f"🗞 <b>{esc(title)}</b> ({len(stories)})\n"
    for st in stories:
        n = st['source_count']
        line = (f"\n{cat_emoji(story_cats(st))} <a href=\"{esc(st['link'])}\">{esc(shorten(st['title'], 140))}</a> "
                f"<i>— {esc(st['source'])}{f' +{n - 1}' if n > 1 else ''}</i>")
        if len(out) + len(line) > limit:
            break
        out += line
    return out

# ───────────────────────────── Gönderim ─────────────────────────────

class ChatGone(Exception):
    """Sohbete artık gönderilemiyor (bot atıldı / engellendi / yetkisi alındı)."""

_GONE_HINTS = ("chat not found", "bot was kicked", "not enough rights", "have no rights", "chat_write_forbidden",
               "need administrator rights", "bot is not a member", "user is deactivated", "peer_id_invalid",
               "topic_closed", "message thread not found")

def _thread_kw(sub: dict) -> dict:
    return {'message_thread_id': sub['thread_id']} if sub.get('thread_id') else {}

async def _safe_send(coro_factory):
    try:
        return await coro_factory()
    except Forbidden as e:
        raise ChatGone(str(e))
    except BadRequest as e:
        if any(h in str(e).lower() for h in _GONE_HINTS):
            raise ChatGone(str(e))
        raise

async def send_story(bot, sub: dict, st, reason: str | None = None):
    s = sub['settings']
    compact = s.get('fmt') == 'compact'
    sign = s.get('sign', '') if sub['kind'] != 'private' else ''
    markup = story_markup(st, sub['kind'], s, reason)
    chat_id, tk = sub['chat_id'], _thread_kw(sub)
    if not compact and st['image']:
        caption = story_text(st, reason, sign, limit=1024)
        try:
            return await _safe_send(lambda: bot.send_photo(chat_id, st['image'], caption=caption, parse_mode=ParseMode.HTML,
                                                          reply_markup=markup, **tk))
        except BadRequest as e:   # görsel indirilemedi / biçim hatası → yazılı gönder
            logger.debug("Görsel gönderilemedi (%s): %s", st['image'], e)
    text = story_text(st, reason, sign, compact=compact)
    return await _safe_send(lambda: bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                                     link_preview_options=preview_opts(st, compact), **tk))

async def send_text(bot, sub: dict, text: str, markup=None):
    return await _safe_send(lambda: bot.send_message(sub['chat_id'], text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                                     link_preview_options=LinkPreviewOptions(is_disabled=True),
                                                     **_thread_kw(sub)))

def mark_sent(chat_id: int, story_ids, kind: str):
    with db() as c:
        c.executemany("INSERT OR IGNORE INTO sent (chat_id, story_id, at, kind) VALUES (?, ?, ?, ?)",
                      [(int(chat_id), int(sid), now(), kind) for sid in story_ids])

def sent_ids(chat_id: int, story_ids, kind: str | None = None) -> set[int]:
    ids = list(story_ids)
    if not ids:
        return set()
    with db() as c:
        rows = c.execute(f"SELECT story_id FROM sent WHERE chat_id = ? AND story_id IN ({','.join('?' * len(ids))})"
                         + (" AND kind = ?" if kind else ""), (int(chat_id), *ids, *([kind] if kind else []))).fetchall()
    return {r['story_id'] for r in rows}

def sent_count(chat_id: int, seconds: int, kinds: tuple) -> int:
    with db() as c:
        return c.execute(f"SELECT COUNT(*) FROM sent WHERE chat_id = ? AND at > ? AND kind IN ({','.join('?' * len(kinds))})",
                         (int(chat_id), now() - seconds, *kinds)).fetchone()[0]

async def deactivate(bot, sub: dict, why: str):
    set_sub(sub['chat_id'], active=0)
    logger.info("Abonelik durduruldu %s (%s): %s", sub['chat_id'], sub['kind'], why)
    if sub['kind'] != 'private' and sub.get('owner_id'):
        try:
            await bot.send_message(sub['owner_id'],
                                   f"⚠️ <b>{esc(sub.get('title') or sub['chat_id'])}</b> sohbetine haber gönderilemiyor, "
                                   f"gönderim durduruldu.\n<i>Sebep: {esc(why[:150])}</i>\n\n"
                                   "Botu tekrar yönetici yapıp /kanallarim üzerinden yeniden ekleyebilirsin.",
                                   parse_mode=ParseMode.HTML)
        except (Forbidden, BadRequest, TimedOut, NetworkError):
            pass

_deliver_lock = asyncio.Lock()

async def deliver_cycle(bot) -> int:
    """Yeni haberleri (son dakika, anahtar kelime, anlık) abonelere dağıtır. Gönderilen mesaj sayısını döndürür."""
    if _deliver_lock.locked():
        return 0
    async with _deliver_lock:
        t = now()
        subs = [s for s in all_subs() if not s['paused']]
        if not subs:
            return 0
        since = max(min(s['cursor'] for s in subs), t - FRESH_AGE)
        with db() as c:
            fresh = c.execute("SELECT * FROM stories WHERE updated > ? AND fresh = 1 "
                              "AND COALESCE(published, first_seen) > ? ORDER BY updated LIMIT 600",
                              (since, t - FRESH_AGE)).fetchall()
            breaking = c.execute("SELECT * FROM stories WHERE breaking_at > ? ORDER BY breaking_at",
                                 (t - 30 * 60,)).fetchall()
        total = 0
        for sub in subs:
            try:
                total += await _deliver_to(bot, sub, fresh, breaking, t)
            except ChatGone as e:
                await deactivate(bot, sub, str(e))
            except (TimedOut, NetworkError, RetryAfter) as e:
                logger.warning("Gönderim ertelendi %s: %s", sub['chat_id'], e)
            except Exception:
                logger.exception("Gönderim hatası %s", sub['chat_id'])
        return total

async def _deliver_to(bot, sub: dict, fresh: list, breaking: list, t: float) -> int:
    s, cid = sub['settings'], sub['chat_id']
    quiet = in_quiet(s, t)
    already = sent_ids(cid, [st['id'] for st in fresh] + [st['id'] for st in breaking])
    count = 0
    if s.get('breaking', 'off') != 'off' and not quiet:
        daily = sent_count(cid, 86400, ('bd',))
        for st in breaking:
            if daily >= BREAKING_DAILY_CAP:
                break
            if st['id'] in already or not source_allowed(st, s):
                continue
            if s['breaking'] == 'cats' and not set(story_cats(st)) & set(s.get('cats') or []):
                continue
            await send_story(bot, sub, st, 'breaking')
            mark_sent(cid, [st['id']], 'bd')
            already.add(st['id'])
            daily += 1
            count += 1
    picks = []
    for st in fresh:
        if st['updated'] <= sub['cursor'] or st['id'] in already:
            continue
        r = match_reason(st, s)
        if r and (s.get('instant') or r.startswith('kw:')):
            picks.append((st, r))
    if quiet or not picks:
        set_sub(cid, cursor=t)
        return count
    budget = s.get('per_hour', 6) - sent_count(cid, 3600, ('card', 'bulk1'))
    if budget <= 0:
        return count   # imleç ilerlemez; kota açılınca (hâlâ tazeyse) toplu liste olarak gelir
    picks.sort(key=lambda p: (not p[1].startswith('kw:'), -p[0]['source_count'], -(p[0]['published'] or p[0]['first_seen'])))
    if len(picks) <= min(budget, 3):
        for st, r in picks:
            await send_story(bot, sub, st, r if r != 'cat' else None)
            mark_sent(cid, [st['id']], 'card')
            count += 1
    else:
        cards = picks[:1] if budget >= 2 else []
        for st, r in cards:
            await send_story(bot, sub, st, r if r != 'cat' else None)
            mark_sent(cid, [st['id']], 'card')
            count += 1
        rest = [st for st, _ in picks[len(cards):]]
        shown = rest[:12]
        markup = InlineKeyboardMarkup([[ibtn("📰 Tüm son haberler", "l|all|0")]]) if sub['kind'] == 'private' else None
        await send_text(bot, sub, bulk_text(shown, "Diğer yeni haberler") + (f"\n\n<i>+{len(rest) - len(shown)} haber daha</i>"
                                                                              if len(rest) > len(shown) else ""), markup)
        mark_sent(cid, [shown[0]['id']], 'bulk1')
        mark_sent(cid, [st['id'] for st in rest[1:]], 'bulk')
        count += 1
    set_sub(cid, cursor=t)
    return count

# ───────────────────────────── Bülten (günlük özet) ─────────────────────────────

def digest_name(ts: float) -> str:
    h = datetime.fromtimestamp(ts, TR).hour
    return "Sabah Bülteni" if 5 <= h < 12 else "Öğle Bülteni" if h < 17 else "Akşam Bülteni" if h < 22 else "Gece Bülteni"

def build_digest(sub: dict, t: float | None = None) -> tuple[str, list[int]] | None:
    t = t or now()
    s = sub['settings']
    with db() as c:
        rows = c.execute("SELECT * FROM stories WHERE updated > ? AND COALESCE(published, first_seen) > ? "
                         "ORDER BY source_count DESC, COALESCE(published, first_seen) DESC LIMIT 800",
                         (t - 14 * 3600, t - 24 * 3600)).fetchall()
    done = sent_ids(sub['chat_id'], [r['id'] for r in rows], kind='dg')   # günün özeti: sadece önceki bültenler hariç
    rows = [r for r in rows if r['id'] not in done and source_allowed(r, s)]
    used, sections = set(), []
    kw_rows = [r for r in rows if any(kw_match(fold(k), r['ftitle']) for k in s.get('kw') or [])][:5]
    if kw_rows:
        sections.append(("🔔 Takip ettiklerin", kw_rows))
        used |= {r['id'] for r in kw_rows}
    for cat in [c for c in CAT_ORDER if c in (s.get('cats') or [])]:
        picked = [r for r in rows if r['id'] not in used and cat in story_cats(r) and r['source_count'] >= s.get('min_src', 1)][:5]
        if picked:
            sections.append((cat_label(cat), picked))
            used |= {r['id'] for r in picked}
    if not sections:
        return None
    text = f"🗞 <b>{digest_name(t)}</b> · {tr_date(t)}\n"
    trends = TRENDS[:6]
    if trends:
        text += f"🔥 <i>Gündemde: {esc(', '.join(p for p, _ in trends))}</i>\n"
    included = []
    for name, items in sections:
        block = f"\n<b>{esc(name)}</b>\n<blockquote expandable>"
        lines = []
        for r in items:
            n = r['source_count']
            lines.append(f"• <a href=\"{esc(r['link'])}\">{esc(shorten(r['title'], 130))}</a> "
                         f"<i>— {esc(r['source'])}{f' +{n - 1}' if n > 1 else ''}</i>")
        block += "\n".join(lines) + "</blockquote>"
        if len(text) + len(block) > 3900:
            break
        text += block
        included += [r['id'] for r in items]
    if sub['kind'] != 'private' and s.get('sign'):
        text += f"\n\n{esc(s['sign'])}"
    return text, included

async def send_digest(bot, sub: dict) -> bool:
    built = build_digest(sub)
    if not built:
        return False
    text, ids = built
    markup = (InlineKeyboardMarkup([[ibtn("🔥 Öne çıkanlar", "l|top|0"), ibtn("📰 Son haberler", "l|all|0")]])
              if sub['kind'] == 'private' else None)
    await send_text(bot, sub, text, markup)
    with db() as c:   # anlık gönderilmiş olsa da bu haber artık bültende de gösterildi
        c.executemany("INSERT OR REPLACE INTO sent (chat_id, story_id, at, kind) VALUES (?, ?, ?, 'dg')",
                      [(sub['chat_id'], i, now()) for i in ids])
    return True

async def digest_job(context: ContextTypes.DEFAULT_TYPE):
    t = now()
    d = datetime.fromtimestamp(t, TR)
    slot = d.strftime("%H:%M")
    stamp = f"{d:%Y-%m-%d} {slot}"
    for sub in all_subs():
        s = sub['settings']
        if sub['paused'] or slot not in (s.get('digest') or []) or sub.get('last_digest') == stamp:
            continue
        set_sub(sub['chat_id'], last_digest=stamp)
        try:
            await send_digest(context.bot, sub)
        except ChatGone as e:
            await deactivate(context.bot, sub, str(e))
        except (TimedOut, NetworkError, RetryAfter, BadRequest) as e:
            logger.warning("Bülten gönderilemedi %s: %s", sub['chat_id'], e)

# ───────────────────────────── Arayüz yardımcıları ─────────────────────────────

MENU_ROWS = [["📰 Son Haberler", "🔥 Öne Çıkanlar"],
             ["🔴 Son Dakika", "📈 Gündem"],
             ["🗂 Kategoriler", "🔎 Ara"],
             ["⚙️ Ayarlarım", "📡 Kanallarım"]]
MENU_TEXTS = {t for row in MENU_ROWS for t in row} | {"ℹ️ Yardım"}

def menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(t) for t in row] for row in MENU_ROWS] + [[KeyboardButton("ℹ️ Yardım")]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Haber ara ya da menüden seç")

def cb_trim(prefix: str, text: str, limit: int = 64) -> str:
    """callback_data 64 bayt sınırı: Türkçe harfler 2 bayt olduğundan bayta göre kırpılır."""
    data = prefix + text
    while len(data.encode()) > limit:
        data = data[:-1]
    return data

async def safe_edit(query, text: str, markup=None, preview: LinkPreviewOptions | None = None):
    try:
        if query.message and (query.message.photo or query.message.video):
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                           link_preview_options=preview or LinkPreviewOptions(is_disabled=True))
            return
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      link_preview_options=preview or LinkPreviewOptions(is_disabled=True))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise

async def reply(update: Update, text: str, markup=None, preview: LinkPreviewOptions | None = None):
    msg = update.effective_message
    kw = {'message_thread_id': msg.message_thread_id} if getattr(msg, 'is_topic_message', False) and msg.message_thread_id else {}
    return await msg.get_bot().send_message(msg.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                            link_preview_options=preview or LinkPreviewOptions(is_disabled=True), **kw)

def is_founder(uid: int) -> bool:
    return bool(FOUNDER_ID) and uid == FOUNDER_ID

_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}

async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    key = (int(chat_id), int(user_id))
    hit = _admin_cache.get(key)
    if hit and now() - hit[0] < (300 if hit[1] else 30):   # "yönetici değil" kısa süre saklanır
        return hit[1]
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        ok = m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except (BadRequest, Forbidden):
        ok = False
    _admin_cache[key] = (now(), ok)
    return ok

async def can_manage(bot, user_id: int, sub: dict) -> bool:
    if sub['kind'] == 'private':
        return sub['chat_id'] == user_id
    if sub.get('owner_id') == user_id or is_founder(user_id):
        return True
    return await is_chat_admin(bot, sub['chat_id'], user_id)

def add_links() -> InlineKeyboardMarkup:
    u = BOT_USERNAME or "bot"
    return InlineKeyboardMarkup([
        [ibtn("📢 Kanala ekle", url=f"https://t.me/{u}?startchannel&admin=post_messages+edit_messages", style=GREEN),
         ibtn("👥 Gruba ekle", url=f"https://t.me/{u}?startgroup=haber", style=GREEN)]])

# ───────────────────────────── Haber listeleri ─────────────────────────────

PAGE = 8
SEARCHES: dict[str, tuple[str, list[int], float]] = {}

def _list_title(key: str) -> str:
    if key == 'all':
        return "📰 Son Haberler"
    if key == 'top':
        return "🔥 Öne Çıkanlar <i>(en çok kaynağın verdiği)</i>"
    if key == 'bd':
        return "🔴 Son Dakika"
    if key.startswith('s:'):
        q = SEARCHES.get(key[2:], ("",))[0]
        return f"🔎 “{esc(q)}” sonuçları"
    return cat_label(key)

def _list_rows(key: str, page: int) -> tuple[list, bool]:
    off = page * PAGE
    if key.startswith('s:'):
        ids = SEARCHES.get(key[2:], ("", [], 0))[1]
        chunk = ids[off: off + PAGE]
        rows = [r for r in (story_row(i) for i in chunk) if r]
        return rows, len(ids) > off + PAGE
    rows = latest_stories(None if key == 'all' else key, limit=PAGE + 1, offset=off)
    return rows[:PAGE], len(rows) > PAGE

def render_list(key: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    page = max(0, page)
    rows, more = _list_rows(key, page)
    text = f"<b>{_list_title(key)}</b>"
    if page:
        text += f" · sayfa {page + 1}"
    text += "\n"
    if not rows:
        text += "\n<i>Şu an gösterilecek haber yok. Kaynaklar birkaç dakikada bir yenileniyor.</i>"
    for i, st in enumerate(rows, 1):
        n = st['source_count']
        badge = " 🔴" if st['breaking_at'] else ""
        text += (f"\n<b>{i}.</b> {cat_emoji(story_cats(st))} <a href=\"{esc(st['link'])}\">{esc(shorten(st['title'], 150))}</a>{badge}\n"
                 f"      <i>{esc(st['source'])}{f' +{n - 1} kaynak' if n > 1 else ''} · {ago(st['published'] or st['first_seen'])}</i>")
    kb = []
    nums = [ibtn(str(i), f"v|{st['id']}|{key}|{page}") for i, st in enumerate(rows, 1)]
    if nums:
        kb += [nums[:4], nums[4:]] if len(nums) > 4 else [nums]
    nav = []
    if page:
        nav.append(ibtn("◀️ Önceki", f"l|{key}|{page - 1}"))
    nav.append(ibtn("🔄", f"l|{key}|{page}"))
    if more:
        nav.append(ibtn("Sonraki ▶️", f"l|{key}|{page + 1}"))
    kb.append(nav)
    if not key.startswith('s:'):
        tabs = [('all', "📰 Tümü"), ('top', "🔥 Öne çıkan"), ('bd', "🔴 Son dakika")] + \
               [(c, cat_label(c)) for c in CAT_ORDER]
        btns = [ibtn(("• " if k == key else "") + label, f"l|{k}|0", BLUE if k == key else None) for k, label in tabs]
        kb += [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return text, InlineKeyboardMarkup([r for r in kb if r])

def render_story(st, back: str | None = None) -> tuple[str, InlineKeyboardMarkup, LinkPreviewOptions]:
    text = story_text(st, 'breaking' if st['breaking_at'] else None)
    markup = story_markup(st, 'private', {}, back=back)
    rows = list(markup.inline_keyboard)
    rows.insert(1, (InlineKeyboardButton("🔗 Linki kopyala", copy_text=CopyTextButton(st['link'][:256])),
                    ibtn("🧩 Benzer haberler", cb_trim("a|", " ".join(sorted(name_tokens(st['title']))[:3]) or st['title'][:30]))))
    return text, InlineKeyboardMarkup(rows), preview_opts(st, False)

def render_sources(st) -> str:
    items = story_items(st['id'])
    text = f"📚 <b>Bu haberi veren kaynaklar ({len(items)})</b>\n{esc(shorten(st['title'], 200))}\n"
    for it in items[:25]:
        text += (f"\n• <b>{esc(it['source'])}</b> — <a href=\"{esc(it['link'])}\">{esc(shorten(it['title'], 110))}</a> "
                 f"<i>{tr_time(it['published'] or it['fetched'])}</i>")
    return text[:4000]

def render_trends() -> tuple[str, InlineKeyboardMarkup]:
    if not TRENDS:
        return "📈 <b>Gündem</b>\n\n<i>Gündem konuları hesaplanıyor, birazdan tekrar dene.</i>", InlineKeyboardMarkup(
            [[ibtn("🔥 Öne çıkan haberler", "l|top|0")]])
    text = "📈 <b>Şu an gündemde</b> <i>(son saatlerde en çok haberi yapılan konular)</i>\n"
    for i, (p, w) in enumerate(TRENDS, 1):
        text += f"\n{i}. <b>{esc(p)}</b> — {w} haber"
    btns = [ibtn(f"{i}. {p}", cb_trim("a|", p)) for i, (p, _) in enumerate(TRENDS, 1)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([ibtn("🔄 Yenile", "g|0")])
    return text, InlineKeyboardMarkup(rows)

async def do_search(query: str) -> str:
    """Arar, sonuçları önbelleğe alır ve liste anahtarını ('s:<id>') döndürür."""
    query = query.strip()[:60]
    rows = search_stories(query)
    for k in [k for k, v in SEARCHES.items() if now() - v[2] > 3600]:
        del SEARCHES[k]
    sid = secrets.token_hex(3)
    SEARCHES[sid] = (query, [r['id'] for r in rows], now())
    return f"s:{sid}"

# ───────────────────────────── Ayar paneli ─────────────────────────────

def known_sources() -> list[str]:
    with db() as c:
        rows = c.execute("SELECT source, COUNT(*) AS n FROM items WHERE fetched > ? GROUP BY source HAVING n >= 3 "
                         "ORDER BY n DESC LIMIT 60", (now() - 3 * 86400,)).fetchall()
    return sorted((r['source'] for r in rows), key=fold)

def _sub_title(sub: dict) -> str:
    if sub['kind'] == 'private':
        return "Kişisel haber ayarların"
    icon = "📢" if sub['kind'] == 'channel' else "👥"
    return f"{icon} {sub.get('title') or sub['chat_id']}"

def _quiet_label(q) -> str:
    return "Kapalı" if not q else f"{q[0]:02d}:00–{q[1]:02d}:00"

def _breaking_label(v: str) -> str:
    return {'all': "Tümü", 'cats': "Seçili kategoriler", 'off': "Kapalı"}.get(v, "Kapalı")

def _min_src_label(v: int) -> str:
    return {1: "Tüm haberler", 2: "En az 2 kaynak", 3: "En az 3 kaynak (sadece önemli)"}.get(v, "Tüm haberler")

def render_panel(sub: dict, page: str = 'm', arg: str = '') -> tuple[str, InlineKeyboardMarkup]:
    s, cid = sub['settings'], sub['chat_id']
    P = lambda op, a='': f"p|{cid}|{op}|{a}"
    back = [ibtn("⬅️ Geri", P('pg', 'm'))]
    if page == 'c' or page == 'w0':
        text = ("🗂 <b>Hangi konulardan haber gelsin?</b>\n\nİstediklerini seç (yeşil = açık)."
                if page == 'w0' else "🗂 <b>Kategoriler</b>\nAnlık haberlerde ve bültende hangi konular olsun?")
        btns = [toggle_btn(cat_label(c), c in s['cats'], P('c', c)) for c in CAT_ORDER]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([ibtn("✅ Hepsi", P('ca')), ibtn("⬜ Hiçbiri", P('cn'))])
        rows.append([ibtn("Devam ➡️", P('pg', 'w'), GREEN)] if page == 'w0' else back)
        return text, InlineKeyboardMarkup(rows)
    if page == 'w':
        text = ("📬 <b>Haberleri nasıl almak istersin?</b>\n\n"
                "⚡ <b>Anlık:</b> Seçtiğin konulardaki yeni haberler geldikçe (saatte en fazla 6).\n"
                "🗞 <b>Bülten:</b> Günde iki kez (09:00 ve 19:00) günün özeti.\n"
                "🔴 <b>Sadece son dakika:</b> Yalnızca çok sayıda kaynağın aynı anda verdiği önemli gelişmeler.\n\n"
                "<i>Hepsinde son dakika uyarıları açıktır; sonradan Ayarlarım'dan değiştirebilirsin.</i>")
        return text, InlineKeyboardMarkup([[ibtn("⚡ Anlık", P('w', 'a'), GREEN), ibtn("🗞 Bülten", P('w', 'b'), BLUE)],
                                           [ibtn("🔴 Sadece son dakika", P('w', 'sd'))]])
    if page == 's':
        srcs = known_sources()
        pg = int(arg or 0)
        per = 16
        chunk = srcs[pg * per:(pg + 1) * per]
        text = ("📰 <b>Kaynaklar</b>\nKapattığın sitelerin haberleri sana gelmez "
                "(aynı haberi başka bir açık kaynak da verdiyse yine gelir).\n\n"
                f"Kapalı: {len(s['src_off'])}" + (f"\n<i>{esc(', '.join(s['src_off'][:15]))}</i>" if s['src_off'] else ""))
        btns = [toggle_btn(shorten(name, 22), name not in s['src_off'], P('s', f"{pg}.{pg * per + i}"))
                for i, name in enumerate(chunk)]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        nav = []
        if pg:
            nav.append(ibtn("◀️", P('pg', f"s.{pg - 1}")))
        if (pg + 1) * per < len(srcs):
            nav.append(ibtn("▶️", P('pg', f"s.{pg + 1}")))
        if nav:
            rows.append(nav)
        rows.append([ibtn("✅ Hepsini aç", P('sa')), *back])
        return text, InlineKeyboardMarkup(rows)
    if page == 'k':
        text = ("🔔 <b>Takip kelimeleri</b>\nBu kelimeler geçen haberler, kategori ve anlık ayarından bağımsız olarak "
                "hemen gönderilir. Örnek: <code>dolar</code>, <code>deprem</code>, <code>Fenerbahçe</code>\n")
        text += "\n" + (", ".join(f"<code>{esc(k)}</code>" for k in s['kw']) if s['kw'] else "<i>Henüz kelime yok.</i>")
        btns = [ibtn(f"❌ {shorten(k, 20)}", P('k-', str(i)), RED) for i, k in enumerate(s['kw'])]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        if len(s['kw']) < MAX_KEYWORDS:
            rows.append([ibtn("➕ Kelime ekle", P('k+'), GREEN)])
        rows.append(back)
        return text, InlineKeyboardMarkup(rows)
    if page == 'd':
        text = ("🕘 <b>Bülten saatleri</b> (Türkiye saati)\nSeçtiğin saatlerde günün en çok konuşulan haberleri "
                f"kategorilere göre özetlenir. En fazla {MAX_DIGESTS} saat.\n\nSeçili: "
                + (", ".join(sorted(s['digest'])) if s['digest'] else "<i>yok (bülten kapalı)</i>"))
        btns = [toggle_btn(t, t in s['digest'], P('d', t.replace(':', ''))) for t in DIGEST_PRESETS]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        custom = [t for t in s['digest'] if t not in DIGEST_PRESETS]
        if custom:
            rows.append([toggle_btn(t, True, P('d', t.replace(':', ''))) for t in custom])
        rows.append([ibtn("⏰ Başka saat", P('d+')), ibtn("🚫 Bülteni kapat", P('dx'), RED)])
        rows.append([ibtn("📨 Şimdi bir bülten gönder", P('dn'), BLUE)])
        rows.append(back)
        return text, InlineKeyboardMarkup(rows)
    if page == 'o':
        text = ("🎨 <b>Görünüm ve sınırlar</b>\n\n"
                f"• Biçim: <b>{'Kartlı (görsel + özet)' if s['fmt'] == 'card' else 'Kompakt (tek satır)'}</b>\n"
                f"• Saatte en fazla: <b>{s['per_hour']}</b> anlık haber (fazlası tek mesajda listelenir)\n"
                f"• Önem filtresi: <b>{_min_src_label(s['min_src'])}</b>\n"
                f"• Sessiz saatler: <b>{_quiet_label(s['quiet'])}</b>"
                + (f"\n• Haber altı butonlar: <b>{'Açık' if s['buttons'] else 'Kapalı'}</b>" if sub['kind'] != 'private' else ""))
        rows = [[ibtn("🖼 Kartlı" if s['fmt'] != 'card' else "📝 Kompakt", P('f'), BLUE)],
                [ibtn("➖", P('ph', '-')), ibtn(f"Saatte {s['per_hour']}", P('pg', 'o')), ibtn("➕", P('ph', '+'))],
                [ibtn(f"⭐ {_min_src_label(s['min_src'])}", P('ms'))],
                [ibtn(f"🌙 Sessiz: {_quiet_label(s['quiet'])}", P('q'))]]
        if sub['kind'] != 'private':
            rows.append([toggle_btn("Haber altı butonlar", s['buttons'], P('bt'))])
        rows.append(back)
        return text, InlineKeyboardMarkup(rows)
    if page == 'rm':
        return (f"🗑 <b>{esc(_sub_title(sub))}</b> için haber gönderimi tamamen kaldırılsın mı?",
                InlineKeyboardMarkup([[ibtn("Evet, kaldır", P('rm!'), RED), *back]]))
    # ana sayfa
    cats = ", ".join(cat_label(c, False) for c in CAT_ORDER if c in s['cats']) or "—"
    src_state = "Tümü açık" if not s['src_off'] else f"{len(s['src_off'])} kapalı"
    lines = [f"⚙️ <b>{esc(_sub_title(sub))}</b>", "",
             f"🗂 Kategoriler: <b>{esc(cats)}</b>",
             f"📰 Kaynaklar: <b>{src_state}</b>",
             f"🔔 Takip kelimeleri: <b>{esc(', '.join(s['kw'][:6])) if s['kw'] else 'yok'}</b>",
             f"⚡ Anlık haber: <b>{'Açık' if s['instant'] else 'Kapalı'}</b>" + (f" (saatte en fazla {s['per_hour']})" if s['instant'] else ""),
             f"🕘 Bülten: <b>{', '.join(sorted(s['digest'])) if s['digest'] else 'Kapalı'}</b>",
             f"🔴 Son dakika: <b>{_breaking_label(s['breaking'])}</b>",
             f"🌙 Sessiz saatler: <b>{_quiet_label(s['quiet'])}</b>"]
    if sub['kind'] != 'private':
        lines.append(f"✍️ İmza: <b>{esc(s['sign']) if s['sign'] else 'yok'}</b>")
        if sub.get('thread_id'):
            lines.append(f"🧵 Konu (topic): <b>#{sub['thread_id']}</b>")
    lines.append("")
    lines.append("⏸ <b>Duraklatıldı</b>" if sub['paused'] else ("▶️ Aktif" if sub['active'] else "⚠️ Gönderim durdu (bot yetkisi yok)"))
    rows = [[ibtn("🗂 Kategoriler", P('pg', 'c'), BLUE), ibtn("📰 Kaynaklar", P('pg', 's'), BLUE)],
            [ibtn("🔔 Takip kelimeleri", P('pg', 'k'), BLUE), ibtn("🕘 Bülten saatleri", P('pg', 'd'), BLUE)],
            [toggle_btn("Anlık haber", s['instant'], P('i')),
             ibtn(f"🔴 Son dakika: {_breaking_label(s['breaking'])}", P('b'), GREEN if s['breaking'] != 'off' else RED)],
            [ibtn("🎨 Görünüm ve sınırlar", P('pg', 'o'), BLUE)]]
    if sub['kind'] != 'private':
        rows.append([ibtn("✍️ İmza", P('sg')), ibtn("🧪 Deneme gönder", P('t'))])
    rows.append([ibtn("▶️ Sürdür" if sub['paused'] else "⏸ Duraklat", P('ps'), GREEN if sub['paused'] else None)]
                + ([ibtn("🗑 Kaldır", P('pg', 'rm'), RED)] if sub['kind'] != 'private' else [])
                + [ibtn("✖️ Kapat", P('x'))])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def apply_panel_op(sub: dict, op: str, arg: str) -> tuple[str | None, str]:
    """Paneldeki butonun ayarı değiştirmesi. (açılacak sayfa ya da None, kullanıcıya kısa bildirim)"""
    s = sub['settings']
    note = ""
    page = 'm'
    if op == 'c':
        if arg in CATEGORIES:
            s['cats'] = [c for c in s['cats'] if c != arg] if arg in s['cats'] else s['cats'] + [arg]
        page = 'w0' if not s.get('onboarded') else 'c'
    elif op in ('ca', 'cn'):
        s['cats'] = list(CAT_ORDER) if op == 'ca' else []
        page = 'w0' if not s.get('onboarded') else 'c'
    elif op == 's':
        pg, _, idx = arg.partition('.')
        srcs = known_sources()
        if idx.isdigit() and int(idx) < len(srcs):
            name = srcs[int(idx)]
            s['src_off'] = [x for x in s['src_off'] if x != name] if name in s['src_off'] else s['src_off'] + [name]
        page = f"s.{pg or 0}"
    elif op == 'sa':
        s['src_off'] = []
        page = 's.0'
    elif op == 'k-':
        if arg.isdigit() and int(arg) < len(s['kw']):
            note = f"“{s['kw'].pop(int(arg))}” çıkarıldı"
        page = 'k'
    elif op == 'd':
        t = f"{arg[:2]}:{arg[2:]}" if len(arg) == 4 else ""
        if t in s['digest']:
            s['digest'].remove(t)
        elif re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            if len(s['digest']) >= MAX_DIGESTS:
                return 'd', f"En fazla {MAX_DIGESTS} bülten saati seçilebilir"
            s['digest'].append(t)
        page = 'd'
    elif op == 'dx':
        s['digest'] = []
        page = 'd'
    elif op == 'i':
        s['instant'] = not s['instant']
        note = "Anlık haber " + ("açıldı" if s['instant'] else "kapatıldı")
    elif op == 'b':
        order = ['all', 'cats', 'off']
        s['breaking'] = order[(order.index(s['breaking']) + 1) % 3 if s['breaking'] in order else 0]
    elif op == 'f':
        s['fmt'] = 'compact' if s['fmt'] == 'card' else 'card'
        page = 'o'
    elif op == 'ph':
        s['per_hour'] = max(1, min(30, s['per_hour'] + (1 if arg == '+' else -1)))
        page = 'o'
    elif op == 'ms':
        s['min_src'] = s['min_src'] % 3 + 1
        page = 'o'
    elif op == 'q':
        cur = QUIET_PRESETS.index(s['quiet']) if s['quiet'] in QUIET_PRESETS else 0
        s['quiet'] = QUIET_PRESETS[(cur + 1) % len(QUIET_PRESETS)]
        page = 'o'
    elif op == 'bt':
        s['buttons'] = not s['buttons']
        page = 'o'
    elif op == 'w':
        preset = {'a': dict(instant=True, digest=[], breaking='all'),
                  'b': dict(instant=False, digest=['09:00', '19:00'], breaking='all'),
                  'sd': dict(instant=False, digest=[], breaking='all')}.get(arg)
        if preset:
            s.update(preset, onboarded=True)
            note = "Hazırsın! 🎉"
    else:
        return None, ""
    save_settings(sub['chat_id'], s)
    return page, note

INPUT_PROMPTS = {
    'kw': "🔔 Takip etmek istediğin kelimeyi yaz (virgülle birden fazla yazabilirsin).\nÖrnek: <code>dolar, deprem, Fenerbahçe</code>",
    'time': "⏰ Bülten saatini <b>SS:DD</b> biçiminde yaz. Örnek: <code>07:30</code>",
    'sign': "✍️ Kanal gönderilerinin altına eklenecek imzayı yaz (en fazla 100 karakter). Örnek: <code>📢 @kanalim</code>\n"
            "Kaldırmak için <code>-</code> yaz.",
    'search': "🔎 Ne aramak istersin? Örnek: <code>asgari ücret</code>",
}

async def ask_input(context, chat_id: int, user_id: int, kind: str, target: int | None = None, thread_kw: dict | None = None):
    msg = await context.bot.send_message(chat_id, INPUT_PROMPTS[kind], parse_mode=ParseMode.HTML,
                                         reply_markup=ForceReply(selective=True, input_field_placeholder="Yaz ve gönder"),
                                         **(thread_kw or {}))
    context.user_data['await'] = {'kind': kind, 'target': target, 'msg': msg.message_id, 'chat': chat_id, 'at': now()}

def apply_input(sub: dict, kind: str, text: str) -> str:
    s = sub['settings']
    text = text.strip()
    if kind == 'kw':
        added = []
        for raw in re.split(r"[,\n;]+", text):
            k = _WS.sub(" ", raw).strip(" #\"'")
            if not (2 <= len(k) <= 40) or fold(k) in {fold(x) for x in s['kw']}:
                continue
            if len(s['kw']) >= MAX_KEYWORDS:
                break
            s['kw'].append(k)
            added.append(k)
        save_settings(sub['chat_id'], s)
        return (f"✅ Eklendi: {esc(', '.join(added))}" if added else "⚠️ Eklenecek geçerli kelime yok (2-40 karakter).")
    if kind == 'time':
        m = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", text)
        if not m:
            return "⚠️ Saat SS:DD biçiminde olmalı, örnek 07:30"
        t = f"{int(m.group(1)):02d}:{m.group(2)}"
        if t not in s['digest']:
            if len(s['digest']) >= MAX_DIGESTS:
                return f"⚠️ En fazla {MAX_DIGESTS} bülten saati olabilir."
            s['digest'].append(t)
        save_settings(sub['chat_id'], s)
        return f"✅ Bülten saati eklendi: {t}"
    if kind == 'sign':
        s['sign'] = "" if text == "-" else text[:100]
        save_settings(sub['chat_id'], s)
        return "✅ İmza kaldırıldı." if not s['sign'] else f"✅ İmza: {esc(s['sign'])}"
    return ""

# ───────────────────────────── Komutlar ─────────────────────────────

HELP_PRIVATE = """🗞 <b>ULUS Haber</b> — Türkiye'nin haber sitelerinden canlı haberler

<b>Okumak için</b>
/son — Son haberler (kategori sekmeleriyle)
/onecikan — En çok kaynağın verdiği haberler
/sondakika — Son dakika gelişmeleri
/gundem — Şu an gündemde olan konular
/ara <i>kelime</i> — Haber ara
/bulten — Şimdi bir bülten gönder

<b>Ayarlar</b>
/ayarlar — Kategori, kaynak, takip kelimesi, bülten saati, anlık/son dakika bildirimleri
/kanallarim — Kanalına veya grubuna otomatik haber gönder

<b>Her yerde paylaş</b>
Herhangi bir sohbette <code>@{bot} dolar</code> yaz, çıkan haberi seçip gönder."""

HELP_GROUP = """🗞 <b>ULUS Haber</b>
/son — Son haberler · /onecikan — Öne çıkanlar
/sondakika — Son dakika · /gundem — Gündem konuları
/ara <i>kelime</i> — Haber ara
/haberkur — (yönetici) Bu gruba otomatik haber ayarları"""

HELP_BLOCK = """<b>Yönetici</b>
/istatistik · /kaynaklar · /duyuru <i>metin</i>"""

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if chat.type != 'private':
        await reply(update, HELP_GROUP)
        return
    sub, created = ensure_sub(user.id, 'private', user.id, user.full_name)
    arg = (context.args or [""])[0]
    if not sub['settings'].get('onboarded'):
        await update.effective_message.reply_text(
            f"👋 Merhaba <b>{esc(user.first_name)}</b>! Ben <b>ULUS Haber</b>.\n\n"
            "TRT, AA, NTV, Hürriyet, Sözcü, BBC Türkçe ve daha pek çok siteden haberleri toplayıp aynı haberi "
            "tek mesajda birleştiriyorum; son dakika gelişmelerini ve gündemi takip ediyorum.",
            parse_mode=ParseMode.HTML, reply_markup=menu_keyboard())
        text, markup = render_panel(sub, 'w0')
        await reply(update, text, markup)
        return
    if arg == 'ayarlar':
        await cmd_settings(update, context)
        return
    await update.effective_message.reply_text(
        "🗞 <b>ULUS Haber</b> hazır. Aşağıdaki menüden haberlere göz atabilir, /ayarlar ile bildirimlerini düzenleyebilirsin.",
        parse_mode=ParseMode.HTML, reply_markup=menu_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        await reply(update, HELP_GROUP)
        return
    text = HELP_PRIVATE.replace("{bot}", esc(BOT_USERNAME or "bot"))
    if is_founder(update.effective_user.id):
        text += "\n\n" + HELP_BLOCK
    await reply(update, text)

def _cat_from_args(args) -> str:
    if not args:
        return 'all'
    a = fold(" ".join(args))
    for key, (label, _) in CATEGORIES.items():
        if a in (key, fold(label)) or fold(label).startswith(a):
            return key
    return 'all'

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = render_list(_cat_from_args(context.args))
    await reply(update, text, markup)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = render_list('top')
    await reply(update, text, markup)

async def cmd_breaking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = render_list('bd')
    await reply(update, text, markup)

async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btns = [ibtn(cat_label(c), f"l|{c}|0", BLUE) for c in CAT_ORDER]
    await reply(update, "🗂 <b>Kategoriler</b>\nBir kategori seç:", InlineKeyboardMarkup([btns[i:i + 3] for i in range(0, len(btns), 3)]))

async def cmd_trends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = render_trends()
    await reply(update, text, markup)

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args or []).strip()
    if len(q) < 2:
        await ask_input(context, update.effective_chat.id, update.effective_user.id, 'search')
        return
    wait = await reply(update, f"🔎 “{esc(q)}” aranıyor…")
    text, markup = render_list(await do_search(q))
    await wait.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if chat.type != 'private':
        await cmd_setup_group(update, context)
        return
    sub, _ = ensure_sub(user.id, 'private', user.id, user.full_name)
    text, markup = render_panel(sub)
    await reply(update, text, markup)

async def cmd_digest_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != 'private':
        return
    sub, _ = ensure_sub(user.id, 'private', user.id, user.full_name)
    if not await send_digest(context.bot, sub):
        await reply(update, "📭 Şu an bülten için yeni haber yok (son bültenden beri gösterilmemiş haber bulunamadı).")

async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != 'private':
        return
    subs = owned_subs(user.id)
    text = ("📡 <b>Kanallarım ve gruplarım</b>\n\nBotu kanalına ya da grubuna eklediğinde seçtiğin konulardaki haberler "
            "otomatik paylaşılır. Aşağıdaki butonlarla ekle; bot eklenince ayar paneli buraya gelir.\n")
    rows = []
    for s in subs:
        state = "⏸" if s['paused'] else ("✅" if s['active'] else "⚠️")
        text += f"\n{state} {esc(_sub_title(s))}"
        rows.append([ibtn(f"⚙️ {shorten(_sub_title(s), 40)}", f"p|{s['chat_id']}|pg|m", BLUE)])
    if not subs:
        text += "\n<i>Henüz eklenmiş kanal ya da grup yok.</i>"
    rows += add_links().inline_keyboard
    await reply(update, text, InlineKeyboardMarkup(rows))

async def cmd_setup_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/haberkur: grupta yönetici; haber ayarlarını açar. Konulu grupta komutun yazıldığı konuya gönderir."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == 'private':
        await cmd_channels(update, context)
        return
    if not (is_founder(user.id) or await is_chat_admin(context.bot, chat.id, user.id)):
        await reply(update, "Bu ayarı sadece grup yöneticileri yapabilir.")
        return
    sub, _ = ensure_sub(chat.id, 'group', user.id, chat.title)
    note = ""
    if getattr(msg, 'is_topic_message', False) and msg.message_thread_id:
        set_sub(chat.id, thread_id=msg.message_thread_id)
        note = "\n🧵 Haberler bu konuya (topic) gönderilecek."
        sub = get_sub(chat.id)
    text, markup = render_panel(sub)
    try:
        await context.bot.send_message(user.id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        await reply(update, "⚙️ Ayar panelini sana özelden gönderdim." + note)
    except (Forbidden, BadRequest):
        await reply(update, text + note, markup)

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.effective_message.text
    context.user_data.pop('await', None)
    handlers = {"📰 Son Haberler": cmd_latest, "🔥 Öne Çıkanlar": cmd_top, "🔴 Son Dakika": cmd_breaking,
                "📈 Gündem": cmd_trends, "🗂 Kategoriler": cmd_categories, "🔎 Ara": cmd_search,
                "⚙️ Ayarlarım": cmd_settings, "📡 Kanallarım": cmd_channels, "ℹ️ Yardım": cmd_help}
    context.args = []
    await handlers[t](update, context)

async def input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ForceReply ile istenen metin girdileri (takip kelimesi, bülten saati, imza, arama)."""
    msg = update.effective_message
    pending = context.user_data.get('await')
    if not pending or not msg or not msg.text or msg.text in MENU_TEXTS:
        return
    replied = msg.reply_to_message.message_id if msg.reply_to_message else None
    if update.effective_chat.type != 'private' and replied != pending['msg']:
        return
    if update.effective_chat.id != pending['chat'] or now() - pending['at'] > 900:
        context.user_data.pop('await', None)
        return
    context.user_data.pop('await', None)
    if pending['kind'] == 'search':
        context.args = msg.text.split()
        await cmd_search(update, context)
        raise ApplicationHandlerStop
    sub = get_sub(pending['target']) if pending['target'] is not None else None
    if not sub or not await can_manage(context.bot, update.effective_user.id, sub):
        raise ApplicationHandlerStop
    result = apply_input(sub, pending['kind'], msg.text)
    page = {'kw': 'k', 'time': 'd', 'sign': 'm'}[pending['kind']]
    text, markup = render_panel(get_sub(sub['chat_id']), page)
    await reply(update, f"{result}\n\n{text}", markup)
    raise ApplicationHandlerStop

# ───────────────────────────── Buton yönlendirme ─────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    kind, _, rest = data.partition("|")
    try:
        if kind == 'l':
            key, _, page = rest.partition("|")
            text, markup = render_list(key, int(page or 0))
            await q.answer()
            await safe_edit(q, text, markup)
        elif kind == 'v':
            sid, key, page = (rest.split("|") + ["all", "0"])[:3]
            st = story_row(int(sid))
            if not st:
                await q.answer("Bu haber artık arşivde yok.", show_alert=True)
                return
            text, markup, prev = render_story(st, back=f"l|{key}|{page}")
            await q.answer()
            await safe_edit(q, text, markup, prev)
        elif kind == 'k':
            st = story_row(int(rest))
            if not st:
                await q.answer("Bu haber artık arşivde yok.", show_alert=True)
                return
            if q.message and q.message.chat.type == 'private':
                await q.answer()
                await q.message.reply_text(render_sources(st), parse_mode=ParseMode.HTML,
                                           link_preview_options=LinkPreviewOptions(is_disabled=True))
            else:
                names = ", ".join(x['source'] for x in story_items(st['id']))
                await q.answer(shorten(f"📚 {names}", 195), show_alert=True)
        elif kind == 'a':
            await q.answer("Aranıyor…")
            text, markup = render_list(await do_search(rest))
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                       link_preview_options=LinkPreviewOptions(is_disabled=True))
        elif kind == 'g':
            text, markup = render_trends()
            await q.answer()
            await safe_edit(q, text, markup)
        elif kind == 'me':
            uid = q.from_user.id
            sub, _ = ensure_sub(uid, 'private', uid, q.from_user.full_name)
            text, markup = render_panel(sub, rest or 'm')
            await q.answer()
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        elif kind == 'p':
            await panel_callback(update, context, rest)
        else:
            await q.answer()
    except (ValueError, IndexError):
        await q.answer("Geçersiz buton.", show_alert=True)

async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, rest: str):
    q = update.callback_query
    cid, op, arg = (rest.split("|") + ["", ""])[:3]
    sub = get_sub(int(cid))
    if not sub:
        await q.answer("Kayıt bulunamadı.", show_alert=True)
        return
    if not await can_manage(context.bot, q.from_user.id, sub):
        await q.answer("Bu ayarları değiştirme yetkin yok.", show_alert=True)
        return
    if op == 'pg':
        page, _, sub_arg = arg.partition('.')
        text, markup = render_panel(sub, page or 'm', sub_arg)
        await q.answer()
        await safe_edit(q, text, markup)
        return
    if op == 'x':
        await q.answer()
        try:
            await q.message.delete()
        except BadRequest:
            await safe_edit(q, "✔️ Ayarlar kaydedildi.")
        return
    if op == 'ps':
        set_sub(sub['chat_id'], paused=0 if sub['paused'] else 1, cursor=now())
        await q.answer("Sürdürüldü" if sub['paused'] else "Duraklatıldı")
        text, markup = render_panel(get_sub(sub['chat_id']))
        await safe_edit(q, text, markup)
        return
    if op == 'rm!':
        with db() as c:
            c.execute("DELETE FROM subs WHERE chat_id = ?", (sub['chat_id'],))
            c.execute("DELETE FROM sent WHERE chat_id = ?", (sub['chat_id'],))
        await q.answer("Kaldırıldı")
        await safe_edit(q, f"🗑 {esc(_sub_title(sub))} kaldırıldı. Botu sohbetten de çıkarabilirsin.")
        return
    if op in ('k+', 'd+', 'sg'):
        await q.answer()
        tk = {'message_thread_id': q.message.message_thread_id} if q.message and q.message.is_topic_message else {}
        await ask_input(context, q.message.chat_id, q.from_user.id, {'k+': 'kw', 'd+': 'time', 'sg': 'sign'}[op],
                        sub['chat_id'], tk)
        return
    if op == 't':
        rows = latest_stories('top', limit=1)
        if not rows:
            await q.answer("Henüz haber yok.", show_alert=True)
            return
        try:
            await send_story(context.bot, sub, rows[0])
            await q.answer("Deneme haberi gönderildi ✅", show_alert=True)
        except ChatGone as e:
            await q.answer(shorten(f"Gönderilemedi: bot yönetici mi? ({e})", 195), show_alert=True)
        return
    if op == 'dn':
        try:
            ok = await send_digest(context.bot, sub)
        except ChatGone as e:
            await q.answer(shorten(f"Gönderilemedi: {e}", 195), show_alert=True)
            return
        await q.answer("Bülten gönderildi ✅" if ok else "Bülten için yeni haber yok.", show_alert=not ok)
        return
    page, note = apply_panel_op(sub, op, arg)
    if page is None:
        await q.answer()
        return
    sub = get_sub(sub['chat_id'])
    await q.answer(note)
    if op == 'w':
        await safe_edit(q, f"{note}\n\n" + render_panel(sub)[0], render_panel(sub)[1])
        return
    p, _, a = page.partition('.')
    text, markup = render_panel(sub, p, a)
    await safe_edit(q, text, markup)

# ───────────────────────────── Kanala / gruba eklenme ─────────────────────────────

async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    chat, adder = cm.chat, cm.from_user
    new, old = cm.new_chat_member, cm.old_chat_member
    if chat.type == 'private':
        if new.status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            set_sub(chat.id, active=0)    # kullanıcı botu engelledi
        elif old.status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            set_sub(chat.id, active=1, cursor=now())
        return
    if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        if get_sub(chat.id):
            set_sub(chat.id, active=0)
        return
    was_in = old.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    kind = 'channel' if chat.type == 'channel' else 'group'
    can_post = kind == 'group' or (new.status == ChatMemberStatus.ADMINISTRATOR and getattr(new, 'can_post_messages', False))
    if was_in and get_sub(chat.id) and get_sub(chat.id)['active']:
        if kind == 'channel' and not can_post:
            set_sub(chat.id, active=0)
        return
    if kind == 'channel' and not can_post:
        try:
            await context.bot.send_message(adder.id, f"⚠️ <b>{esc(chat.title)}</b> kanalında bota <b>Mesaj gönderme</b> "
                                                     "yetkisi vermelisin; aksi hâlde haber paylaşamam.", parse_mode=ParseMode.HTML)
        except (Forbidden, BadRequest):
            pass
        return
    sub, _ = ensure_sub(chat.id, kind, adder.id if adder and not adder.is_bot else None, chat.title)
    text, markup = render_panel(sub)
    intro = (f"✅ <b>{esc(chat.title)}</b> eklendi!\n\n"
             + ("Varsayılan: Gündem, Dünya ve Ekonomi haberleri anlık paylaşılır (saatte en fazla 6). " if kind == 'channel' else
                "Grupta herkes /son, /onecikan, /gundem komutlarını kullanabilir. Otomatik bülten 09:00 ve 19:00'da gelir. ")
             + "Aşağıdan değiştirebilirsin.\n\n")
    try:
        await context.bot.send_message(adder.id, intro + text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except (Forbidden, BadRequest):
        pass
    if kind == 'group':
        try:
            await context.bot.send_message(chat.id, "🗞 <b>ULUS Haber</b> gruba katıldı!\n\n" + HELP_GROUP, parse_mode=ParseMode.HTML)
        except (Forbidden, BadRequest):
            pass

# ───────────────────────────── Satır içi (inline) paylaşım ─────────────────────────────

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip()
    if q.startswith("#") and q[1:].isdigit():
        st = story_row(int(q[1:]))
        rows = [st] if st else []
    elif len(q) >= 2:
        rows = search_stories(q, limit=20)
    else:
        rows = latest_stories('top', limit=20)
    results = []
    for st in rows:
        n = st['source_count']
        desc = f"{st['source']}{f' +{n - 1}' if n > 1 else ''} · {ago(st['published'] or st['first_seen'])}"
        if st['summary']:
            desc += f" — {shorten(st['summary'], 90)}"
        results.append(InlineQueryResultArticle(
            id=str(st['id']), title=shorten(st['title'], 120), description=desc,
            thumbnail_url=st['image'] or None,
            input_message_content=InputTextMessageContent(story_text(st), parse_mode=ParseMode.HTML,
                                                          link_preview_options=preview_opts(st, False)),
            reply_markup=InlineKeyboardMarkup([[ibtn("📖 Haberi oku", url=st['link'])]])))
    await iq.answer(results, cache_time=60, is_personal=False)

# ───────────────────────────── Kurucu komutları ─────────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_founder(update.effective_user.id):
        return
    t = now()
    with db() as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        text = (f"📊 <b>ULUS Haber istatistik</b>\n\n"
                f"👤 Kullanıcı: <b>{q('SELECT COUNT(*) FROM subs WHERE kind = ? AND active = 1', 'private')}</b> "
                f"(toplam {q('SELECT COUNT(*) FROM subs WHERE kind = ?', 'private')})\n"
                f"📢 Kanal: <b>{q('SELECT COUNT(*) FROM subs WHERE kind = ? AND active = 1', 'channel')}</b> · "
                f"👥 Grup: <b>{q('SELECT COUNT(*) FROM subs WHERE kind = ? AND active = 1', 'group')}</b>\n\n"
                f"🗞 Son 24 saatte haber: <b>{q('SELECT COUNT(*) FROM stories WHERE first_seen > ?', t - 86400)}</b> "
                f"(çok kaynaklı {q('SELECT COUNT(*) FROM stories WHERE first_seen > ? AND source_count > 1', t - 86400)})\n"
                f"🔴 Son dakika: <b>{q('SELECT COUNT(*) FROM stories WHERE breaking_at > ?', t - 86400)}</b>\n"
                f"📨 Gönderilen: <b>{q('SELECT COUNT(*) FROM sent WHERE at > ?', t - 86400)}</b>\n\n"
                f"🔌 Akış: {len(FEEDS)} · hatalı: {sum(1 for f in FEEDS if f.errors)}")
    await reply(update, text)

async def cmd_sources_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_founder(update.effective_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT source, COUNT(*) AS n FROM items WHERE fetched > ? GROUP BY source ORDER BY n DESC LIMIT 25",
                         (now() - 86400,)).fetchall()
    text = f"🔌 <b>Kaynaklar</b> ({len(FEEDS)} akış)\n\n<b>Son 24 saatte gelen haber (ilk 25)</b>\n"
    text += "\n".join(f"• {esc(r['source'])}: {r['n']}" for r in rows) or "<i>veri yok</i>"
    bad = [f for f in FEEDS if f.errors]
    if bad:
        text += "\n\n<b>Hatalı akışlar</b>\n" + "\n".join(
            f"• {esc(f.source)} ({esc(cat_label(f.cat, False))}): {esc(f.last_error[:80])}" for f in bad[:15])
    await reply(update, text[:4000])

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_founder(update.effective_user.id):
        return
    msg = update.effective_message
    text = " ".join(context.args or []).strip() or (msg.reply_to_message.text_html if msg.reply_to_message and msg.reply_to_message.text else "")
    if not text:
        await reply(update, "Kullanım: /duyuru <i>metin</i> (ya da bir mesaja yanıt olarak)")
        return
    targets = [s for s in all_subs() if s['kind'] == 'private']
    await reply(update, f"📣 Duyuru {len(targets)} kişiye gönderiliyor…")

    async def run():
        ok = 0
        for s in targets:
            try:
                await context.bot.send_message(s['chat_id'], text, parse_mode=ParseMode.HTML)
                ok += 1
            except Forbidden:
                set_sub(s['chat_id'], active=0)
            except (BadRequest, TimedOut, NetworkError):
                pass
        await context.bot.send_message(update.effective_user.id, f"✅ Duyuru tamamlandı: {ok}/{len(targets)}")
    context.application.create_task(run())

# ───────────────────────────── Zamanlanmış işler ─────────────────────────────

async def fetch_job(context: ContextTypes.DEFAULT_TYPE):
    changed = await fetch_cycle()
    if changed:
        logger.debug("%d haber güncellendi", changed)
    await deliver_cycle(context.bot)

async def trends_job(context: ContextTypes.DEFAULT_TYPE):
    TRENDS[:] = compute_trends()

async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    limit = now() - KEEP_DAYS * 86400
    with db() as c:
        c.execute("DELETE FROM items WHERE story_id IN (SELECT id FROM stories WHERE updated < ?)", (limit,))
        c.execute("DELETE FROM stories WHERE updated < ?", (limit,))
        c.execute("DELETE FROM sent WHERE at < ?", (now() - 4 * 86400,))
    logger.info("Eski haberler temizlendi")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning("Ağ hatası: %s", context.error)
        return
    logger.error("İşlenmeyen hata", exc_info=context.error)

async def post_init(app: Application):
    global BOT_ID, BOT_USERNAME
    me = await app.bot.get_me()
    BOT_ID, BOT_USERNAME = me.id, me.username or ""
    index.load()
    setup_feeds()
    TRENDS[:] = compute_trends()
    try:
        await app.bot.set_my_commands([BotCommand("son", "Son haberler"), BotCommand("onecikan", "Öne çıkan haberler"),
                                       BotCommand("sondakika", "Son dakika"), BotCommand("gundem", "Gündemdeki konular"),
                                       BotCommand("ara", "Haber ara"), BotCommand("bulten", "Şimdi bülten gönder"),
                                       BotCommand("ayarlar", "Bildirim ayarları"), BotCommand("kanallarim", "Kanal/grup ekle"),
                                       BotCommand("yardim", "Yardım")], scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands([BotCommand("son", "Son haberler"), BotCommand("onecikan", "Öne çıkanlar"),
                                       BotCommand("sondakika", "Son dakika"), BotCommand("gundem", "Gündem konuları"),
                                       BotCommand("ara", "Haber ara"), BotCommand("haberkur", "Otomatik haber ayarları (yönetici)")],
                                      scope=BotCommandScopeAllGroupChats())
        if FOUNDER_ID:
            await app.bot.set_my_commands([BotCommand("son", "Son haberler"), BotCommand("ayarlar", "Bildirim ayarları"),
                                           BotCommand("istatistik", "İstatistik"), BotCommand("kaynaklar", "Kaynak durumu"),
                                           BotCommand("duyuru", "Duyuru gönder"),
                                           BotCommand("yardim", "Yardım")], scope=BotCommandScopeChat(FOUNDER_ID))
        await app.bot.set_my_short_description("Türkiye'nin haber sitelerinden canlı haber, son dakika ve gündem.")
        await app.bot.set_my_description(
            "🗞 ULUS Haber: TRT, AA, NTV, Hürriyet, Sözcü, BBC Türkçe ve daha fazlasından haberleri toplar, aynı haberi "
            "birleştirir. Son dakika uyarıları, gündem konuları, anahtar kelime takibi, sabah/akşam bülteni. "
            "Kanalına veya grubuna ekleyip otomatik haber paylaşabilirsin.")
    except (BadRequest, TimedOut, NetworkError) as e:
        logger.warning("Komut menüsü ayarlanamadı: %s", e)
    logger.info("ULUS Haber başladı: @%s", BOT_USERNAME)

async def post_shutdown(app: Application):
    if _http is not None:
        await _http.aclose()

def build_app() -> Application:
    app = (ApplicationBuilder().token(BOT_TOKEN).rate_limiter(AIORateLimiter(max_retries=3))
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, input_handler), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["yardim", "help"], cmd_help))
    app.add_handler(CommandHandler(["son", "haberler"], cmd_latest))
    app.add_handler(CommandHandler(["onecikan", "manset"], cmd_top))
    app.add_handler(CommandHandler("sondakika", cmd_breaking))
    app.add_handler(CommandHandler(["gundem", "trend"], cmd_trends))
    app.add_handler(CommandHandler("kategoriler", cmd_categories))
    app.add_handler(CommandHandler("ara", cmd_search))
    app.add_handler(CommandHandler("ayarlar", cmd_settings))
    app.add_handler(CommandHandler("bulten", cmd_digest_now))
    app.add_handler(CommandHandler("kanallarim", cmd_channels))
    app.add_handler(CommandHandler("haberkur", cmd_setup_group))
    app.add_handler(CommandHandler("istatistik", cmd_stats))
    app.add_handler(CommandHandler("kaynaklar", cmd_sources_health))
    app.add_handler(CommandHandler("duyuru", cmd_broadcast))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Text(list(MENU_TEXTS)), menu_router))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(error_handler)
    jq = app.job_queue
    jq.run_repeating(fetch_job, interval=60, first=5, name="fetch")
    jq.run_repeating(digest_job, interval=60, first=60 - datetime.now().second + 2, name="digest")
    jq.run_repeating(trends_job, interval=600, first=90, name="trends")
    jq.run_daily(cleanup_job, time=datetime.strptime("04:30", "%H:%M").time().replace(tzinfo=TR), name="cleanup")
    return app

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env dosyasında tanımlı değil.")
    init_db()
    app = build_app()
    app.run_polling(allowed_updates=["message", "callback_query", "inline_query", "my_chat_member"],
                    drop_pending_updates=True)

if __name__ == '__main__':
    main()
