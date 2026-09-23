"""Ana yönetici bot: Flask + pyTelegramBotAPI, webhook ile çalışır.

WSGI dosyası (PythonAnywhere → Web → WSGI configuration file):
    import sys
    sys.path.insert(0, "/home/KULLANICI/cevenc/yonetici")
    from calisan_bot import app as application

Kurulum komutları (Bash konsolunda, yonetici/ klasöründe):
    python calisan_bot.py setup      # tabloları oluştur, webhook + komut menüsünü ayarla
    python calisan_bot.py polling    # yerel geliştirme (webhook yerine polling)
"""
import json
import logging
import re
import sys
import time

import telebot
from flask import Flask, abort, request
from telebot import types

import config
import core
import db
import services
import ui
from core import UserError, esc

log = logging.getLogger("calisan_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
bot = telebot.TeleBot(config.MAIN_BOT_TOKEN or "0:placeholder", threaded=False, parse_mode="HTML")

ADMIN_COMMANDS = [
    ("panel", "🛠 Yönetim paneli"),
    ("botlar", "🤖 Bot listesi"),
    ("yeni", "➕ Yeni bot ekle"),
    ("durum", "📌 Sabitlenmiş canlı durum mesajı"),
    ("log", "📜 /log bot_adi — son loglar"),
    ("iptal", "✖️ Devam eden işlemi iptal et"),
    ("yardim", "❓ Yardım"),
]
HELP = (
    "❓ <b>Yardım</b>\n\n"
    "• /panel — ana ekran (tek mesajda gezinme)\n"
    "• /yeni — yeni bot: isim → token → zip (main.py + requirements.txt)\n"
    "• /log <code>bot_adi</code> — son loglar\n"
    "• /durum — sabitlenmiş, kendini güncelleyen durum mesajı\n"
    "• 📱 Panel düğmesi — Mini App yönetim paneli\n\n"
    "Alt botlar <code>bots/&lt;ad&gt;/</code> altında kendi venv'leriyle, kendi klasörlerinde çalışır. "
    "Başlat/durdur gibi işlemler supervisor'a kuyrukla iletilir."
)


# ---------------------------------------------------------------- Telegram yardımcıları
def api(method, **params):
    r = core.tg_api(config.MAIN_BOT_TOKEN, method, **params)
    if not r.get("ok"):
        log.debug("%s başarısız: %s", method, r.get("description"))
    return r


def react(chat_id, message_id, emoji="👌"):
    api("setMessageReaction", chat_id=chat_id, message_id=message_id,
        reaction=[{"type": "emoji", "emoji": emoji}])


def send_screen(chat_id, screen):
    text, markup = screen
    return bot.send_message(chat_id, text, reply_markup=json.dumps(markup), disable_web_page_preview=True)


def edit_screen(chat_id, message_id, screen):
    text, markup = screen
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=json.dumps(markup),
                              disable_web_page_preview=True)
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" in e.description:
            return
        if "can't be edited" in e.description or "not found" in e.description:
            send_screen(chat_id, screen)
            return
        raise


def move_panel(user_id, chat_id, old_msg_id, screen):
    """Kullanıcı yazı/dosya gönderdikten sonra paneli en alta taşır (tek panel mesajı kalır).
    Devam eden bir konuşma durumu varsa yeni panel mesajının kimliği duruma yazılır."""
    if old_msg_id:
        try:
            bot.delete_message(chat_id, old_msg_id)
        except telebot.apihelper.ApiTelegramException:
            pass
    sent = send_screen(chat_id, screen)
    state, data = db.get_state(user_id)
    if state:
        data["panel"] = sent.message_id
        db.set_state(user_id, state, **data)
    return sent


def with_notice(screen, notice):
    text, markup = screen
    return f"{notice}\n\n{text}", markup


