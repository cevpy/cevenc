"""Always-on task: alt botları başlatır, izler, çökenleri yeniden açar.

PythonAnywhere → Tasks → Always-on tasks:
    /home/KULLANICI/.virtualenvs/yonetici/bin/python /home/KULLANICI/cevenc/yonetici/supervisor.py

Web app ile iletişim MySQL'deki `commands` tablosu üzerinden olur.
"""
import collections
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil

import config
import core
import db
import ui
from core import UserError, esc
from envfile import read_env, write_env

log = logging.getLogger("supervisor")
LAUNCHER = config.BASE_DIR / "_launch.py"
PASS_ENV = ("HOME", "USER", "LOGNAME", "TZ", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY",
            "NO_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CERT")
STABLE_AFTER = 120          # bu kadar sn çalışan bot "sağlıklı" sayılır, ardışık çökme sayacı sıfırlanır
MAX_BACKOFF = 300
SAMPLE_EVERY = 5
STATS_EVERY = 10
LIVE_EVERY = 60
ROTATE_EVERY = 30
DISK_EVERY = 300


class ManagedBot:
    def __init__(self, name):
        self.name = name
        self.lock = threading.RLock()
        self.proc = None                  # subprocess.Popen
        self.started_at = None
        self.expected_stop = False
        self.crash_times = collections.deque()
        self.consecutive = 0
        self.next_start_at = None
        self.ps_procs = {}               # pid -> psutil.Process (cpu_percent için kalıcı nesneler)
        self.cpu_high_since = None
        self.cpu_warned = False
        self.ram_warned = False

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


