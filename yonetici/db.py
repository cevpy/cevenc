"""MySQL (PythonAnywhere) / SQLite (yerel test) veritabanı katmanı.

PythonAnywhere MySQL boşta kalan bağlantıları ~300 sn sonra kapattığı için
her işlem kendi kısa ömürlü bağlantısını açar.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

import config

MYSQL = config.DB_BACKEND == "mysql"
if MYSQL:
    import pymysql
    import pymysql.cursors

AUTO_ID = "BIGINT AUTO_INCREMENT PRIMARY KEY" if MYSQL else "INTEGER PRIMARY KEY AUTOINCREMENT"
TAIL = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if MYSQL else ""

SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS bots (
        name VARCHAR(32) PRIMARY KEY,
        tg_bot_id BIGINT NULL,
        tg_username VARCHAR(64) NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'stopped',
        desired_state VARCHAR(16) NOT NULL DEFAULT 'stopped',
        busy VARCHAR(32) NULL,
        pid INT NULL,
        started_at BIGINT NULL,
        ram_limit_mb INT NOT NULL DEFAULT {config.DEFAULT_RAM_MB},
        autostart TINYINT NOT NULL DEFAULT 1,
        auto_restart TINYINT NOT NULL DEFAULT 1,
        crash_count INT NOT NULL DEFAULT 0,
        restart_count INT NOT NULL DEFAULT 0,
        last_error TEXT NULL,
        last_exit_code INT NULL,
        last_crash_at BIGINT NULL,
        rss_mb DOUBLE NULL,
        cpu_percent DOUBLE NULL,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    ){TAIL}""",
    f"""CREATE TABLE IF NOT EXISTS commands (
        id {AUTO_ID},
        bot_name VARCHAR(32) NULL,
        action VARCHAR(32) NOT NULL,
        payload TEXT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        result TEXT NULL,
        requested_by BIGINT NULL,
        chat_id BIGINT NULL,
        created_at BIGINT NOT NULL,
        started_at BIGINT NULL,
        finished_at BIGINT NULL
    ){TAIL}""",
    f"""CREATE TABLE IF NOT EXISTS admins (
        user_id BIGINT PRIMARY KEY,
        username VARCHAR(64) NULL,
        added_by BIGINT NULL,
        added_at BIGINT NOT NULL
    ){TAIL}""",
    f"""CREATE TABLE IF NOT EXISTS settings (
        k VARCHAR(64) PRIMARY KEY,
        v TEXT NULL
    ){TAIL}""",
    f"""CREATE TABLE IF NOT EXISTS audit_log (
        id {AUTO_ID},
        user_id BIGINT NULL,
        action VARCHAR(48) NOT NULL,
        target VARCHAR(64) NULL,
        detail TEXT NULL,
        created_at BIGINT NOT NULL
    ){TAIL}""",
    f"""CREATE TABLE IF NOT EXISTS user_state (
        user_id BIGINT PRIMARY KEY,
        state VARCHAR(32) NOT NULL,
        data TEXT NULL,
        updated_at BIGINT NOT NULL
    ){TAIL}""",
]

INDEXES = [
    ("commands", "idx_commands_status", "status, id"),
    ("audit_log", "idx_audit_created", "created_at"),
]

DEFAULT_SETTINGS = {
    "cpu_warn_percent": "80",     # bu değerin üstünde cpu_window_sec boyunca → uyarı
    "cpu_kill_percent": "0",      # 0 = kapalı; üstünde cpu_window_sec boyunca → durdur
    "cpu_window_sec": "60",
    "ram_warn_percent": "90",     # RSS, RAM limitinin bu yüzdesini aşınca uyarı
    "crash_limit": "5",           # crash_window_sec içinde bu kadar çökme → durdur + bildir
    "crash_window_sec": "60",
    "notify_crashes": "1",        # her çökmede bildirim
}


def now() -> int:
    return int(time.time())