def sync_admin_ui(user_id=None, remove=False):
    """Admin'e özel komut menüsü + Mini App menü düğmesi."""
    ids = [user_id] if user_id else db.admin_ids()
    for uid in ids:
        scope = {"type": "chat", "chat_id": uid}
        if remove:
            api("deleteMyCommands", scope=scope)
            api("setChatMenuButton", chat_id=uid, menu_button={"type": "default"})
            continue
        api("setMyCommands", scope=scope, commands=[{"command": c, "description": d} for c, d in ADMIN_COMMANDS])
        api("setChatMenuButton", chat_id=uid, menu_button={
            "type": "web_app", "text": "Panel",
            "web_app": {"url": f"https://{config.WEB_HOST}{config.MINIAPP_PATH}"}})


# ---------------------------------------------------------------- yetki
_admin_cache = {}


def is_admin(user_id):
    hit = _admin_cache.get(user_id)
    if hit and time.time() - hit[1] < 30:
        return hit[0]
    ok = db.is_admin(user_id)
    _admin_cache[user_id] = (ok, time.time())
    return ok


def private_admin(message) -> bool:
    return message.chat.type == "private" and is_admin(message.from_user.id)


# ---------------------------------------------------------------- komutlar
@bot.message_handler(commands=["start", "panel"])
def cmd_panel(m):
    if not private_admin(m):
        bot.reply_to(m, f"⛔️ Bu bot özeldir.\nKullanıcı ID'niz: <code>{m.from_user.id}</code>")
        return
    db.clear_state(m.from_user.id)
    send_screen(m.chat.id, ui.main_menu())


@bot.message_handler(commands=["botlar"], func=private_admin)
def cmd_bots(m):
    send_screen(m.chat.id, ui.bot_list(0))


@bot.message_handler(commands=["yeni"], func=private_admin)
def cmd_new(m):
    sent = send_screen(m.chat.id, ui.prompt("➕ <b>Yeni bot</b>\n\nBot adını yazın (a-z, 0-9, _ ; 2-32 karakter):"))
    db.set_state(m.from_user.id, "new_name", panel=sent.message_id)


@bot.message_handler(commands=["yardim", "help"], func=private_admin)
def cmd_help(m):
    send_screen(m.chat.id, (HELP, ui.kb(ui.back("m", "🛠 Panel"))))


@bot.message_handler(commands=["iptal"], func=private_admin)
def cmd_cancel(m):
    db.clear_state(m.from_user.id)
    react(m.chat.id, m.message_id, "👌")
    send_screen(m.chat.id, ui.main_menu())


@bot.message_handler(commands=["log"], func=private_admin)
def cmd_log(m):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        send_screen(m.chat.id, ui.bot_list(0))
        return
    try:
        services.require_bot(parts[1].strip())
        send_screen(m.chat.id, ui.logs(parts[1].strip(), "a"))
    except UserError as e:
        bot.reply_to(m, f"❌ {e}")


@bot.message_handler(commands=["durum"], func=lambda m: is_admin(m.from_user.id))
def cmd_live(m):
    create_live_message(m.chat.id, m.from_user.id)


