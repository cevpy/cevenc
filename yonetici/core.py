"""Web app ve supervisor'ın ortak kullandığı yardımcılar."""
import hashlib
import html
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import requests

import config
import db
from envfile import parse_env_text, read_env

NAME_RE = re.compile(r"^[a-z0-9_]{2,32}$")
TOKEN_RE = re.compile(r"^(\d{5,16}):[A-Za-z0-9_-]{30,}$")
TOKEN_KEYS = ("BOT_TOKEN", "TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "API_TOKEN")
ERROR_RE = re.compile(r"Traceback|Error|Exception|CRITICAL|FATAL|\bERROR\b", re.I)


class UserError(Exception):
    """Kullanıcıya olduğu gibi gösterilebilecek hata."""


# ---------------------------------------------------------------- isim / yollar
def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise UserError("Bot adı 2-32 karakter olmalı ve sadece a-z, 0-9, _ içermeli.")
    return name


def bot_dir(name) -> Path:
    return config.BOTS_DIR / validate_name(name)


def venv_python(name) -> Path:
    return bot_dir(name) / "venv" / "bin" / "python"


def log_path(name) -> Path:
    return bot_dir(name) / "logs" / "bot.log"


def install_log_path(name) -> Path:
    return bot_dir(name) / "logs" / "install.log"


def backup_dir(name) -> Path:
    return config.BACKUPS_DIR / validate_name(name)


def safe_join(base: Path, rel: str) -> Path:
    """rel yolunu base içinde tutar; dışarı çıkmaya çalışırsa hata verir."""
    base = base.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise UserError("Geçersiz dosya yolu.")
    return target


def ensure_dirs():
    for d in (config.BOTS_DIR, config.BACKUPS_DIR, config.UPLOADS_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- Telegram API
def tg_api(token, method, timeout=15, files=None, **params):
    """Ham Bot API çağrısı. Her zaman {'ok': bool, ...} sözlüğü döner."""
    import json as _json
    data = {k: (_json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items() if v is not None}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=timeout)
        return r.json()
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error_code": 0, "description": f"Ağ hatası: {e}"}


def token_bot_id(token):
    m = TOKEN_RE.match((token or "").strip())
    return int(m.group(1)) if m else None


def check_token(token: str, exclude_name=None, online=True) -> dict:
    """Token'ı doğrular. Hata varsa UserError fırlatır; bilgi + uyarılar döner."""
    token = (token or "").strip()
    bot_id = token_bot_id(token)
    if not bot_id:
        raise UserError("Token biçimi geçersiz (123456:ABC... olmalı).")
    if bot_id == token_bot_id(config.MAIN_BOT_TOKEN):
        raise UserError("Yönetici botun token'ı alt bot olarak kullanılamaz.")
    other = db.bot_by_tg_id(bot_id)
    if other and other["name"] != exclude_name:
        raise UserError(f"Bu token zaten <b>{html.escape(other['name'])}</b> botunda kullanılıyor.")
    info = {"tg_bot_id": bot_id, "tg_username": None, "warnings": []}
    if not online:
        return info
    me = tg_api(token, "getMe")
    if not me.get("ok"):
        if me.get("error_code") == 401:
            raise UserError("Token geçersiz (401 Unauthorized).")
        info["warnings"].append(f"getMe başarısız: {me.get('description')}")
        return info
    info["tg_username"] = me["result"].get("username")
    own_running = exclude_name and (db.get_bot(exclude_name) or {}).get("status") == "running"
    if not own_running:
        # offset verilmez: bekleyen güncellemeler onaylanmaz/silinmez
        upd = tg_api(token, "getUpdates", limit=1)
        if upd.get("error_code") == 409:
            desc = upd.get("description", "")
            if "webhook" in desc.lower():
                info["warnings"].append("Bu bot için webhook ayarlı; polling kullanacaksa bot deleteWebhook çağırmalı.")
            else:
                raise UserError("409 Conflict: Bu token başka bir yerde zaten çalışıyor (getUpdates). "
                                "Önce diğer kopyayı durdurun.")
    return info


def tokens_in_env(env: dict) -> list:
    return [v for k, v in env.items() if (k.upper() in TOKEN_KEYS or "TOKEN" in k.upper()) and TOKEN_RE.match(v)]


# ---------------------------------------------------------------- zip (zip slip korumalı)
def _zip_members(zf: zipfile.ZipFile):
    """Güvenli üyeleri (arcname, info) olarak döner, tehlikelileri reddeder."""
    total = 0
    infos = zf.infolist()
    if len(infos) > config.MAX_ZIP_FILES:
        raise UserError(f"Zip içinde çok fazla dosya var (>{config.MAX_ZIP_FILES}).")
    out = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        p = PurePosixPath(name)
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in p.parts:
            raise UserError(f"Zip içinde güvensiz yol: {html.escape(info.filename)}")
        mode = (info.external_attr >> 16) & 0o177777
        if stat.S_ISLNK(mode):
            raise UserError(f"Zip içinde sembolik bağlantı var: {html.escape(info.filename)}")
        if info.flag_bits & 0x1:
            raise UserError("Şifreli zip desteklenmiyor.")
        total += info.file_size
        if total > config.MAX_UNZIPPED_BYTES:
            raise UserError("Zip açıldığında boyut sınırını aşıyor (zip bombası koruması).")
        parts = [x for x in p.parts if x not in ("", ".")]
        if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
            continue
        if any(x in config.SKIP_DIRS for x in parts):
            continue
        out.append(("/".join(parts), info))
    return out


def inspect_zip(zip_path) -> dict:
    """Zip'i açmadan denetler. {'files': [...], 'prefix': str, 'env': dict} döner."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise UserError("Geçerli bir zip dosyası değil.")
    with zf:
        members = _zip_members(zf)
        files = [n for n, i in members if not i.is_dir()]
        prefix = ""
        tops = {n.split("/")[0] for n in files}
        if "main.py" not in files and len(tops) == 1 and all("/" in n for n in files):
            prefix = tops.pop() + "/"
        if prefix + "main.py" not in files:
            raise UserError("Zip içinde main.py bulunamadı (kökte veya tek bir üst klasörde olmalı).")
        env = {}
        if prefix + ".env" in files:
            env = parse_env_text(zf.read(prefix + ".env").decode("utf-8", "replace"))
        return {"files": [f[len(prefix):] for f in files], "prefix": prefix, "env": env}


def safe_extract(zip_path, dest: Path) -> list:
    meta = inspect_zip(zip_path)
    prefix = meta["prefix"]
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    with zipfile.ZipFile(zip_path) as zf:
        for name, info in _zip_members(zf):
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel:
                continue
            target = safe_join(dest, rel)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(rel)
    return written


# ---------------------------------------------------------------- yedekler
def _backup_walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in config.SKIP_DIRS
                       and not (rel_dir == Path(".") and d in ("logs", "data"))]
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_file() and not p.is_symlink():
                yield p, p.relative_to(root)


def make_backup(name, reason="manual") -> Path:
    """Kod + .env + requirements yedeklenir; venv/, logs/, data/ hariç."""
    src = bot_dir(name)
    if not src.is_dir():
        raise UserError("Bot klasörü yok.")
    bdir = backup_dir(name)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    reason = re.sub(r"[^a-z0-9_]", "", reason.lower())[:16] or "manual"
    path = bdir / f"{stamp}_{reason}.zip"
    n = 1
    while path.exists():
        path = bdir / f"{stamp}_{reason}_{n}.zip"
        n += 1
    tmp = path.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, rel in _backup_walk(src):
            zf.write(p, rel.as_posix())
    tmp.replace(path)
    prune_backups(name)
    return path


def list_backups(name) -> list:
    bdir = backup_dir(name)
    if not bdir.is_dir():
        return []
    items = sorted(bdir.glob("*.zip"), key=lambda p: p.name, reverse=True)
    return [{"file": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)} for p in items]


def prune_backups(name, keep=None):
    keep = keep or config.MAX_BACKUPS_PER_BOT
    for b in list_backups(name)[keep:]:
        (backup_dir(name) / b["file"]).unlink(missing_ok=True)


def backup_file(name, file) -> Path:
    if not re.match(r"^[\w.-]+\.zip$", file or ""):
        raise UserError("Geçersiz yedek adı.")
    p = backup_dir(name) / file
    if not p.is_file():
        raise UserError("Yedek bulunamadı.")
    return p


def replace_code(name, src_dir: Path):
    """Bot klasöründeki kodu src_dir ile değiştirir; venv/, logs/, data/ ve (varsa) .env korunur."""
    dest = bot_dir(name)
    dest.mkdir(parents=True, exist_ok=True)
    keep = {"venv", ".venv", "logs", "data", ".env"}
    for child in dest.iterdir():
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in Path(src_dir).iterdir():
        if child.name in ("venv", ".venv", "logs"):
            continue
        target = dest / child.name
        if child.name == ".env" and target.exists():
            continue
        if child.name == "data" and target.exists():
            continue
        shutil.move(str(child), str(target))
    for d in ("logs", "data"):
        (dest / d).mkdir(exist_ok=True)


def extract_to_temp(zip_path) -> Path:
    config.BOTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp_", dir=config.BOTS_DIR))
    try:
        safe_extract(zip_path, tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp


# ---------------------------------------------------------------- loglar
def rotate_log(path: Path, max_bytes=None, keep=None) -> bool:
    """copytruncate yöntemi: süreç dosyayı O_APPEND ile tuttuğu için güvenli."""
    max_bytes = max_bytes or config.LOG_MAX_BYTES
    keep = keep or config.LOG_KEEP
    try:
        if path.stat().st_size < max_bytes:
            return False
    except FileNotFoundError:
        return False
    for i in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{i + 1}"))
    shutil.copyfile(path, path.with_name(f"{path.name}.1"))
    with open(path, "r+b") as f:
        f.truncate(0)
    return True


def tail_lines(path: Path, n=50, max_bytes=256 * 1024) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except FileNotFoundError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines[-n:] if n else lines


def filter_errors(lines: list) -> list:
    """Hata satırlarını ve traceback bloklarını seçer."""
    out, in_tb = [], False
    for line in lines:
        if line.startswith("Traceback"):
            in_tb = True
        if in_tb or ERROR_RE.search(line):
            out.append(line)
        if in_tb and line and not line.startswith((" ", "Traceback")):
            in_tb = False
    return out


def extract_last_error(name) -> str:
    lines = tail_lines(log_path(name), 200)
    tb_start = max((i for i, l in enumerate(lines) if l.startswith("Traceback")), default=None)
    if tb_start is not None:
        block = lines[tb_start:]
        end = next((i for i, l in enumerate(block[1:], 1) if l and not l.startswith(" ")), len(block) - 1)
        return "\n".join(block[:end + 1])[-1500:]
    errs = filter_errors(lines)
    if errs:
        return "\n".join(errs[-5:])[-1500:]
    return "\n".join(lines[-3:])[-500:]


def log_files(name) -> list:
    d = bot_dir(name) / "logs"
    return sorted(p for p in d.glob("*.log*") if p.is_file()) if d.is_dir() else []


# ---------------------------------------------------------------- dosyalar / paketler
def list_files(name) -> list:
    root = bot_dir(name)
    out = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in config.SKIP_DIRS)
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            try:
                out.append({"path": p.relative_to(root).as_posix(), "size": p.stat().st_size})
            except OSError:
                pass
    return out


def installed_packages(name) -> list:
    site = sorted((bot_dir(name) / "venv" / "lib").glob("python*/site-packages"))
    if not site:
        return []
    pkgs = []
    for d in site[-1].glob("*.dist-info"):
        base = d.name[:-len(".dist-info")]
        pkg, _, ver = base.rpartition("-")
        pkgs.append((pkg, ver))
    return sorted(pkgs, key=lambda x: x[0].lower())


def requirements(name) -> list:
    p = bot_dir(name) / "requirements.txt"
    if not p.is_file():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
            if l.strip() and not l.strip().startswith("#")]


PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],]*([<>=!~]=?[A-Za-z0-9.*+!\-]+(,[<>=!~]=?[A-Za-z0-9.*+!\-]+)*)?$")


def _req_name(line):
    return re.split(r"[<>=!~\[; ]", line, 1)[0].strip().lower().replace("_", "-")


def add_requirement(name, spec: str):
    spec = spec.strip()
    if not PKG_RE.match(spec) or spec.startswith("-"):
        raise UserError("Geçersiz paket tanımı. Örnek: <code>requests</code> veya <code>aiogram==3.4.1</code>")
    reqs = [r for r in requirements(name) if _req_name(r) != _req_name(spec)]
    reqs.append(spec)
    (bot_dir(name) / "requirements.txt").write_text("\n".join(reqs) + "\n", encoding="utf-8")


def remove_requirement(name, pkg: str) -> bool:
    reqs = requirements(name)
    new = [r for r in reqs if _req_name(r) != _req_name(pkg)]
    (bot_dir(name) / "requirements.txt").write_text("\n".join(new) + ("\n" if new else ""), encoding="utf-8")
    return len(new) != len(reqs)


def requirements_hash(name) -> str:
    p = bot_dir(name) / "requirements.txt"
    data = p.read_bytes() if p.is_file() else b""
    return hashlib.sha256(data + config.BOT_PYTHON.encode()).hexdigest()


def env_of(name) -> dict:
    return read_env(bot_dir(name) / ".env")


def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------- biçimlendirme
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(sec) -> str:
    sec = int(sec or 0)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return f"{d}g {h}sa"
    if h:
        return f"{h}sa {m}dk"
    if m:
        return f"{m}dk {s}sn"
    return f"{s}sn"


def fmt_time(ts) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "-"


def bar(pct, width=10) -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    full = round(pct / 100 * width)
    return "█" * full + "░" * (width - full)


STATUS_ICONS = {
    "running": "🟢", "stopped": "⚪️", "starting": "🟡", "restarting": "🟠", "crashed": "🔴",
    "crashloop": "⛔️", "error": "❌", "installing": "📦", "deploying": "📦", "stopping": "🟡",
}
STATUS_TEXT = {
    "running": "Çalışıyor", "stopped": "Durduruldu", "starting": "Başlatılıyor", "restarting": "Yeniden başlatılacak",
    "crashed": "Çöktü", "crashloop": "Çökme döngüsü — durduruldu", "error": "Hata", "installing": "Kurulum",
    "deploying": "Yükleniyor", "stopping": "Durduruluyor",
}


def status_icon(bot) -> str:
    if bot.get("busy"):
        return "⏳"
    return STATUS_ICONS.get(bot["status"], "❔")
