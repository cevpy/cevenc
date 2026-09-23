"""Ana bot (webhook) ve Mini App API testleri — Telegram API taklit edilir."""
import hashlib
import hmac
import io
import json
import time
import zipfile
from urllib.parse import urlencode

import pytest

import config
import core
import db

TOKEN = "999999:" + "M" * 35
ADMIN = 111
STRANGER = 222


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "MAIN_BOT_TOKEN", TOKEN)
    import calisan_bot
    import telebot.apihelper as ah

    calls = []
    counter = {"id": 1000}

    def fake_request(token, method_url, method="get", params=None, files=None, **kw):
        method = method_url
        calls.append((method, dict(params or {})))
        if method in ("sendMessage", "sendDocument", "editMessageText"):
            counter["id"] += 1
            p = params or {}
            return {"message_id": p.get("message_id", counter["id"]), "date": int(time.time()),
                    "chat": {"id": p.get("chat_id", ADMIN), "type": "private"}, "text": p.get("text", "")}
        if method == "getFile":
            return {"file_id": "f", "file_unique_id": "u", "file_path": "documents/x.zip"}
        return True

    raw = []

    def fake_tg_api(token, method, timeout=15, files=None, **params):
        raw.append((method, params))
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "username": "alt_test_bot"}}
        return {"ok": True, "result": []}

    monkeypatch.setattr(ah, "_make_request", fake_request)
    monkeypatch.setattr(core, "tg_api", fake_tg_api)
    client = calisan_bot.app.test_client()
    calisan_bot._admin_cache.clear()

    class E:
        pass
    e = E()
    e.calls, e.raw, e.client, e.bot = calls, raw, client, calisan_bot
    e.upd_id = 0

    def post(update, secret=config.WEBHOOK_SECRET):
        e.upd_id += 1
        update["update_id"] = e.upd_id
        return client.post(config.WEBHOOK_PATH, data=json.dumps(update),
                           headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"})

    def msg(text=None, uid=ADMIN, **extra):
        m = {"message_id": 5000 + e.upd_id, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
             "from": {"id": uid, "is_bot": False, "first_name": "T"}}
        if text is not None:
            m["text"] = text
            if text.startswith("/"):
                m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        m.update(extra)
        return post({"message": m})

    def cb(data, uid=ADMIN, msg_id=777):
        return post({"callback_query": {"id": "cq", "chat_instance": "ci", "data": data,
                                        "from": {"id": uid, "is_bot": False, "first_name": "T"},
                                        "message": {"message_id": msg_id, "date": 0,
                                                    "chat": {"id": uid, "type": "private"}, "text": "x"}}})

    e.post, e.msg, e.cb = post, msg, cb
    e.sent = lambda m: [p for (meth, p) in calls if meth == m]
    return e


def test_webhook_requires_secret(env):
    assert env.post({"message": {}}, secret="yanlis").status_code == 403
    assert env.post({"message": {}}, secret="").status_code == 403


def test_stranger_rejected(env):
    env.msg("/start", uid=STRANGER)
    assert "özeldir" in env.sent("sendMessage")[-1]["text"]
    env.cb("m", uid=STRANGER)
    assert env.sent("answerCallbackQuery")[-1].get("show_alert")


def test_admin_menu_and_navigation(env):
    env.msg("/panel")
    text = env.sent("sendMessage")[-1]["text"]
    assert "Bot Yöneticisi" in text
    markup = json.loads(env.sent("sendMessage")[-1]["reply_markup"])
    assert any("web_app" in b for row in markup["inline_keyboard"] for b in row)
    for data in ("l:0", "s", "sa", "st", "h:0", "n", "x"):
        env.cb(data)
    assert len(env.sent("editMessageText")) >= 6


def make_zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", "print('hi')\n")
        zf.writestr("requirements.txt", "")
    return buf.getvalue()


def test_new_bot_flow_queues_deploy(env, monkeypatch):
    for n in ("yenibot",):
        if db.get_bot(n):
            db.delete_bot(n)
    env.msg("/yeni")
    env.msg("Kotu-Ad")                                    # geçersiz ad → hata, durum korunur
    assert db.get_state(ADMIN)[0] == "new_name"
    env.msg("yenibot")
    assert db.get_state(ADMIN)[0] == "new_token"
    env.msg(TOKEN)                                        # ana botun token'ı yasak
    assert db.get_state(ADMIN)[0] == "new_token"
    tok = "555555:" + "Z" * 35
    env.msg(tok)
    state, data = db.get_state(ADMIN)
    assert state == "new_zip" and data["token"] == tok
    assert any(m == "deleteMessage" for m, _ in env.calls)  # token mesajı silindi
    monkeypatch.setattr(env.bot.bot, "download_file", lambda path: make_zip_bytes())
    env.msg(None, document={"file_id": "f", "file_unique_id": "u", "file_name": "bot.zip", "file_size": 200})
    row = db.get_bot("yenibot")
    assert row and row["tg_bot_id"] == 555555 and row["busy"] == "deploying"
    cmd = db.fetchone("SELECT * FROM commands WHERE bot_name='yenibot' ORDER BY id DESC")
    payload = json.loads(cmd["payload"])
    assert cmd["action"] == "deploy" and payload["new"] and payload["token"] == tok and payload["msg_id"]
    assert (config.UPLOADS_DIR / payload["zip"].split("/")[-1]).exists()
    assert any(m == "setMessageReaction" for m, _ in env.raw)
    # aynı token başka bota verilemez
    from core import UserError
    with pytest.raises(UserError):
        core.check_token(tok, online=False)
    db.delete_bot("yenibot")


def _mkbot(name):
    if db.get_bot(name):
        db.delete_bot(name)
    db.create_bot(name)
    d = core.bot_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text("print(1)")
    return d


def test_actions_env_ram_packages(env):
    d = _mkbot("panelbot")
    env.cb("b:panelbot")
    env.cb("a:panelbot:start")
    cmd = db.fetchone("SELECT * FROM commands WHERE bot_name='panelbot' ORDER BY id DESC")
    assert cmd["action"] == "start" and json.loads(cmd["payload"])["msg_id"] == 777
    env.cb("ram:panelbot:512")
    assert db.get_bot("panelbot")["ram_limit_mb"] == 512
    env.cb("tg:panelbot:autostart")
    assert db.get_bot("panelbot")["autostart"] == 0
    env.cb("ramc:panelbot")
    env.msg("5")                                           # sınır dışı
    assert db.get_bot("panelbot")["ram_limit_mb"] == 512
    env.msg("300")
    assert db.get_bot("panelbot")["ram_limit_mb"] == 300
    env.cb("ee:panelbot")
    env.msg("API_KEY=abc 123\nDEBUG=1")
    assert core.env_of("panelbot") == {"API_KEY": "abc 123", "DEBUG": "1"}
    env.cb("ee:panelbot")
    env.msg("-DEBUG")
    assert core.env_of("panelbot") == {"API_KEY": "abc 123"}
    assert oct((d / ".env").stat().st_mode)[-3:] == "600"
    env.cb("pa:panelbot")
    env.msg("requests aiogram==3.4.1")
    assert core.requirements("panelbot") == ["requests", "aiogram==3.4.1"]
    env.cb("fu:panelbot")
    env.cb("b:panelbot")                                   # başka düğme → bekleyen durum temizlenir
    assert db.get_state(ADMIN)[0] is None
    for data in ("lg:panelbot:a", "lg:panelbot:e", "lg:panelbot:i", "f:panelbot:0", "e:panelbot:1",
                 "p:panelbot", "k:panelbot", "kc:panelbot", "r:panelbot", "d:panelbot"):
        env.cb(data)
    assert core.list_backups("panelbot")
    alerts = [p for p in env.sent("answerCallbackQuery") if p.get("show_alert")]
    assert not alerts, alerts


def test_single_file_upload_blocks_traversal(env, monkeypatch):
    d = _mkbot("filebot")
    monkeypatch.setattr(env.bot.bot, "download_file", lambda path: b"x = 1\n")
    env.cb("fu:filebot")
    env.msg(None, document={"file_id": "f", "file_unique_id": "u", "file_name": "yardim.py", "file_size": 6},
            caption="handlers/yardim.py")
    assert (d / "handlers" / "yardim.py").read_bytes() == b"x = 1\n"
    env.cb("fu:filebot")
    env.msg(None, document={"file_id": "f", "file_unique_id": "u", "file_name": "x.py", "file_size": 6},
            caption="../../evil.py")
    assert not (config.BOTS_DIR / "evil.py").exists()
    env.cb("fu:filebot")
    env.msg(None, document={"file_id": "f", "file_unique_id": "u", "file_name": "x.py", "file_size": 6},
            caption="venv/bin/python")
    assert not (d / "venv").exists()


def test_admin_add_remove(env):
    env.cb("sad")
    env.msg("333")
    assert db.is_admin(333)
    assert any(m == "setMyCommands" and p["scope"]["chat_id"] == 333 for m, p in env.raw)
    env.cb("sr:333")
    assert not db.is_admin(333)
    env.cb(f"sr:{ADMIN}")                                  # sahip kaldırılamaz
    assert db.is_admin(ADMIN)


# ---------------------------------------------------------------- Mini App
def init_data(uid, token=TOKEN, age=0, tamper=False):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAH",
              "user": json.dumps({"id": uid, "first_name": "T"}), "signature": "sig"}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": ADMIN, "first_name": "X"})
    return urlencode(fields)