def create_live_message(chat_id, user_id):
    old = db.get_setting("live_message")
    if old:
        oc, om = old.split(":")
        api("unpinChatMessage", chat_id=int(oc), message_id=int(om))
    sent = send_screen(chat_id, ui.live_status())
    api("pinChatMessage", chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
    db.set_setting("live_message", f"{chat_id}:{sent.message_id}")
    db.audit(user_id, "setting", None, "canlı durum mesajı")


# ---------------------------------------------------------------- düğmeler
def _screen_for(data, user_id, chat_id, msg_id):
    """Salt görüntüleme düğmeleri. (ekran) döner ya da None."""
    parts = data.split(":")
    p = parts[0]
    arg = parts[1] if len(parts) > 1 else None
    arg2 = parts[2] if len(parts) > 2 else None
    if p == "m":
        return ui.main_menu()
    if p == "l":
        return ui.bot_list(int(arg or 0))
    if p == "b":
        return ui.bot_card(arg)
    if p == "r":
        return ui.resources(arg)
    if p == "lg":
        return ui.logs(arg, arg2 or "a")
    if p == "f":
        return ui.files(arg, int(arg2 or 0))
    if p == "e":
        return ui.env(arg, arg2 == "1")
    if p == "p":
        return ui.packages(arg)
    if p == "k":
        return ui.backups(arg)
    if p == "kr":
        return ui.backup_detail(arg, int(arg2))
    if p == "d":
        return ui.delete_confirm(arg)
    if p == "s":
        return ui.settings()
    if p == "sa":
        return ui.admins(user_id)
    if p == "st":
        return ui.thresholds()
    if p == "h":
        return ui.history(int(arg or 0))
    return None


PROMPTS = {
    # önek: (durum, metin şablonu)
    "ramc": ("ram_custom", "✏️ <b>{name}</b> için RAM limitini MB olarak yazın (64 - {max}):"),
    "ee": ("env_edit", "✏️ <b>{name}</b> — .env düzenle\n\nHer satıra bir tane:\n"
                       "<code>ANAHTAR=değer</code> → ekle/değiştir\n<code>-ANAHTAR</code> → sil\n\n"
                       "<i>Gönderdiğiniz mesaj güvenlik için silinecek.</i>"),
    "pa": ("pkg_add", "➕ <b>{name}</b> — eklenecek paket(ler)i yazın.\nÖrnek: <code>requests aiogram==3.4.1</code>\n"
                      "requirements.txt güncellenir ve kurulum kuyruğa alınır."),
    "pr": ("pkg_remove", "➖ <b>{name}</b> — kaldırılacak paket adlarını yazın.\n"
                         "venv temiz şekilde yeniden kurulur."),
    "fu": ("upload", "📤 <b>{name}</b> — dosya gönderin.\n\n• <b>.zip</b> → bot güncellenir (önce yedek alınır)\n"
                     "• <b>tek dosya</b> → bot klasörüne kaydedilir. Alt klasöre koymak için açıklamaya yol yazın "
                     "(ör. <code>handlers/admin.py</code>)."),
}


@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "⛔️ Yetkiniz yok.", show_alert=True)
        return
    data = c.data or ""
    chat_id, msg_id = c.message.chat.id, c.message.message_id
    toast, alert = None, False
    if data != "noop":
        db.clear_state(uid)  # başka bir düğmeye basıldı: yarım kalan yazı bekleme durumu iptal
    try:
        if data == "noop":
            pass
        elif data == "x":
            db.clear_state(uid)
            edit_screen(chat_id, msg_id, ui.main_menu())
            toast = "İptal edildi"
        elif data == "mnew":
            send_screen(chat_id, ui.main_menu())
        elif data == "liverf":
            edit_screen(chat_id, msg_id, ui.live_status())
        elif data == "live":
            create_live_message(chat_id, uid)
            toast = "📌 Canlı durum mesajı sabitlendi"
        elif data == "liveoff":
            old = db.get_setting("live_message")
            if old:
                oc, om = old.split(":")
                api("unpinChatMessage", chat_id=int(oc), message_id=int(om))
            db.set_setting("live_message", None)
            edit_screen(chat_id, msg_id, ui.settings())
        else:
            screen = _screen_for(data, uid, chat_id, msg_id)
            if screen is not None:
                edit_screen(chat_id, msg_id, screen)
            else:
                toast, alert = handle_action(c, data, uid, chat_id, msg_id)
    except UserError as e:
        toast, alert = str(e)[:190], True
    except (ValueError, IndexError):
        toast, alert = "Geçersiz istek.", True
    bot.answer_callback_query(c.id, _plain(toast) if toast else None, show_alert=alert)


def _plain(s):
    return re.sub(r"<[^>]+>", "", s)


