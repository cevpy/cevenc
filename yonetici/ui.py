"""Ekranlar: her fonksiyon (html_metin, reply_markup_dict) döner.

Hem ana bot (calisan_bot.py) hem supervisor kullanır; bu yüzden telebot'a bağımlı değildir.
"""
import time

import config
import core
import db
from core import bar, esc, fmt_bytes, fmt_duration, fmt_time

PAGE = 8
SETTING_LABELS = {
    "cpu_warn_percent": ("CPU uyarı eşiği", "%"),
    "cpu_kill_percent": ("CPU durdurma eşiği (0=kapalı)", "%"),
    "cpu_window_sec": ("CPU aşım süresi", " sn"),
    "ram_warn_percent": ("RAM uyarı eşiği (limitin)", "%"),
    "crash_limit": ("Çökme sınırı", " kez"),
    "crash_window_sec": ("Çökme penceresi", " sn"),
    "notify_crashes": ("Her çökmede bildir (1/0)", ""),
}
AUDIT_LABELS = {
    "start": "▶️ başlat", "stop": "⏹ durdur", "restart": "🔄 yeniden başlat", "deploy": "📦 yükle",
    "install": "📥 kurulum", "rollback": "⏪ geri al", "delete": "🗑 sil", "env": "🔑 .env",
    "ram": "💾 RAM", "toggle": "⚙️ ayar", "backup": "💾 yedek", "admin_add": "👤+ admin",
    "admin_remove": "👤− admin", "setting": "⚙️ eşik", "file": "📄 dosya", "pkg": "📦 paket",
}


# ---------------------------------------------------------------- küçük yardımcılar
def btn(text, data):
    return {"text": text, "callback_data": data}


def kb(*rows):
    return {"inline_keyboard": [r for r in rows if r]}


def webapp_btn(text="📱 Panel"):
    return {"text": text, "web_app": {"url": f"https://{config.WEB_HOST}{config.MINIAPP_PATH}"}}


def copy_btn(text, value):
    return {"text": text, "copy_text": {"text": (value or "-")[:256]}}


def quote(text, limit=1500):
    text = (text or "").strip()
    if len(text) > limit:
        text = "…" + text[-limit:]
    return f"<blockquote expandable>{esc(text)}</blockquote>"


def back(data="m", text="⬅️ Geri"):
    return [btn(text, data)]