def _connect():
    if MYSQL:
        return pymysql.connect(
            host=config.MYSQL_HOST, user=config.MYSQL_USER, password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DB, charset="utf8mb4", autocommit=True,
            cursorclass=pymysql.cursors.DictCursor, connect_timeout=10,
        )
    conn = sqlite3.connect(str(config.SQLITE_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def connection():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _sql(sql: str) -> str:
    return sql if MYSQL else sql.replace("%s", "?")


def execute(sql, params=()):
    """INSERT/UPDATE/DELETE. (lastrowid, rowcount) döner."""
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(_sql(sql), tuple(params))
        return cur.lastrowid, cur.rowcount


def fetchall(sql, params=()) -> list:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(_sql(sql), tuple(params))
        return [dict(r) for r in cur.fetchall()]


def fetchone(sql, params=()):
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def upsert(table: str, keys: dict, values: dict):
    data = {**keys, **values}
    cols = ", ".join(data)
    marks = ", ".join(["%s"] * len(data))
    if MYSQL:
        upd = ", ".join(f"{c}=VALUES({c})" for c in values)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON DUPLICATE KEY UPDATE {upd}"
    else:
        upd = ", ".join(f"{c}=excluded.{c}" for c in values)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({', '.join(keys)}) DO UPDATE SET {upd}"
    execute(sql, data.values())


def init_db():
    with connection() as conn:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        for table, name, cols in INDEXES:
            if MYSQL:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.statistics "
                    "WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s", (table, name))
                if cur.fetchone()["n"]:
                    continue
                cur.execute(f"CREATE INDEX {name} ON {table} ({cols})")
            else:
                cur.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")
    for k, v in DEFAULT_SETTINGS.items():
        if get_setting(k) is None:
            set_setting(k, v)
    for uid in config.OWNER_IDS:
        if not fetchone("SELECT user_id FROM admins WHERE user_id=%s", (uid,)):
            add_admin(uid, None, None)


# ---------------------------------------------------------------- settings
def get_setting(k, default=None):
    row = fetchone("SELECT v FROM settings WHERE k=%s", (k,))
    return row["v"] if row else default


def get_int_setting(k, default=0) -> int:
    try:
        return int(float(get_setting(k, default)))
    except (TypeError, ValueError):
        return default


def set_setting(k, v):
    upsert("settings", {"k": k}, {"v": None if v is None else str(v)})


def get_json_setting(k, default=None):
    raw = get_setting(k)
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


# ---------------------------------------------------------------- bots
BOT_UPDATABLE = {
    "tg_bot_id", "tg_username", "status", "desired_state", "busy", "pid", "started_at",
    "ram_limit_mb", "autostart", "auto_restart", "crash_count", "restart_count", "last_error",
    "last_exit_code", "last_crash_at", "rss_mb", "cpu_percent",
}


def get_bot(name):
    return fetchone("SELECT * FROM bots WHERE name=%s", (name,))


def list_bots():
    return fetchall("SELECT * FROM bots ORDER BY name")


def create_bot(name, **fields):
    t = now()
    data = {"name": name, "created_at": t, "updated_at": t}
    data.update({k: v for k, v in fields.items() if k in BOT_UPDATABLE})
    cols = ", ".join(data)
    execute(f"INSERT INTO bots ({cols}) VALUES ({', '.join(['%s'] * len(data))})", data.values())


def update_bot(name, **fields):
    bad = set(fields) - BOT_UPDATABLE
    if bad:
        raise ValueError(f"Bilinmeyen alan(lar): {bad}")
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=%s" for k in fields)
    execute(f"UPDATE bots SET {sets} WHERE name=%s", [*fields.values(), name])


def delete_bot(name):
    execute("DELETE FROM bots WHERE name=%s", (name,))


def bot_by_tg_id(tg_bot_id):
    return fetchone("SELECT * FROM bots WHERE tg_bot_id=%s", (tg_bot_id,))


# ---------------------------------------------------------------- commands (kuyruk)
def enqueue(action, bot_name=None, payload=None, requested_by=None, chat_id=None) -> int:
    cid, _ = execute(
        "INSERT INTO commands (bot_name, action, payload, status, requested_by, chat_id, created_at) "
        "VALUES (%s, %s, %s, 'pending', %s, %s, %s)",
        (bot_name, action, json.dumps(payload or {}), requested_by, chat_id, now()),
    )
    return cid


def claim_pending(limit=20) -> list:
    """Bekleyen komutları alır ve 'running' olarak işaretler (tek supervisor varsayımı)."""
    rows = fetchall("SELECT * FROM commands WHERE status='pending' ORDER BY id LIMIT %s", (limit,))
    claimed = []
    for r in rows:
        _, n = execute("UPDATE commands SET status='running', started_at=%s WHERE id=%s AND status='pending'",
                       (now(), r["id"]))
        if n:
            r["payload"] = json.loads(r["payload"] or "{}")
            claimed.append(r)
    return claimed


def finish_command(cid, ok: bool, result: str = ""):
    execute("UPDATE commands SET status=%s, result=%s, finished_at=%s WHERE id=%s",
            ("done" if ok else "error", (result or "")[:4000], now(), cid))


def pending_for_bot(name) -> list:
    return fetchall("SELECT * FROM commands WHERE bot_name=%s AND status IN ('pending','running') ORDER BY id",
                    (name,))


def fail_stale_running():
    """Supervisor yeniden başladığında yarıda kalmış komutları kapatır."""
    execute("UPDATE commands SET status='error', result='Supervisor yeniden başladı', finished_at=%s "
            "WHERE status='running'", (now(),))


def cleanup_commands(days=7):
    execute("DELETE FROM commands WHERE status IN ('done','error') AND created_at < %s", (now() - days * 86400,))


# ---------------------------------------------------------------- admins
def is_admin(user_id) -> bool:
    if user_id in config.OWNER_IDS:
        return True
    return fetchone("SELECT user_id FROM admins WHERE user_id=%s", (user_id,)) is not None


def list_admins():
    return fetchall("SELECT * FROM admins ORDER BY added_at")


def admin_ids() -> list:
    ids = {r["user_id"] for r in list_admins()} | set(config.OWNER_IDS)
    return sorted(ids)


def add_admin(user_id, username, added_by):
    upsert("admins", {"user_id": user_id}, {"username": username, "added_by": added_by, "added_at": now()})


def remove_admin(user_id):
    execute("DELETE FROM admins WHERE user_id=%s", (user_id,))


# ---------------------------------------------------------------- audit
def audit(user_id, action, target=None, detail=None):
    execute("INSERT INTO audit_log (user_id, action, target, detail, created_at) VALUES (%s,%s,%s,%s,%s)",
            (user_id, action, target, (detail or "")[:1000] or None, now()))


def audit_page(offset, limit):
    rows = fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset))
    total = fetchone("SELECT COUNT(*) AS n FROM audit_log")["n"]
    return rows, total


# ---------------------------------------------------------------- konuşma durumu
def set_state(user_id, state, **data):
    upsert("user_state", {"user_id": user_id}, {"state": state, "data": json.dumps(data), "updated_at": now()})


def get_state(user_id):
    row = fetchone("SELECT * FROM user_state WHERE user_id=%s", (user_id,))
    if not row or row["updated_at"] < now() - 3600:
        return None, {}
    return row["state"], json.loads(row["data"] or "{}")


def clear_state(user_id):
    execute("DELETE FROM user_state WHERE user_id=%s", (user_id,))