def handle_action(c, data, uid, chat_id, msg_id):
    parts = data.split(":")
    p, name = parts[0], (parts[1] if len(parts) > 1 else None)
    arg = parts[2] if len(parts) > 2 else None

    if p == "a":  # start / stop / restart / install
        note = services.queue_action(uid, name, arg, chat_id=chat_id, msg_id=msg_id)
        edit_screen(chat_id, msg_id, ui.bot_card(name, notice=note))
        return "⏳ Kuyruğa alındı", False
    if p == "ram":
        services.set_ram(uid, name, arg)
        edit_screen(chat_id, msg_id, ui.resources(name))
        return f"RAM limiti {arg} MB", False
    if p == "tg":
        services.toggle(uid, name, arg)
        edit_screen(chat_id, msg_id, ui.resources(name))
        return None, False
    if p in PROMPTS:
        services.require_bot(name)
        state, tmpl = PROMPTS[p]
        db.set_state(uid, state, name=name, panel=msg_id)
        edit_screen(chat_id, msg_id, ui.prompt(tmpl.format(name=esc(name), max=config.MAX_RAM_MB), f"b:{name}"))
        return None, False
    if p == "ld":
        files = core.log_files(services.require_bot(name)["name"])
        if not files:
            return "Log dosyası yok.", True
        for f in files[:4]:
            if f.stat().st_size:
                with open(f, "rb") as fh:
                    bot.send_document(chat_id, fh, visible_file_name=f"{name}_{f.name}.txt",
                                      caption=f"📜 {esc(name)} — {f.name}")
        return None, False
    if p == "fd":
        services.require_bot(name)
        items = core.list_files(name)
        idx = int(arg)
        if idx >= len(items):
            return "Dosya bulunamadı (liste değişmiş olabilir).", True
        path = core.safe_join(core.bot_dir(name), items[idx]["path"])
        if path.stat().st_size > 50 * 1024 * 1024:
            return "Dosya 50 MB'tan büyük, Telegram ile gönderilemez.", True
        if path.name == ".env":
            return "Güvenlik için .env indirilemez; 🔑 .env ekranını kullanın.", True
        with open(path, "rb") as fh:
            bot.send_document(chat_id, fh, visible_file_name=path.name,
                              caption=f"📄 {esc(name)}/{esc(items[idx]['path'])}")
        return None, False
    if p == "kc":
        fname = services.create_backup(uid, name)
        edit_screen(chat_id, msg_id, with_notice(ui.backups(name), f"✅ Yedek alındı: <code>{esc(fname)}</code>"))
        return "Yedek alındı", False
    if p == "krc":
        file = services.backup_by_index(name, int(arg))
        note = services.queue_rollback(uid, name, file, chat_id=chat_id, msg_id=msg_id)
        edit_screen(chat_id, msg_id, ui.bot_card(name, notice=note))
        return "⏳ Geri yükleme kuyruğa alındı", False
    if p == "kd":
        file = services.backup_by_index(name, int(arg))
        services.delete_backup(uid, name, file)
        edit_screen(chat_id, msg_id, ui.backups(name))
        return "Yedek silindi", False
    if p == "kdl":
        file = services.backup_by_index(name, int(arg))
        with open(core.backup_file(name, file), "rb") as fh:
            bot.send_document(chat_id, fh, visible_file_name=f"{name}_{file}", caption=f"💾 {esc(name)} — {esc(file)}")
        return None, False
    if p == "dc":
        services.queue_delete(uid, name, chat_id=chat_id, msg_id=msg_id)
        edit_screen(chat_id, msg_id, ui.bot_card(name, notice="⏳ Silme kuyruğa alındı."))
        return "⏳ Silme kuyruğa alındı", False
    if p == "n":
        db.set_state(uid, "new_name", panel=msg_id)
        edit_screen(chat_id, msg_id, ui.prompt("➕ <b>Yeni bot</b>\n\nBot adını yazın (a-z, 0-9, _ ; 2-32 karakter):"))
        return None, False
    if p == "sad":
        db.set_state(uid, "admin_add", panel=msg_id)
        edit_screen(chat_id, msg_id, ui.prompt(
            "👤 Yeni adminin Telegram kullanıcı ID'sini yazın ya da ondan bir mesajı buraya iletin.\n"
            "<i>(Kişi ID'sini bu bota /start yazarak öğrenebilir.)</i>", "sa"))
        return None, False
    if p == "sr":
        target = int(name)
        if target in config.OWNER_IDS:
            return "Sahip hesap kaldırılamaz.", True
        if target == uid:
            return "Kendinizi kaldıramazsınız.", True
        db.remove_admin(target)
        _admin_cache.pop(target, None)
        db.audit(uid, "admin_remove", str(target))
        sync_admin_ui(target, remove=True)
        edit_screen(chat_id, msg_id, ui.admins(uid))
        return "Admin kaldırıldı", False
    if p == "sts":
        key = name
        if key not in services.SETTING_RANGES:
            return "Geçersiz ayar.", True
        label, unit = ui.SETTING_LABELS[key]
        lo, hi = services.SETTING_RANGES[key]
        db.set_state(uid, "setting", key=key, panel=msg_id)
        edit_screen(chat_id, msg_id, ui.prompt(
            f"📊 <b>{label}</b>\nŞu an: {esc(db.get_setting(key))}{unit}\n\nYeni değeri yazın ({lo} - {hi}):", "st"))
        return None, False
    return "Bilinmeyen düğme.", True


