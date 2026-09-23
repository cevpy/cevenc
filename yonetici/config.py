"""Yönetici bot yapılandırması. Değerler yonetici/.env dosyasından okunur."""
import os
import sys
from pathlib import Path

from envfile import read_env

BASE_DIR = Path(__file__).resolve().parent

for _k, _v in read_env(BASE_DIR / ".env").items():
    os.environ.setdefault(_k, _v)


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _path(name, default):
    p = Path(os.environ.get(name, default)).expanduser()
    return p if p.is_absolute() else BASE_DIR / p


# --- Telegram ---
MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN", "")
# Silinemeyen sahip hesap(lar), virgülle ayrılmış Telegram kullanıcı ID'leri
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}
WEB_HOST = os.environ.get("WEB_HOST", "kullaniciadi.pythonanywhere.com")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # 1-256 karakter, A-Z a-z 0-9 _ -
WEBHOOK_PATH = "/webhook"
MINIAPP_PATH = "/app"

# --- Veritabanı ---
DB_BACKEND = os.environ.get("DB_BACKEND", "mysql")  # mysql | sqlite (yerel test)
MYSQL_HOST = os.environ.get("MYSQL_HOST", "")  # kullaniciadi.mysql.pythonanywhere-services.com
MYSQL_USER = os.environ.get("MYSQL_USER", "")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB = os.environ.get("MYSQL_DB", "")  # kullaniciadi$botyonetici
SQLITE_PATH = _path("SQLITE_PATH", "yonetici.sqlite3")

# --- Klasörler ---
BOTS_DIR = _path("BOTS_DIR", "bots")
BACKUPS_DIR = _path("BACKUPS_DIR", "backups")
UPLOADS_DIR = _path("UPLOADS_DIR", "uploads")
SUPERVISOR_LOG = _path("SUPERVISOR_LOG", "supervisor.log")

# --- Alt bot çalıştırma ---
# Alt botların venv'lerini oluşturacak Python (varsayılan: supervisor'ı çalıştıran Python)
BOT_PYTHON = os.environ.get("BOT_PYTHON", sys.executable)
DEFAULT_RAM_MB = _int("DEFAULT_RAM_MB", 256)
RAM_CHOICES_MB = (128, 256, 512, 1024)
MAX_RAM_MB = _int("MAX_RAM_MB", 2048)
RAM_LIMIT_MODE = os.environ.get("RAM_LIMIT_MODE", "AS").upper()  # AS | DATA
BOT_NICE = _int("BOT_NICE", 10)
STOP_TIMEOUT = _int("STOP_TIMEOUT", 10)
PIP_TIMEOUT = _int("PIP_TIMEOUT", 900)
DISK_QUOTA_MB = _int("DISK_QUOTA_MB", 0)  # 0 = bilinmiyor; PythonAnywhere planınızdaki kota

# --- Loglar / yedekler / yüklemeler ---
LOG_MAX_BYTES = _int("LOG_MAX_BYTES", 5 * 1024 * 1024)
LOG_KEEP = _int("LOG_KEEP", 3)
MAX_BACKUPS_PER_BOT = _int("MAX_BACKUPS_PER_BOT", 10)
MAX_ZIP_BYTES = _int("MAX_ZIP_MB", 20) * 1024 * 1024  # Telegram bot indirme limiti 20 MB
MAX_UNZIPPED_BYTES = _int("MAX_UNZIPPED_MB", 200) * 1024 * 1024
MAX_ZIP_FILES = _int("MAX_ZIP_FILES", 3000)

# Yedek/dosya listelerinde yok sayılan klasörler
SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git"}
