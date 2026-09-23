"""Ana bot ve Mini App'in ortak kullandığı işlemler (yetki kontrolü çağıran tarafta yapılır)."""
import re
import time
import uuid
from pathlib import Path

import config
import core
import db
from core import UserError, esc
from envfile import KEY_RE, parse_env_text, write_env

BOT_ACTIONS = {"start", "stop", "restart", "install"}
SETTING_RANGES = {
    "cpu_warn_percent": (0, 1000), "cpu_kill_percent": (0, 1000), "cpu_window_sec": (5, 3600),
    "ram_warn_percent": (0, 100), "crash_limit": (0, 100), "crash_window_sec": (10, 3600),
    "notify_crashes": (0, 1),
}


def require_bot(name):
    core.validate_name(name)
    bot = db.get_bot(name)
    if not bot:
        raise UserError("Bot bulunamadı.")
    return bot


def supervisor_alive() -> bool:
    return time.time() - db.get_int_setting("supervisor_heartbeat", 0) < 30


def queue_action(user_id, name, action, chat_id=None, msg_id=None, **payload) -> str:
    bot = require_bot(name)
    if action not in BOT_ACTIONS:
        raise UserError("Geçersiz işlem.")
    if bot.get("busy"):
        raise UserError(f"Bot üzerinde işlem sürüyor: {bot['busy']}")
    if action == "start" and bot["status"] == "running":
        raise UserError("Bot zaten çalışıyor.")
    db.enqueue(action, name, dict(payload, msg_id=msg_id), requested_by=user_id, chat_id=chat_id)
    db.audit(user_id, action, name)
    note = "⏳ Kuyruğa alındı, supervisor işleyecek."
    if not supervisor_alive():
        note += "\n⚠️ Supervisor şu an yanıt vermiyor; açıldığında işlenecek."
    return note


def set_ram(user_id, name, mb):
    require_bot(name)
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        raise UserError("Sayı girin (MB).")
    if not 64 <= mb <= config.MAX_RAM_MB:
        raise UserError(f"RAM limiti 64 - {config.MAX_RAM_MB} MB arasında olmalı.")
    db.update_bot(name, ram_limit_mb=mb)
    db.audit(user_id, "ram", name, f"{mb} MB")


def toggle(user_id, name, field, value=None):
    bot = require_bot(name)
    if field not in ("autostart", "auto_restart"):
        raise UserError("Geçersiz ayar.")
    new = (0 if bot[field] else 1) if value is None else int(bool(value))
    db.update_bot(name, **{field: new})
    db.audit(user_id, "toggle", name, f"{field}={new}")


def _check_env_tokens(name, env):
    """.env'deki token'lar çakışıyor mu? tg bilgilerini günceller."""
    warnings = []
    for tok in core.tokens_in_env(env):
        info = core.check_token(tok, exclude_name=name)
        db.update_bot(name, tg_bot_id=info["tg_bot_id"], tg_username=info["tg_username"])
        warnings += info["warnings"]
    return warnings


def apply_env_text(user_id, name, text) -> list:
    """'KEY=VALUE' satırları ayarlar, '-KEY' satırları siler."""
    require_bot(name)
    env = core.env_of(name)
    changed = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            key = line[1:].strip()
            if env.pop(key, None) is not None:
                changed.append(f"-{key}")
            continue
        if "=" not in line:
            raise UserError(f"Anlaşılamayan satır: <code>{esc(line[:50])}</code>")
        key, value = (x.strip() for x in line.split("=", 1))
        if not KEY_RE.match(key):
            raise UserError(f"Geçersiz anahtar: <code>{esc(key[:50])}</code>")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
        changed.append(key)
    if not changed:
        raise UserError("Değişiklik yok.")
    return save_env(user_id, name, env, changed)


def save_env(user_id, name, env: dict, changed=None) -> list:
    require_bot(name)
    for k in env:
        if not KEY_RE.match(k):
            raise UserError(f"Geçersiz anahtar: {esc(k)}")
    warnings = _check_env_tokens(name, env)
    write_env(core.bot_dir(name) / ".env", env)
    db.audit(user_id, "env", name, ", ".join(changed or env.keys())[:200])
    return warnings


# ---------------------------------------------------------------- yükleme / yeni bot
def new_upload_path(name) -> Path:
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return config.UPLOADS_DIR / f"{name}_{int(time.time())}_{uuid.uuid4().hex[:6]}.zip"


def check_new_name(name):
    name = core.validate_name(name)
    if db.get_bot(name) or core.bot_dir(name).exists():
        raise UserError("Bu isimde bir bot zaten var.")
    return name


def create_bot_from_zip(user_id, name, zip_path: Path, token=None, chat_id=None, msg_id=None) -> list:
    """Zip'i denetler, token çakışmalarını kontrol eder, bot kaydını oluşturur ve deploy'u kuyruğa alır."""
    try:
        check_new_name(name)
        meta = core.inspect_zip(zip_path)
        warnings, info = [], {"tg_bot_id": None, "tg_username": None}
        tokens = [token] if token else core.tokens_in_env(meta["env"])
        for tok in tokens:
            info = core.check_token(tok)
            warnings += info["warnings"]
        if not tokens:
            warnings.append("Token bulunamadı (.env'de BOT_TOKEN yok). Bot token'ı başka yoldan alıyorsa sorun yok.")
        db.create_bot(name, tg_bot_id=info["tg_bot_id"], tg_username=info["tg_username"], busy="deploying",
                      status="stopped", ram_limit_mb=config.DEFAULT_RAM_MB)
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
    db.enqueue("deploy", name, {"zip": str(zip_path), "token": token, "new": True, "msg_id": msg_id},
               requested_by=user_id, chat_id=chat_id)
    db.audit(user_id, "deploy", name, f"yeni bot, {len(meta['files'])} dosya")
    return warnings