# ---------------------------------------------------------------- yazılı girdiler
@bot.message_handler(func=private_admin, content_types=["text"])
def on_text(m):
    uid, chat_id = m.from_user.id, m.chat.id
    state, data = db.get_state(uid)
    if not state:
        send_screen(chat_id, with_notice(ui.main_menu(), "ℹ️ Bir işlem seçin:"))
        return
    text = m.text.strip()
    name = data.get("name")
    panel = data.get("panel")
    secret = state in ("new_token", "env_edit")
    try:
        if state == "new_name":
            name = services.check_new_name(text.lower())
            db.set_state(uid, "new_token", name=name, panel=panel)
            screen = ui.prompt(f"🔑 <b>{esc(name)}</b> için bot token'ını gönderin.\n\n"
                               "Token zip içindeki .env dosyasındaysa <code>atla</code> yazın.\n"
                               "<i>Mesajınız güvenlik için silinecek.</i>")
        elif state == "new_token":
            token, head = None, ""
            if text.lower() != "atla":
                info = core.check_token(text)
                token = text
                uname = f" (@{esc(info['tg_username'])})" if info.get("tg_username") else ""
                head = f"✅ Token doğrulandı{uname}." + "".join(f"\n⚠️ {esc(x)}" for x in info["warnings"]) + "\n\n"
            db.set_state(uid, "new_zip", name=name, token=token, panel=panel)
            screen = ui.prompt(head + f"📦 Şimdi <b>{esc(name)}</b> için .zip dosyasını gönderin "
                                      f"(main.py + requirements.txt; en fazla {config.MAX_ZIP_BYTES >> 20} MB).")
        elif state == "ram_custom":
            services.set_ram(uid, name, text)
            db.clear_state(uid)
            screen = with_notice(ui.resources(name), f"✅ RAM limiti {esc(text)} MB")
        elif state == "env_edit":
            warnings = services.apply_env_text(uid, name, text)
            db.clear_state(uid)
            w = "".join(f"\n⚠️ {esc(x)}" for x in warnings)
            screen = with_notice(ui.env(name), f"✅ .env güncellendi. Yeniden başlatınca geçerli olur.{w}")
        elif state == "pkg_add":
            specs = services.add_packages(uid, name, text)
            note = services.queue_action(uid, name, "install", chat_id=chat_id)
            db.clear_state(uid)
            screen = with_notice(ui.packages(name), f"✅ Eklendi: <code>{esc(' '.join(specs))}</code>\n{note}")
        elif state == "pkg_remove":
            removed = services.remove_packages(uid, name, text)
            note = services.queue_action(uid, name, "install", chat_id=chat_id, recreate=True)
            db.clear_state(uid)
            screen = with_notice(ui.packages(name), f"✅ Kaldırıldı: <code>{esc(' '.join(removed))}</code>\n{note}")
        elif state == "admin_add":
            screen = _add_admin(uid, m, text)
        elif state == "setting":
            services.set_setting(uid, data["key"], text)
            db.clear_state(uid)
            screen = with_notice(ui.thresholds(), "✅ Kaydedildi")
        elif state in ("new_zip", "upload"):
            raise UserError("Lütfen bir dosya gönderin (veya İptal).")
        else:
            db.clear_state(uid)
            screen = ui.main_menu()
    except UserError as e:
        if secret:
            _delete(chat_id, m.message_id)
        else:
            react(chat_id, m.message_id, "👎")
        # durum korunur; kullanıcı tekrar deneyebilir
        cancel = f"b:{name}" if name and state not in ("new_name", "new_token") else "x"
        move_panel(uid, chat_id, panel, (f"❌ {e}\n\n<i>Tekrar deneyin veya iptal edin.</i>",
                                         ui.kb([ui.btn("✖️ İptal", cancel)])))
        return
    if secret:
        _delete(chat_id, m.message_id)
    else:
        react(chat_id, m.message_id, "👌")
    move_panel(uid, chat_id, panel, screen)


