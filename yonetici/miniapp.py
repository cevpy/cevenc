"""Telegram Mini App paneli: /app sayfası ve /api/* uç noktaları.

Her API isteği `X-Telegram-Init-Data` başlığında Telegram'ın imzaladığı initData'yı taşır;
imza ana bot token'ı ile doğrulanır ve kullanıcı admin değilse istek reddedilir.
"""
import hashlib
import hmac
import json
import re
import time
from functools import wraps
from urllib.parse import parse_qsl

from flask import Blueprint, g, jsonify, request, send_file

import config
import core
import db
import services
import ui
from core import UserError

bp = Blueprint("miniapp", __name__)
INIT_DATA_MAX_AGE = 24 * 3600


def verify_init_data(init_data: str, bot_token: str, max_age=INIT_DATA_MAX_AGE):
    """Geçerliyse kullanıcı sözlüğünü, değilse None döner.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app"""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", 0)) > max_age:
            return None
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    return user if user.get("id") else None


def api_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), config.MAIN_BOT_TOKEN)
        if not user:
            return jsonify(error="Kimlik doğrulanamadı. Paneli Telegram içinden açın."), 401
        if not db.is_admin(user["id"]):
            return jsonify(error="Yetkiniz yok."), 403
        g.user_id = user["id"]
        try:
            return fn(*args, **kwargs)
        except UserError as e:
            return jsonify(error=ui_plain(str(e))), 400
    return wrapper


def ui_plain(s):
    return re.sub(r"<[^>]+>", "", s)


@bp.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.get(config.MINIAPP_PATH)
def page():
    return send_file(config.BASE_DIR / "templates" / "miniapp.html", mimetype="text/html")


@bp.get("/api/overview")
@api_auth
def overview():
    bots = db.list_bots()
    return jsonify(
        system=db.get_json_setting("sys_stats", {}) or {},
        supervisor_ok=services.supervisor_alive(),
        heartbeat=db.get_int_setting("supervisor_heartbeat", 0),
        counts=ui.bot_counts(bots),
        bots=[services.bot_summary(b) for b in bots],
    )


@bp.get("/api/bots/<name>")
@api_auth
def bot_detail(name):
    b = services.require_bot(name)
    env = core.env_of(name)
    return jsonify(
        bot=services.bot_summary(b),
        pending=[p["action"] for p in db.pending_for_bot(name)],
        env_keys=list(env.keys()),
        backups=core.list_backups(name),
        requirements=core.requirements(name),
        packages=len(core.installed_packages(name)),
        ram_choices=list(config.RAM_CHOICES_MB), max_ram=config.MAX_RAM_MB,
    )


@bp.post("/api/bots/<name>/action")
@api_auth
def bot_action(name):
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action == "delete":
        services.queue_delete(g.user_id, name)
        return jsonify(ok=True, message="Silme kuyruğa alındı.")
    if action == "backup":
        return jsonify(ok=True, message=f"Yedek alındı: {services.create_backup(g.user_id, name)}")
    if action == "rollback":
        return jsonify(ok=True, message=ui_plain(services.queue_rollback(g.user_id, name, body.get("file"))))
    return jsonify(ok=True, message=ui_plain(services.queue_action(g.user_id, name, action)))


@bp.get("/api/bots/<name>/logs")
@api_auth
def bot_logs(name):
    services.require_bot(name)
    mode = request.args.get("mode", "a")
    n = min(max(request.args.get("n", 200, type=int), 1), 1000)
    if mode == "i":
        lines = core.tail_lines(core.install_log_path(name), n)
    elif mode == "e":
        lines = core.filter_errors(core.tail_lines(core.log_path(name), 5000))[-n:]
    else:
        lines = core.tail_lines(core.log_path(name), n)
    return jsonify(lines=lines)


@bp.get("/api/bots/<name>/env")
@api_auth
def env_get(name):
    services.require_bot(name)
    return jsonify(env=core.env_of(name))


@bp.put("/api/bots/<name>/env")
@api_auth
def env_put(name):
    body = request.get_json(silent=True) or {}
    env = body.get("env")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise UserError("Geçersiz .env verisi.")
    warnings = services.save_env(g.user_id, name, env)
    return jsonify(ok=True, warnings=warnings)


@bp.put("/api/bots/<name>/resources")
@api_auth
def resources_put(name):
    body = request.get_json(silent=True) or {}
    if "ram_limit_mb" in body:
        services.set_ram(g.user_id, name, body["ram_limit_mb"])
    for field in ("autostart", "auto_restart"):
        if field in body:
            services.toggle(g.user_id, name, field, bool(body[field]))
    return jsonify(ok=True, bot=services.bot_summary(db.get_bot(name)))


@bp.get("/api/history")
@api_auth
def history():
    rows, total = db.audit_page(0, 50)
    return jsonify(total=total, rows=rows)


@bp.get("/api/settings")
@api_auth
def settings_get():
    return jsonify(settings={k: db.get_setting(k) for k in services.SETTING_RANGES},
                   labels={k: v[0] for k, v in ui.SETTING_LABELS.items()},
                   ranges=services.SETTING_RANGES)


@bp.put("/api/settings")
@api_auth
def settings_put():
    body = request.get_json(silent=True) or {}
    for k, v in body.items():
        services.set_setting(g.user_id, k, v)
    return jsonify(ok=True)