def update_from_zip(user_id, name, zip_path: Path, chat_id=None, msg_id=None) -> list:
    try:
        bot = require_bot(name)
        if bot.get("busy"):
            raise UserError(f"Bot üzerinde işlem sürüyor: {bot['busy']}")
        meta = core.inspect_zip(zip_path)
        warnings = []
        if not (core.bot_dir(name) / ".env").exists():
            for tok in core.tokens_in_env(meta["env"]):
                warnings += core.check_token(tok, exclude_name=name)["warnings"]
        elif meta["env"]:
            warnings.append("Zip içindeki .env yok sayıldı; mevcut .env korunuyor.")
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
    db.enqueue("deploy", name, {"zip": str(zip_path), "msg_id": msg_id}, requested_by=user_id, chat_id=chat_id)
    db.audit(user_id, "deploy", name, f"güncelleme, {len(meta['files'])} dosya")
    return warnings


def save_single_file(user_id, name, filename: str, data: bytes, target: str = None) -> str:
    require_bot(name)
    rel = (target or filename or "").strip().lstrip("/")
    if not rel or not re.match(r"^[\w.\-/ ]+$", rel):
        raise UserError("Geçersiz dosya adı.")
    first = rel.split("/")[0]
    if first in config.SKIP_DIRS or first == "logs":
        raise UserError("Bu klasöre dosya yazılamaz.")
    path = core.safe_join(core.bot_dir(name), rel)
    if path.name == ".env":
        env = parse_env_text(data.decode("utf-8", "replace"))
        save_env(user_id, name, env)
        return ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.audit(user_id, "file", name, rel)
    return rel


def create_backup(user_id, name) -> str:
    require_bot(name)
    p = core.make_backup(name, "manual")
    db.audit(user_id, "backup", name, p.name)
    return p.name


def backup_by_index(name, idx):
    items = core.list_backups(name)
    if not 0 <= idx < len(items):
        raise UserError("Yedek bulunamadı (liste değişmiş olabilir).")
    return items[idx]["file"]


def queue_rollback(user_id, name, file, chat_id=None, msg_id=None) -> str:
    bot = require_bot(name)
    core.backup_file(name, file)
    if bot.get("busy"):
        raise UserError(f"Bot üzerinde işlem sürüyor: {bot['busy']}")
    db.enqueue("rollback", name, {"file": file, "msg_id": msg_id}, requested_by=user_id, chat_id=chat_id)
    db.audit(user_id, "rollback", name, file)
    return "⏳ Geri yükleme kuyruğa alındı."


def delete_backup(user_id, name, file):
    core.backup_file(name, file).unlink()
    db.audit(user_id, "backup", name, f"silindi: {file}")


def queue_delete(user_id, name, chat_id=None, msg_id=None):
    require_bot(name)
    db.enqueue("delete", name, {"msg_id": msg_id}, requested_by=user_id, chat_id=chat_id)
    db.audit(user_id, "delete", name)


def add_packages(user_id, name, text) -> list:
    require_bot(name)
    specs = [s for s in re.split(r"[\s,]+", text.strip()) if s]
    if not specs:
        raise UserError("Paket adı girin.")
    for s in specs:
        core.add_requirement(name, s)
    db.audit(user_id, "pkg", name, "+" + " ".join(specs))
    return specs


def remove_packages(user_id, name, text) -> list:
    require_bot(name)
    removed = [s for s in re.split(r"[\s,]+", text.strip()) if s and core.remove_requirement(name, s)]
    if not removed:
        raise UserError("requirements.txt içinde bu paket(ler) yok.")
    db.audit(user_id, "pkg", name, "-" + " ".join(removed))
    return removed


def set_setting(user_id, key, value):
    if key not in SETTING_RANGES:
        raise UserError("Geçersiz ayar.")
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise UserError("Tam sayı girin.")
    lo, hi = SETTING_RANGES[key]
    if not lo <= v <= hi:
        raise UserError(f"Değer {lo} - {hi} arasında olmalı.")
    db.set_setting(key, v)
    db.audit(user_id, "setting", None, f"{key}={v}")


def bot_summary(b) -> dict:
    return {
        "name": b["name"], "username": b.get("tg_username"), "status": b["status"],
        "status_text": core.STATUS_TEXT.get(b["status"], b["status"]), "busy": b.get("busy"),
        "icon": core.status_icon(b), "ram_limit_mb": b["ram_limit_mb"], "rss_mb": b.get("rss_mb"),
        "cpu_percent": b.get("cpu_percent"), "crash_count": b["crash_count"], "restart_count": b["restart_count"],
        "last_error": b.get("last_error"), "last_exit_code": b.get("last_exit_code"),
        "last_crash_at": b.get("last_crash_at"), "autostart": bool(b["autostart"]),
        "auto_restart": bool(b["auto_restart"]),
        "uptime": int(time.time() - b["started_at"]) if b.get("started_at") and b["status"] == "running" else None,
    }