def _delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except telebot.apihelper.ApiTelegramException:
        pass


def _add_admin(uid, m, text):
    target, uname = None, None
    origin = getattr(m, "forward_origin", None)
    if origin is not None and getattr(origin, "sender_user", None):
        target, uname = origin.sender_user.id, origin.sender_user.username
    elif getattr(m, "forward_from", None):
        target, uname = m.forward_from.id, m.forward_from.username
    elif text.lstrip("-").isdigit():
        target = int(text)
    if not target or target < 0:
        raise UserError("Geçerli bir kullanıcı ID'si girin veya kişinin mesajını iletin "
                        "(gizlilik ayarı iletimi engelliyor olabilir).")
    db.add_admin(target, uname, uid)
    _admin_cache.pop(target, None)
    db.audit(uid, "admin_add", str(target), uname)
    db.clear_state(uid)
    sync_admin_ui(target)
    return with_notice(ui.admins(uid), f"✅ <code>{target}</code> admin yapıldı.")


# ---------------------------------------------------------------- dosyalar
@bot.message_handler(func=private_admin, content_types=["document"])
def on_document(m):
    uid, chat_id = m.from_user.id, m.chat.id
    state, data = db.get_state(uid)
    if state not in ("new_zip", "upload"):
        bot.reply_to(m, "ℹ️ Dosya yüklemek için önce bir botun 📁 Dosyalar → 📤 Yükle düğmesini "
                        "veya /yeni komutunu kullanın.")
        return
    name, panel = data["name"], data.get("panel")
    doc = m.document
    fname = doc.file_name or "dosya"
    try:
        if doc.file_size and doc.file_size > config.MAX_ZIP_BYTES:
            raise UserError(f"Dosya çok büyük (en fazla {config.MAX_ZIP_BYTES >> 20} MB).")
        react(chat_id, m.message_id, "✍")
        content = bot.download_file(bot.get_file(doc.file_id).file_path)
        is_zip = fname.lower().endswith(".zip")
        if state == "new_zip" and not is_zip:
            raise UserError("Yeni bot için .zip dosyası gönderin.")
        if is_zip:
            # Yeni panel mesajı: supervisor iş bitince bunu güncel bot kartıyla yeniler
            if panel:
                _delete(chat_id, panel)
            panel = bot.send_message(chat_id, "⏳ İşleniyor…").message_id
            path = services.new_upload_path(name)
            path.write_bytes(content)
        if state == "new_zip":
            warnings = services.create_bot_from_zip(uid, name, path, token=data.get("token"),
                                                    chat_id=chat_id, msg_id=panel)
            notice = "⏳ Kuruluyor: venv oluşturulacak, kütüphaneler yüklenecek ve bot başlatılacak."
        elif is_zip:
            warnings = services.update_from_zip(uid, name, path, chat_id=chat_id, msg_id=panel)
            notice = "⏳ Güncelleme kuyruğa alındı (önce yedek alınacak)."
        else:
            target = (m.caption or "").strip() or None
            saved = services.save_single_file(uid, name, fname, content, target)
            warnings = []
            notice = f"✅ Kaydedildi: <code>{esc(saved)}</code>\nDeğişikliğin geçerli olması için yeniden başlatın."
        db.clear_state(uid)
    except UserError as e:
        react(chat_id, m.message_id, "👎")
        move_panel(uid, chat_id, panel, (f"❌ {e}\n\n<i>Başka bir dosya gönderin veya iptal edin.</i>",
                                         ui.kb([ui.btn("✖️ İptal", "x")])))
        return
    react(chat_id, m.message_id, "👌")
    notice += "".join(f"\n⚠️ {esc(w)}" for w in warnings)
    if is_zip:
        edit_screen(chat_id, panel, ui.bot_card(name, notice=notice))
    else:
        move_panel(uid, chat_id, panel, with_notice(ui.files(name), notice))