def _pager(prefix, page, pages):
    if pages <= 1:
        return None
    row = []
    row.append(btn("◀️", f"{prefix}:{page - 1}") if page > 0 else btn(" ", "noop"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    row.append(btn("▶️", f"{prefix}:{page + 1}") if page < pages - 1 else btn(" ", "noop"))
    return row


def uptime_of(bot):
    return fmt_duration(time.time() - bot["started_at"]) if bot.get("started_at") and bot["status"] == "running" else "-"


def supervisor_line():
    hb = db.get_int_setting("supervisor_heartbeat", 0)
    age = time.time() - hb
    if hb and age < 30:
        return "🟢 Supervisor çalışıyor"
    return f"🔴 Supervisor yanıt vermiyor (son sinyal: {fmt_duration(age) + ' önce' if hb else 'hiç'})"


def bot_counts(bots):
    c = {"running": 0, "stopped": 0, "problem": 0}
    for b in bots:
        if b["status"] == "running":
            c["running"] += 1
        elif b["status"] in ("crashed", "crashloop", "error", "restarting"):
            c["problem"] += 1
        else:
            c["stopped"] += 1
    return c


def system_block():
    s = db.get_json_setting("sys_stats", {}) or {}
    if not s:
        return "Sistem bilgisi henüz yok (supervisor çalışmıyor olabilir)."
    lines = [
        f"🖥 CPU: <code>{bar(s.get('cpu'))}</code> {s.get('cpu', 0):.0f}% ({s.get('cores', '?')} çekirdek)",
        f"🧠 RAM: <code>{bar(s.get('ram_pct'))}</code> {fmt_bytes(s.get('ram_used'))} / {fmt_bytes(s.get('ram_total'))}",
    ]
    if s.get("quota"):
        pct = 100 * s.get("project_bytes", 0) / s["quota"]
        lines.append(f"💽 Disk: <code>{bar(pct)}</code> {fmt_bytes(s.get('project_bytes'))} / {fmt_bytes(s['quota'])}")
    else:
        lines.append(f"💽 Disk: proje {fmt_bytes(s.get('project_bytes'))} · boş {fmt_bytes(s.get('disk_free'))}")
    lines.append(f"⏱ Uptime: sunucu {fmt_duration(s.get('host_uptime'))} · supervisor {fmt_duration(s.get('sup_uptime'))}")
    lines.append(f"🤖 Botların toplam RAM'i: {fmt_bytes(s.get('bots_rss'))}")
    return "\n".join(lines)


# ---------------------------------------------------------------- ana ekran
def main_menu():
    bots = db.list_bots()
    c = bot_counts(bots)
    text = (
        "🛠 <b>Bot Yöneticisi</b>\n"
        f"{supervisor_line()}\n\n"
        f"{system_block()}\n\n"
        f"🤖 Botlar: 🟢 {c['running']} çalışıyor · ⚪️ {c['stopped']} durdu · 🔴 {c['problem']} sorunlu"
        f" · toplam {len(bots)}"
    )
    return text, kb(
        [btn("🤖 Botlar", "l:0"), btn("➕ Yeni bot", "n")],
        [btn("⚙️ Ayarlar", "s"), btn("📜 Geçmiş", "h:0")],
        [webapp_btn(), btn("🔄 Yenile", "m")],
    )


def bot_list(page=0):
    bots = db.list_bots()
    if not bots:
        return "🤖 <b>Botlar</b>\n\nHenüz bot yok.", kb([btn("➕ Yeni bot", "n")], back())
    pages = (len(bots) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = []
    for b in bots[page * PAGE:(page + 1) * PAGE]:
        extra = f" · {b['rss_mb']:.0f} MB" if b.get("rss_mb") else ""
        rows.append([btn(f"{core.status_icon(b)} {b['name']}{extra}", f"b:{b['name']}")])
    c = bot_counts(bots)
    text = (f"🤖 <b>Botlar</b> ({len(bots)})\n🟢 {c['running']} · ⚪️ {c['stopped']} · 🔴 {c['problem']}\n\n"
            "🟢 çalışıyor · ⚪️ durdu · 🟠 yeniden açılacak · 🔴 çöktü · ⛔️ çökme döngüsü · ⏳ işlem sürüyor")
    return text, kb(*rows, _pager("l", page, pages), [btn("➕ Yeni bot", "n"), btn("⬅️ Ana menü", "m")])


# ---------------------------------------------------------------- bot kartı
def bot_card(name, notice=None):
    b = db.get_bot(name)
    if not b:
        return f"❔ <b>{esc(name)}</b> bulunamadı.", kb(back("l:0", "⬅️ Botlar"))
    lines = []
    if notice:
        lines += [notice, ""]
    uname = f" (@{esc(b['tg_username'])})" if b.get("tg_username") else ""
    lines.append(f"{core.status_icon(b)} <b>{esc(name)}</b>{uname}")
    st = core.STATUS_TEXT.get(b["status"], b["status"])
    lines.append(f"Durum: {st}" + (f" · {uptime_of(b)}" if b["status"] == "running" else ""))
    if b.get("busy"):
        lines.append(f"⏳ İşlem sürüyor: <b>{esc(b['busy'])}</b>")
    pending = db.pending_for_bot(name)
    if pending:
        lines.append("⏳ Kuyrukta: " + ", ".join(esc(p["action"]) for p in pending))
    limit = b["ram_limit_mb"]
    if b["status"] == "running" and b.get("rss_mb") is not None:
        pct = 100 * b["rss_mb"] / limit if limit else 0
        lines.append(f"RAM: <code>{bar(pct)}</code> {b['rss_mb']:.0f} / {limit} MB")
        lines.append(f"CPU: <code>{bar(min(b.get('cpu_percent') or 0, 100))}</code> {b.get('cpu_percent') or 0:.1f}%")
    else:
        lines.append(f"RAM limiti: {limit} MB")
    lines.append(f"Çökme: {b['crash_count']} (son: {fmt_time(b.get('last_crash_at'))})"
                 f" · Yeniden başlatma: {b['restart_count']}")
    lines.append(f"Otomatik başlat: {'✅' if b['autostart'] else '❌'} · "
                 f"Çökünce yeniden aç: {'✅' if b['auto_restart'] else '❌'}")
    if b.get("last_error"):
        code = f" (çıkış kodu {b['last_exit_code']})" if b.get("last_exit_code") is not None else ""
        lines.append(f"\n⚠️ <b>Son hata</b>{code}:\n{quote(b['last_error'], 1200)}")
    running = b["status"] in ("running", "starting", "restarting")
    ctl = [btn("⏹ Durdur", f"a:{name}:stop"), btn("🔄 Yeniden başlat", f"a:{name}:restart")] if running \
        else [btn("▶️ Başlat", f"a:{name}:start")]
    return "\n".join(lines), kb(
        ctl,
        [btn("📜 Loglar", f"lg:{name}:a"), btn("📁 Dosyalar", f"f:{name}:0")],
        [btn("🔑 .env", f"e:{name}:0"), btn("📦 Kütüphaneler", f"p:{name}")],
        [btn("💾 Yedekler", f"k:{name}"), btn("⚙️ Kaynaklar", f"r:{name}")],
        [copy_btn("📋 Hatayı kopyala", b["last_error"])] if b.get("last_error") else None,
        [btn("🗑 Sil", f"d:{name}"), btn("🔄 Yenile", f"b:{name}"), btn("⬅️ Botlar", "l:0")],
    )


def resources(name):
    b = db.get_bot(name)
    if not b:
        return bot_card(name)
    cur = b["ram_limit_mb"]
    ram_btns = [btn(("✅ " if cur == mb else "") + (f"{mb} MB" if mb < 1024 else f"{mb // 1024} GB"), f"ram:{name}:{mb}")
                for mb in config.RAM_CHOICES_MB]
    custom = cur not in config.RAM_CHOICES_MB
    text = (f"⚙️ <b>{esc(name)}</b> — Kaynaklar\n\n"
            f"RAM limiti: <b>{cur} MB</b>{' (özel)' if custom else ''}\n"
            f"Sınır türü: setrlimit RLIMIT_{config.RAM_LIMIT_MODE}, nice +{config.BOT_NICE}\n"
            f"Otomatik başlat (supervisor açılınca): {'✅' if b['autostart'] else '❌'}\n"
            f"Çökünce yeniden aç: {'✅' if b['auto_restart'] else '❌'}\n\n"
            "<i>RAM değişikliği bir sonraki başlatmada uygulanır.</i>")
    running = b["status"] == "running"
    return text, kb(
        ram_btns[:2], ram_btns[2:],
        [btn(("✅ " if custom else "") + "✏️ Özel değer", f"ramc:{name}")],
        [btn(("✅" if b["autostart"] else "❌") + " Otomatik başlat", f"tg:{name}:autostart")],
        [btn(("✅" if b["auto_restart"] else "❌") + " Çökünce yeniden aç", f"tg:{name}:auto_restart")],
        [btn("🔄 Şimdi uygula (yeniden başlat)", f"a:{name}:restart")] if running else None,
        back(f"b:{name}"),
    )


# ---------------------------------------------------------------- loglar
def logs(name, mode="a"):
    b = db.get_bot(name)
    if not b:
        return bot_card(name)
    if mode == "i":
        lines = core.tail_lines(core.install_log_path(name), 60)
        title = "Kurulum logu (pip)"
    elif mode == "e":
        lines = core.filter_errors(core.tail_lines(core.log_path(name), 1500))[-60:]
        title = "Sadece hatalar"
    else:
        lines = core.tail_lines(core.log_path(name), 60)
        title = "Son satırlar"
    body = "\n".join(lines) or "(boş)"
    size = sum(p.stat().st_size for p in core.log_files(name))
    text = f"📜 <b>{esc(name)}</b> — {title}\nToplam log: {fmt_bytes(size)}\n\n{quote(body, 3300)}"
    mode_btns = [btn(("• " if mode == m else "") + label, f"lg:{name}:{m}")
                 for m, label in (("a", "Tümü"), ("e", "Hatalar"), ("i", "Kurulum"))]
    return text, kb(
        mode_btns,
        [btn("⬇️ İndir", f"ld:{name}"), btn("🔄 Yenile", f"lg:{name}:{mode}")],
        [copy_btn("📋 Son hatayı kopyala", b["last_error"])] if b.get("last_error") else None,
        back(f"b:{name}"),
    )


# ---------------------------------------------------------------- dosyalar
FILES_PAGE = 10


def files(name, page=0):
    items = core.list_files(name)
    pages = max(1, (len(items) + FILES_PAGE - 1) // FILES_PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for i, f in enumerate(items[page * FILES_PAGE:(page + 1) * FILES_PAGE], page * FILES_PAGE):
        rows.append([btn(f"📄 {f['path'][-40:]} · {fmt_bytes(f['size'])}", f"fd:{name}:{i}")])
    text = (f"📁 <b>{esc(name)}</b> — Dosyalar ({len(items)})\n"
            "Bir dosyaya dokunarak indirin. venv/ gösterilmez.\n\n"
            "📤 <b>Yükle</b>: .zip gönderirseniz bot güncellenir (önce otomatik yedek alınır); "
            "tek dosya gönderirseniz klasöre eklenir/üzerine yazılır.")
    return text, kb(*rows, _pager(f"f:{name}", page, pages),
                    [btn("📤 Yükle / Güncelle", f"fu:{name}")], back(f"b:{name}"))


# ---------------------------------------------------------------- .env
def env(name, show=False):
    data = core.env_of(name)
    if data:
        rows = []
        for k, v in data.items():
            if show:
                rows.append(f"<code>{esc(k)}</code> = <tg-spoiler>{esc(v)}</tg-spoiler>")
            else:
                masked = (v[:3] + "•" * min(8, max(3, len(v) - 3))) if len(v) > 4 else "•" * len(v)
                rows.append(f"<code>{esc(k)}</code> = {esc(masked)}")
        body = "\n".join(rows)
    else:
        body = "<i>(.env boş)</i>"
    text = (f"🔑 <b>{esc(name)}</b> — .env\n\n{body}\n\n"
            "<i>Değişiklikler botu yeniden başlatınca geçerli olur.</i>")
    return text, kb(
        [btn("✏️ Düzenle", f"ee:{name}"), btn("🙈 Gizle" if show else "👁 Göster", f"e:{name}:{0 if show else 1}")],
        [btn("🔄 Yeniden başlat", f"a:{name}:restart")],
        back(f"b:{name}"),
    )


# ---------------------------------------------------------------- kütüphaneler
def packages(name):
    reqs = core.requirements(name)
    inst = core.installed_packages(name)
    req_txt = "\n".join(reqs) or "(requirements.txt boş)"
    inst_txt = "\n".join(f"{p}=={v}" for p, v in inst) or "(venv yok veya boş)"
    text = (f"📦 <b>{esc(name)}</b> — Kütüphaneler\n\n"
            f"<b>requirements.txt</b> ({len(reqs)}):\n{quote(req_txt, 1200)}\n"
            f"<b>venv'de kurulu</b> ({len(inst)}):\n{quote(inst_txt, 1800)}")
    return text, kb(
        [btn("➕ Ekle", f"pa:{name}"), btn("➖ Kaldır", f"pr:{name}")],
        [btn("📥 Yeniden kur", f"a:{name}:install"), btn("📜 Kurulum logu", f"lg:{name}:i")],
        back(f"b:{name}"),
    )


# ---------------------------------------------------------------- yedekler
def backups(name):
    items = core.list_backups(name)
    rows = [[btn(f"💾 {b['file'][:-4]} · {fmt_bytes(b['size'])}", f"kr:{name}:{i}")] for i, b in enumerate(items)]
    text = (f"💾 <b>{esc(name)}</b> — Yedekler ({len(items)}/{config.MAX_BACKUPS_PER_BOT})\n\n"
            "Her güncellemeden önce otomatik yedek alınır. Yedek: kod + .env + requirements "
            "(venv/, logs/, data/ hariç; geri yüklemede data/ korunur).")
    return text, kb(*rows, [btn("➕ Şimdi yedekle", f"kc:{name}")], back(f"b:{name}"))


def backup_detail(name, idx):
    items = core.list_backups(name)
    if idx >= len(items):
        return backups(name)
    b = items[idx]
    text = (f"💾 <b>{esc(name)}</b>\nYedek: <code>{esc(b['file'])}</code>\n"
            f"Boyut: {fmt_bytes(b['size'])} · Tarih: {fmt_time(b['mtime'])}\n\n"
            "Geri yüklerseniz mevcut kod önce yedeklenir, bot durdurulur, kod değiştirilir, "
            "kütüphaneler kurulur ve bot (çalışıyorsa) yeniden başlatılır.")
    return text, kb(
        [btn("⏪ Geri yükle", f"krc:{name}:{idx}"), btn("⬇️ İndir", f"kdl:{name}:{idx}")],
        [btn("🗑 Yedeği sil", f"kd:{name}:{idx}")],
        back(f"k:{name}"),
    )


def delete_confirm(name):
    return (f"🗑 <b>{esc(name)}</b> silinsin mi?\n\nBot durdurulur, son bir yedek alınır, klasörü "
            "(data/ ve loglar dahil) silinir. Yedekler backups/ altında kalır."), \
        kb([btn("✅ Evet, sil", f"dc:{name}"), btn("✖️ Vazgeç", f"b:{name}")])


# ---------------------------------------------------------------- ayarlar
def settings():
    lines = ["⚙️ <b>Ayarlar</b>\n", "<b>Bildirim eşikleri</b>"]
    for k, (label, unit) in SETTING_LABELS.items():
        lines.append(f"• {label}: <b>{esc(db.get_setting(k, '-'))}{unit}</b>")
    live = db.get_setting("live_message")
    lines.append(f"\n📌 Canlı durum mesajı: {'açık' if live else 'kapalı'}")
    return "\n".join(lines), kb(
        [btn("👥 Adminler", "sa"), btn("📊 Eşikler", "st")],
        [btn("📌 Canlı durum mesajı oluştur", "live")],
        [btn("📴 Canlı mesajı kapat", "liveoff")] if live else None,
        back(),
    )


def admins(viewer_id):
    rows, lines = [], ["👥 <b>Adminler</b>\n"]
    for a in db.list_admins():
        owner = a["user_id"] in config.OWNER_IDS
        uname = f" @{esc(a['username'])}" if a.get("username") else ""
        lines.append(f"• <code>{a['user_id']}</code>{uname}{' 👑' if owner else ''}")
        if not owner and a["user_id"] != viewer_id:
            rows.append([btn(f"➖ {a['username'] or a['user_id']}", f"sr:{a['user_id']}")])
    return "\n".join(lines), kb(*rows, [btn("➕ Admin ekle", "sad")], back("s"))


def thresholds():
    rows = [[btn(f"{label}: {db.get_setting(k, '-')}{unit}", f"sts:{k}")] for k, (label, unit) in SETTING_LABELS.items()]
    return "📊 <b>Bildirim eşikleri</b>\nDeğiştirmek istediğinize dokunun.", kb(*rows, back("s"))


def history(page=0):
    per = 15
    rows, total = db.audit_page(page * per, per)
    pages = max(1, (total + per - 1) // per)
    lines = [f"📜 <b>İşlem geçmişi</b> ({total})\n"]
    for r in rows:
        label = AUDIT_LABELS.get(r["action"], r["action"])
        tgt = f" <b>{esc(r['target'])}</b>" if r.get("target") else ""
        det = f" — {esc(r['detail'][:60])}" if r.get("detail") else ""
        who = r["user_id"] if r.get("user_id") else "sistem"
        lines.append(f"<code>{fmt_time(r['created_at'])}</code> {label}{tgt}{det} <i>({who})</i>")
    if not rows:
        lines.append("(kayıt yok)")
    return "\n".join(lines), kb(_pager("h", page, pages), back())


def prompt(text, cancel="x"):
    return text, kb([btn("✖️ İptal", cancel)])


# ---------------------------------------------------------------- canlı durum (sabitlenmiş)
def live_status():
    bots = db.list_bots()
    c = bot_counts(bots)
    s = db.get_json_setting("sys_stats", {}) or {}
    lines = [
        "📌 <b>Canlı durum</b>",
        supervisor_line(),
        f"🖥 CPU {s.get('cpu', 0):.0f}% · 🧠 RAM {s.get('ram_pct', 0):.0f}% · 🤖 {fmt_bytes(s.get('bots_rss'))}",
        f"🟢 {c['running']} · ⚪️ {c['stopped']} · 🔴 {c['problem']}",
        "",
    ]
    for b in bots[:30]:
        extra = f" {b['rss_mb']:.0f}MB {b.get('cpu_percent') or 0:.0f}%" if b["status"] == "running" and b.get("rss_mb") else ""
        lines.append(f"{core.status_icon(b)} {esc(b['name'])}{extra}")
    if len(bots) > 30:
        lines.append(f"… +{len(bots) - 30}")
    lines.append(f"\n<i>Güncellendi: {time.strftime('%H:%M:%S')}</i>")
    # Bu mesaj sabit kalır; "Panel aç" yeni bir panel mesajı gönderir, bunu değiştirmez.
    return "\n".join(lines), kb([btn("🔄 Yenile", "liverf"), btn("🛠 Panel aç", "mnew")])