class Supervisor:
    def __init__(self):
        self.bots = {}
        self.bots_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cmd")
        self.stopping = threading.Event()
        self.started = time.time()
        self.last = collections.defaultdict(float)
        self.notified = {}
        self.live_cache = None
        self.project_bytes = 0

    # ------------------------------------------------------------ yardımcılar
    def get(self, name) -> ManagedBot:
        with self.bots_lock:
            if name not in self.bots:
                self.bots[name] = ManagedBot(name)
            return self.bots[name]

    def notify(self, text, markup=None, key=None, every=0):
        """Tüm adminlere bildirim. key+every ile aynı bildirim sık tekrarlanmaz."""
        if key and every and time.time() - self.notified.get(key, 0) < every:
            return
        if key:
            self.notified[key] = time.time()
        if not config.MAIN_BOT_TOKEN:
            return
        for uid in db.admin_ids():
            r = core.tg_api(config.MAIN_BOT_TOKEN, "sendMessage", chat_id=uid, text=text[:4096], parse_mode="HTML",
                            reply_markup=markup, link_preview_options={"is_disabled": True})
            if not r.get("ok"):
                log.warning("Bildirim gönderilemedi (%s): %s", uid, r.get("description"))

    def refresh_panel(self, cmd, notice):
        """Komutu veren kullanıcının panel mesajını güncel bot kartıyla yeniler."""
        msg_id, chat_id, name = cmd["payload"].get("msg_id"), cmd.get("chat_id"), cmd.get("bot_name")
        if not (msg_id and chat_id and config.MAIN_BOT_TOKEN):
            return
        if name and db.get_bot(name):
            text, markup = ui.bot_card(name, notice=notice)
        else:
            text, markup = ui.bot_list(0)
            text = f"{notice}\n\n{text}"
        core.tg_api(config.MAIN_BOT_TOKEN, "editMessageText", chat_id=chat_id, message_id=msg_id, text=text,
                    parse_mode="HTML", reply_markup=markup, link_preview_options={"is_disabled": True})

    def child_env(self, name):
        venv = core.bot_dir(name) / "venv"
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        env.update({
            "PATH": f"{venv / 'bin'}:/usr/local/bin:/usr/bin:/bin",
            "VIRTUAL_ENV": str(venv),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
            "MALLOC_ARENA_MAX": "2",          # RLIMIT_AS altında glibc arenalarının sanal bellek israfını azaltır
            "BOT_NAME": name,
            "BOT_DATA_DIR": str(core.bot_dir(name) / "data"),
        })
        # Botun kendi .env değişkenleri (yöneticinin sırları ASLA aktarılmaz)
        env.update(core.env_of(name))
        return env

    # ------------------------------------------------------------ kurulum
    def ensure_venv(self, name, force=False):
        d = core.bot_dir(name)
        py = core.venv_python(name)
        (d / "logs").mkdir(parents=True, exist_ok=True)
        inst_log = core.install_log_path(name)
        if not py.exists():
            db.update_bot(name, busy="venv")
            shutil.rmtree(d / "venv", ignore_errors=True)
            with open(inst_log, "ab") as fh:
                fh.write(f"\n=== {time.strftime('%F %T')} venv oluşturuluyor ({config.BOT_PYTHON}) ===\n".encode())
                fh.flush()
                r = subprocess.run([config.BOT_PYTHON, "-m", "venv", str(d / "venv")], cwd=d, stdout=fh,
                                   stderr=subprocess.STDOUT, timeout=300)
            if r.returncode != 0 or not py.exists():
                raise UserError("venv oluşturulamadı:\n" + "\n".join(core.tail_lines(inst_log, 10)))
        marker = d / "venv" / ".req_hash"
        h = core.requirements_hash(name)
        if not force and marker.is_file() and marker.read_text() == h:
            return False
        reqs = d / "requirements.txt"
        if reqs.is_file() and core.requirements(name):
            db.update_bot(name, busy="installing")
            core.rotate_log(inst_log)
            env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
            env.update({"PATH": f"{d / 'venv' / 'bin'}:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
            with open(inst_log, "ab") as fh:
                fh.write(f"\n=== {time.strftime('%F %T')} pip install -r requirements.txt ===\n".encode())
                fh.flush()
                try:
                    r = subprocess.run([str(py), "-m", "pip", "install", "-r", "requirements.txt"], cwd=d, env=env,
                                       stdout=fh, stderr=subprocess.STDOUT, timeout=config.PIP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    raise UserError(f"pip install {config.PIP_TIMEOUT} sn içinde bitmedi.")
            if r.returncode != 0:
                raise UserError("pip install başarısız:\n" + "\n".join(core.tail_lines(inst_log, 12)))
        marker.write_text(h)
        return True

    # ------------------------------------------------------------ başlat / durdur
    def start_bot(self, name, allow_install=True, reason="manual"):
        mb = self.get(name)
        with mb.lock:
            if mb.alive():
                return "Zaten çalışıyor."
            row = db.get_bot(name)
            if not row:
                raise UserError("Bot kaydı yok.")
            d = core.bot_dir(name)
            if not (d / "main.py").is_file():
                raise UserError("main.py bulunamadı.")
            if allow_install:
                self.ensure_venv(name)
            elif not core.venv_python(name).exists():
                raise UserError("venv yok; önce kurulum gerekli.")
            (d / "logs").mkdir(exist_ok=True)
            (d / "data").mkdir(exist_ok=True)
            lp = core.log_path(name)
            core.rotate_log(lp)
            py = str(core.venv_python(name))
            ram = int(row["ram_limit_mb"]) * 1024 * 1024
            cmd = [py, str(LAUNCHER), str(ram), config.RAM_LIMIT_MODE, str(config.BOT_NICE), "--", py, "-u", "main.py"]
            with open(lp, "ab") as fh:
                fh.write(f"\n=== {time.strftime('%F %T')} başlatılıyor ({reason}, RAM {row['ram_limit_mb']} MB) ===\n"
                         .encode())
                fh.flush()
                mb.proc = subprocess.Popen(cmd, cwd=d, env=self.child_env(name), stdin=subprocess.DEVNULL,
                                           stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
                                           close_fds=True)
            mb.started_at = time.time()
            mb.expected_stop = False
            mb.next_start_at = None
            mb.ps_procs = {}
            mb.cpu_high_since, mb.cpu_warned, mb.ram_warned = None, False, False
            fields = dict(status="running", desired_state="running", pid=mb.proc.pid, started_at=int(mb.started_at),
                          busy=None, rss_mb=None, cpu_percent=None)
            if reason == "auto":
                fields["restart_count"] = row["restart_count"] + 1
            db.update_bot(name, **fields)
            log.info("%s başlatıldı (pid %s, %s)", name, mb.proc.pid, reason)
            return f"Başlatıldı (pid {mb.proc.pid})."

    def _terminate(self, mb):
        """SIGTERM → STOP_TIMEOUT sn → SIGKILL; tüm süreç grubuna."""
        proc = mb.proc
        pgid = proc.pid  # start_new_session=True → süreç grup lideri
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(config.STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("%s SIGTERM'e yanıt vermedi, SIGKILL", mb.name)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(5)
        self._kill_group_leftovers(pgid)

    @staticmethod
    def _kill_group_leftovers(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def stop_bot(self, name, status="stopped", desired="stopped"):
        mb = self.get(name)
        with mb.lock:
            mb.expected_stop = True
            mb.next_start_at = None
            if mb.proc is not None:
                if mb.proc.poll() is None:
                    self._terminate(mb)
                mb.proc = None
            if db.get_bot(name):
                db.update_bot(name, status=status, desired_state=desired, pid=None, started_at=None,
                              rss_mb=None, cpu_percent=None)
            log.info("%s durduruldu", name)
            return "Durduruldu."

    # ------------------------------------------------------------ çökme yönetimi
    def on_exit(self, mb, rc):
        name = mb.name
        pid = mb.proc.pid
        mb.proc = None
        self._kill_group_leftovers(pid)
        if mb.expected_stop:
            return
        row = db.get_bot(name)
        if not row:
            return
        t = time.time()
        uptime = t - (mb.started_at or t)
        err = core.extract_last_error(name)
        if rc < 0:
            try:
                sig = signal.Signals(-rc).name
            except ValueError:
                sig = str(-rc)
            err += f"\n[{sig} sinyali ile sonlandı" + (" — bellek sınırı/OOM olabilir" if rc == -9 else "") + "]"
        window = db.get_int_setting("crash_window_sec", 60)
        limit = db.get_int_setting("crash_limit", 5)
        mb.crash_times.append(t)
        while mb.crash_times and mb.crash_times[0] < t - window:
            mb.crash_times.popleft()
        mb.consecutive = 1 if uptime >= STABLE_AFTER else mb.consecutive + 1
        db.update_bot(name, crash_count=row["crash_count"] + 1, last_error=err, last_exit_code=rc,
                      last_crash_at=int(t), pid=None, started_at=None, rss_mb=None, cpu_percent=None)
        log.warning("%s çıktı (kod %s, %.0f sn çalıştı)", name, rc, uptime)
        buttons = ui.kb([ui.btn("📜 Loglar", f"lg:{name}:e"), ui.btn("🤖 Bot kartı", f"b:{name}")])
        if limit and len(mb.crash_times) >= limit:
            mb.crash_times.clear()
            mb.consecutive = 0
            db.update_bot(name, status="crashloop", desired_state="stopped")
            db.audit(None, "stop", name, f"çökme döngüsü: {window} sn içinde {limit} çökme")
            self.notify(f"⛔️ <b>{esc(name)}</b> {window} sn içinde {limit} kez çöktü ve durduruldu.\n"
                        f"Çıkış kodu: {rc}\n{ui.quote(err, 1500)}", buttons)
        elif row["auto_restart"]:
            delay = min(2 ** (mb.consecutive - 1), MAX_BACKOFF)
            mb.next_start_at = t + delay
            db.update_bot(name, status="restarting")
            if db.get_int_setting("notify_crashes", 1):
                self.notify(f"🔴 <b>{esc(name)}</b> çöktü (kod {rc}, {core.fmt_duration(uptime)} çalıştı). "
                            f"{delay} sn sonra yeniden başlatılacak.\n{ui.quote(err, 1200)}", buttons)
        else:
            db.update_bot(name, status="crashed", desired_state="stopped")
            self.notify(f"🔴 <b>{esc(name)}</b> çöktü (kod {rc}). Otomatik yeniden başlatma kapalı.\n"
                        f"{ui.quote(err, 1500)}", buttons)

    def auto_restart(self, mb):
        mb.next_start_at = None
        try:
            self.start_bot(mb.name, allow_install=False, reason="auto")
        except Exception as e:
            log.exception("%s otomatik başlatılamadı", mb.name)
            db.update_bot(mb.name, status="error", desired_state="stopped", last_error=str(e)[:1500])
            self.notify(f"❌ <b>{esc(mb.name)}</b> yeniden başlatılamadı:\n{ui.quote(str(e))}")

    # ------------------------------------------------------------ izleme
    def sample(self, mb):
        row = db.get_bot(mb.name)
        if not row or not mb.alive():
            return
        try:
            root = mb.ps_procs.get(mb.proc.pid) or psutil.Process(mb.proc.pid)
            procs = [root] + root.children(recursive=True)
        except psutil.Error:
            return
        rss = cpu = 0.0
        fresh = {}
        for p in procs:
            p = mb.ps_procs.get(p.pid, p)
            try:
                with p.oneshot():
                    rss += p.memory_info().rss
                    cpu += p.cpu_percent(None)
                fresh[p.pid] = p
            except psutil.Error:
                pass
        mb.ps_procs = fresh
        rss_mb = rss / 1024 / 1024
        db.update_bot(mb.name, rss_mb=round(rss_mb, 1), cpu_percent=round(cpu, 1))
        name = esc(mb.name)
        # RAM
        limit = row["ram_limit_mb"]
        warn_pct = db.get_int_setting("ram_warn_percent", 90)
        if limit and warn_pct and rss_mb >= limit * warn_pct / 100:
            if not mb.ram_warned:
                mb.ram_warned = True
                self.notify(f"🧠 <b>{name}</b> RAM limitine yaklaştı: {rss_mb:.0f} / {limit} MB",
                            ui.kb([ui.btn("⚙️ Kaynaklar", f"r:{mb.name}")]))
        elif rss_mb < limit * max(warn_pct - 10, 0) / 100:
            mb.ram_warned = False
        # CPU (sürekli aşım)
        cpu_warn = db.get_int_setting("cpu_warn_percent", 80)
        cpu_kill = db.get_int_setting("cpu_kill_percent", 0)
        window = db.get_int_setting("cpu_window_sec", 60)
        high = (cpu_warn and cpu >= cpu_warn) or (cpu_kill and cpu >= cpu_kill)
        if not high:
            mb.cpu_high_since, mb.cpu_warned = None, False
            return
        mb.cpu_high_since = mb.cpu_high_since or time.time()
        if time.time() - mb.cpu_high_since < window:
            return
        if cpu_kill and cpu >= cpu_kill:
            log.warning("%s CPU %.0f%% ≥ %s%%, durduruluyor", mb.name, cpu, cpu_kill)
            self.stop_bot(mb.name)
            db.update_bot(mb.name, last_error=f"CPU {window} sn boyunca %{cpu:.0f} (sınır %{cpu_kill}) — durduruldu")
            db.audit(None, "stop", mb.name, f"CPU aşımı %{cpu:.0f}")
            self.notify(f"🔥 <b>{name}</b> {window} sn boyunca %{cpu:.0f} CPU kullandı ve durduruldu "
                        f"(sınır %{cpu_kill}).", ui.kb([ui.btn("🤖 Bot kartı", f"b:{mb.name}")]))
        elif not mb.cpu_warned:
            mb.cpu_warned = True
            self.notify(f"🔥 <b>{name}</b> {window} sn'dir yüksek CPU kullanıyor: %{cpu:.0f}",
                        ui.kb([ui.btn("🤖 Bot kartı", f"b:{mb.name}")]))

    def write_stats(self):
        vm = psutil.virtual_memory()
        try:
            du = psutil.disk_usage(str(config.BASE_DIR))
        except OSError:
            du = None
        if time.time() - self.last["disk"] >= DISK_EVERY:
            self.last["disk"] = time.time()
            self.project_bytes = core.dir_size(config.BASE_DIR)
        bots_rss = sum((b.get("rss_mb") or 0) for b in db.list_bots()) * 1024 * 1024
        db.set_setting("sys_stats", json.dumps({
            "cpu": psutil.cpu_percent(None), "cores": psutil.cpu_count(),
            "ram_used": vm.total - vm.available, "ram_total": vm.total, "ram_pct": vm.percent,
            "disk_free": du.free if du else None, "project_bytes": self.project_bytes,
            "quota": config.DISK_QUOTA_MB * 1024 * 1024 if config.DISK_QUOTA_MB else None,
            "host_uptime": time.time() - psutil.boot_time(), "sup_uptime": time.time() - self.started,
            "bots_rss": bots_rss,
        }))
        db.set_setting("supervisor_heartbeat", int(time.time()))

    def update_live_message(self):
        ref = db.get_setting("live_message")
        if not ref or not config.MAIN_BOT_TOKEN:
            return
        chat_id, msg_id = ref.split(":")
        text, markup = ui.live_status()
        # "Güncellendi" satırı hariç değişmediyse düzenleme yapma
        key = text.rsplit("\n", 1)[0]
        if key == self.live_cache:
            return
        r = core.tg_api(config.MAIN_BOT_TOKEN, "editMessageText", chat_id=int(chat_id), message_id=int(msg_id),
                        text=text, parse_mode="HTML", reply_markup=markup)
        if r.get("ok") or "not modified" in r.get("description", ""):
            self.live_cache = key
        elif "not found" in r.get("description", "") or r.get("error_code") == 403:
            db.set_setting("live_message", None)

    # ------------------------------------------------------------ komutlar
    def dispatch(self, cmd):
        action, name, payload = cmd["action"], cmd.get("bot_name"), cmd["payload"]
        handlers = {
            "start": lambda: self.start_bot(name),
            "stop": lambda: self.stop_bot(name),
            "restart": lambda: self.cmd_restart(name),
            "deploy": lambda: self.cmd_deploy(name, payload),
            "install": lambda: self.cmd_install(name, payload.get("recreate")),
            "rollback": lambda: self.cmd_rollback(name, payload),
            "delete": lambda: self.cmd_delete(name),
        }
        fn = handlers.get(action)
        try:
            if not fn:
                raise UserError(f"Bilinmeyen komut: {action}")
            if name:
                core.validate_name(name)
            result = fn() or "Tamam."
            db.finish_command(cmd["id"], True, result)
            self.refresh_panel(cmd, f"✅ <b>{esc(action)}</b>: {esc(result)}")
        except Exception as e:
            if not isinstance(e, UserError):
                log.exception("Komut hatası: %s", cmd)
            msg = str(e) or e.__class__.__name__
            db.finish_command(cmd["id"], False, msg)
            if name and db.get_bot(name):
                db.update_bot(name, busy=None)
            self.refresh_panel(cmd, f"❌ <b>{esc(action)}</b> başarısız:\n{ui.quote(msg, 1200)}")
            if not cmd["payload"].get("msg_id"):
                self.notify(f"❌ <b>{esc(name or '-')}</b> — {esc(action)} başarısız:\n{ui.quote(msg, 1500)}")

    def cmd_restart(self, name):
        mb = self.get(name)
        with mb.lock:
            self.stop_bot(name)
            return self.start_bot(name, reason="restart")

    def cmd_install(self, name, recreate=False):
        mb = self.get(name)
        with mb.lock:
            was_running = mb.alive()
            if was_running:
                self.stop_bot(name, desired="running")
            if recreate:  # paket kaldırıldığında temiz venv (pip bağımlılıkları geride bırakmasın)
                shutil.rmtree(core.bot_dir(name) / "venv", ignore_errors=True)
            try:
                self.ensure_venv(name, force=True)
            finally:
                db.update_bot(name, busy=None)
            if was_running:
                self.start_bot(name, reason="install")
            return "Kütüphaneler kuruldu" + (" ve bot yeniden başlatıldı." if was_running else ".")

    def cmd_deploy(self, name, payload):
        zip_path = Path(payload["zip"]).resolve()
        if config.UPLOADS_DIR.resolve() not in zip_path.parents:
            raise UserError("Geçersiz yükleme yolu.")
        row = db.get_bot(name)
        if not row:
            raise UserError("Bot kaydı yok.")
        mb = self.get(name)
        with mb.lock:
            is_new = bool(payload.get("new"))
            was_running = mb.alive() or row["desired_state"] == "running"
            db.update_bot(name, busy="deploying")
            try:
                tmp = core.extract_to_temp(zip_path)
                try:
                    backup = None
                    if (core.bot_dir(name) / "main.py").is_file():
                        backup = core.make_backup(name, "update")
                    if mb.alive():
                        self.stop_bot(name, desired="running")
                    core.replace_code(name, tmp)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                env_path = core.bot_dir(name) / ".env"
                if payload.get("token"):
                    env = read_env(env_path)
                    env["BOT_TOKEN"] = payload["token"]
                    write_env(env_path, env)
                elif env_path.exists():
                    env_path.chmod(0o600)
                self.ensure_venv(name)
            except Exception:
                db.update_bot(name, busy=None, status="error" if is_new else row["status"])
                raise
            finally:
                zip_path.unlink(missing_ok=True)
            db.update_bot(name, busy=None)
            if is_new or was_running:
                self.start_bot(name, reason="deploy")
                return ("Kuruldu ve başlatıldı." if is_new else "Güncellendi ve yeniden başlatıldı.") + \
                    (f" Yedek: {backup.name}" if backup else "")
            db.update_bot(name, status="stopped")
            return "Güncellendi (bot durduruluyordu, başlatılmadı)." + (f" Yedek: {backup.name}" if backup else "")

    def cmd_rollback(self, name, payload):
        src = core.backup_file(name, payload.get("file"))
        mb = self.get(name)
        with mb.lock:
            was_running = mb.alive()
            db.update_bot(name, busy="rollback")
            try:
                tmp = core.extract_to_temp(src)
                try:
                    core.make_backup(name, "prerollback")
                    if was_running:
                        self.stop_bot(name, desired="running")
                    (core.bot_dir(name) / ".env").unlink(missing_ok=True)  # yedekteki .env geri gelsin
                    core.replace_code(name, tmp)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                self.ensure_venv(name)
            finally:
                db.update_bot(name, busy=None)
            if was_running:
                self.start_bot(name, reason="rollback")
            return f"{src.name} geri yüklendi" + (" ve bot yeniden başlatıldı." if was_running else ".")

    def cmd_delete(self, name):
        mb = self.get(name)
        with mb.lock:
            self.stop_bot(name)
            if (core.bot_dir(name) / "main.py").is_file():
                core.make_backup(name, "deleted")
            shutil.rmtree(core.bot_dir(name), ignore_errors=True)
            db.delete_bot(name)
        with self.bots_lock:
            self.bots.pop(name, None)
        return "Silindi (son yedek backups/ altında)."

    # ------------------------------------------------------------ ana döngü
    def cleanup_orphans(self):
        """Önceki supervisor'dan kalan alt bot süreçlerini sonlandırır."""
        for row in db.list_bots():
            pid = row.get("pid")
            if not pid:
                continue
            try:
                p = psutil.Process(pid)
                if Path(p.cwd()).resolve() == core.bot_dir(row["name"]).resolve():
                    log.info("Yetim süreç sonlandırılıyor: %s (pid %s)", row["name"], pid)
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                        p.wait(config.STOP_TIMEOUT)
                    except psutil.TimeoutExpired:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (psutil.Error, ProcessLookupError, OSError):
                pass

    def boot(self):
        core.ensure_dirs()
        db.init_db()
        db.fail_stale_running()
        self.cleanup_orphans()
        for d in config.BOTS_DIR.glob(".tmp_*"):
            shutil.rmtree(d, ignore_errors=True)
        rows = db.list_bots()
        for row in rows:
            db.update_bot(row["name"], pid=None, started_at=None, busy=None, rss_mb=None, cpu_percent=None,
                          status="stopped" if row["status"] not in ("crashloop", "error") else row["status"])
        for row in rows:
            if row["autostart"] and row["status"] not in ("crashloop",):
                self.pool.submit(self._safe_start, row["name"])
        self.notify(f"🟢 Supervisor başladı. {sum(1 for r in rows if r['autostart'])}/{len(rows)} bot "
                    "otomatik başlatılıyor.", key="boot", every=60)

    def _safe_start(self, name):
        try:
            self.start_bot(name, reason="autostart")
        except Exception as e:
            log.exception("%s otomatik başlatılamadı", name)
            db.update_bot(name, status="error", busy=None, last_error=str(e)[:1500])
            self.notify(f"❌ <b>{esc(name)}</b> otomatik başlatılamadı:\n{ui.quote(str(e))}")

    def tick(self):
        now = time.time()
        for cmd in db.claim_pending():
            self.pool.submit(self.dispatch, cmd)
        with self.bots_lock:
            bots = list(self.bots.values())
        do_sample = now - self.last["sample"] >= SAMPLE_EVERY
        do_rotate = now - self.last["rotate"] >= ROTATE_EVERY
        for mb in bots:
            if not mb.lock.acquire(blocking=False):
                continue  # üzerinde uzun bir komut çalışıyor
            try:
                if mb.proc is not None:
                    rc = mb.proc.poll()
                    if rc is not None:
                        self.on_exit(mb, rc)
                    else:
                        if do_sample:
                            self.sample(mb)
                        if do_rotate:
                            core.rotate_log(core.log_path(mb.name))
                elif mb.next_start_at and now >= mb.next_start_at:
                    self.auto_restart(mb)
            except Exception:
                log.exception("İzleme hatası: %s", mb.name)
            finally:
                mb.lock.release()
        if do_sample:
            self.last["sample"] = now
        if do_rotate:
            self.last["rotate"] = now
            core.rotate_log(config.SUPERVISOR_LOG)
        if now - self.last["stats"] >= STATS_EVERY:
            self.last["stats"] = now
            self.write_stats()
        if now - self.last["live"] >= LIVE_EVERY:
            self.last["live"] = now
            self.update_live_message()
        if now - self.last["cleanup"] >= 3600:
            self.last["cleanup"] = now
            db.cleanup_commands()

    def run(self):
        self.boot()
        log.info("Supervisor hazır.")
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Döngü hatası")  # ör. MySQL geçici olarak erişilemez
                self.stopping.wait(5)
            self.stopping.wait(1)
        self.shutdown()

    def shutdown(self):
        log.info("Kapanıyor: tüm botlar durduruluyor…")
        with self.bots_lock:
            bots = [mb for mb in self.bots.values() if mb.alive()]
        threads = []
        for mb in bots:
            t = threading.Thread(target=self._stop_keep_desired, args=(mb.name,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(config.STOP_TIMEOUT + 10)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _stop_keep_desired(self, name):
        try:
            self.stop_bot(name, desired="running")
        except Exception:
            log.exception("%s durdurulamadı", name)


def setup_logging():
    handler = RotatingFileHandler(config.SUPERVISOR_LOG, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_KEEP,
                                  encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[handler, stream])


def acquire_single_instance_lock():
    fh = open(config.BASE_DIR / "supervisor.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Başka bir supervisor zaten çalışıyor, çıkılıyor.", file=sys.stderr)
        sys.exit(1)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main():
    setup_logging()
    lock = acquire_single_instance_lock()  # noqa: F841  (dosya açık kaldıkça kilit sürer)
    sup = Supervisor()

    def on_signal(signum, _frame):
        log.info("Sinyal alındı: %s", signal.Signals(signum).name)
        sup.stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    sup.run()


if __name__ == "__main__":
    main()