@bot.message_handler(func=lambda m: m.chat.type == "private", content_types=["text", "document", "photo"])
def on_other(m):
    bot.reply_to(m, f"⛔️ Bu bot özeldir.\nKullanıcı ID'niz: <code>{m.from_user.id}</code>")


# ---------------------------------------------------------------- Flask
@app.get("/")
def index():
    return "Bot yöneticisi çalışıyor."


@app.post(config.WEBHOOK_PATH)
def webhook():
    if not config.WEBHOOK_SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.WEBHOOK_SECRET:
        abort(403)
    try:
        update = types.Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([update])
    except Exception:
        # 200 dönülür; aksi halde Telegram aynı güncellemeyi tekrar tekrar gönderir
        log.exception("Güncelleme işlenemedi")
    return ""


import miniapp  # noqa: E402  (Blueprint, yukarıdaki nesnelere ihtiyaç duymaz)

app.register_blueprint(miniapp.bp)


# ---------------------------------------------------------------- CLI
def setup():
    core.ensure_dirs()
    db.init_db()
    print("✓ Veritabanı tabloları hazır.")
    if not config.WEBHOOK_SECRET:
        sys.exit("WEBHOOK_SECRET .env içinde tanımlı olmalı.")
    url = f"https://{config.WEB_HOST}{config.WEBHOOK_PATH}"
    r = api("setWebhook", url=url, secret_token=config.WEBHOOK_SECRET, drop_pending_updates=True,
            allowed_updates=["message", "callback_query"], max_connections=10)
    print(("✓" if r.get("ok") else "✗") + f" setWebhook {url}: {r.get('description')}")
    api("deleteMyCommands")  # admin olmayanlar komut görmesin
    sync_admin_ui()
    print(f"✓ {len(db.admin_ids())} admin için komut menüsü ve Mini App düğmesi ayarlandı.")


def polling():
    core.ensure_dirs()
    db.init_db()
    api("deleteWebhook")
    sync_admin_ui()
    print("Polling başladı (yalnızca yerel geliştirme için).")
    bot.infinity_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        setup()
    elif cmd == "polling":
        polling()
    else:
        print(__doc__)