def test_verify_init_data():
    import miniapp
    assert miniapp.verify_init_data(init_data(5), TOKEN)["id"] == 5
    assert miniapp.verify_init_data(init_data(5, tamper=True), TOKEN) is None
    assert miniapp.verify_init_data(init_data(5, token="1:other"), TOKEN) is None
    assert miniapp.verify_init_data(init_data(5, age=90000), TOKEN) is None
    assert miniapp.verify_init_data("", TOKEN) is None
    assert miniapp.verify_init_data("hash=abc", TOKEN) is None


def test_miniapp_api(env):
    _mkbot("appbot")
    c = env.client
    assert c.get("/app").status_code == 200
    assert c.get("/api/overview").status_code == 401
    assert c.get("/api/overview", headers={"X-Telegram-Init-Data": init_data(STRANGER)}).status_code == 403
    h = {"X-Telegram-Init-Data": init_data(ADMIN)}
    r = c.get("/api/overview", headers=h)
    assert r.status_code == 200 and any(b["name"] == "appbot" for b in r.json["bots"])
    assert c.get("/api/bots/appbot", headers=h).json["bot"]["name"] == "appbot"
    assert c.get("/api/bots/..%2Fetc", headers=h).status_code in (400, 404)
    r = c.put("/api/bots/appbot/resources", headers=h, json={"ram_limit_mb": 128, "auto_restart": False})
    assert r.status_code == 200 and r.json["bot"]["ram_limit_mb"] == 128 and not r.json["bot"]["auto_restart"]
    assert c.put("/api/bots/appbot/resources", headers=h, json={"ram_limit_mb": 10}).status_code == 400
    r = c.put("/api/bots/appbot/env", headers=h, json={"env": {"A": "1", "B": "iki üç"}})
    assert r.status_code == 200 and c.get("/api/bots/appbot/env", headers=h).json["env"] == {"A": "1", "B": "iki üç"}
    assert c.put("/api/bots/appbot/env", headers=h, json={"env": {"1bad": "x"}}).status_code == 400
    r = c.post("/api/bots/appbot/action", headers=h, json={"action": "restart"})
    assert r.status_code == 200
    assert c.post("/api/bots/appbot/action", headers=h, json={"action": "rm -rf"}).status_code == 400
    assert c.get("/api/bots/appbot/logs?mode=e&n=abc", headers=h).status_code == 200
    assert c.put("/api/settings", headers=h, json={"crash_limit": 7}).status_code == 200
    assert db.get_setting("crash_limit") == "7"
    assert c.put("/api/settings", headers=h, json={"crash_limit": 999}).status_code == 400
    db.set_setting("crash_limit", 5)
    assert c.get("/api/history", headers=h).json["total"] > 0
