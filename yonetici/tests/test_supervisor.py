"""Supervisor'ı gerçek alt süreçlerle dener (SQLite modunda)."""
import os
import sys
import time
import zipfile

import psutil
import pytest

import config
import core
import db
import supervisor as sv


def fake_venv(name):
    """Hızlı testler için venv yerine mevcut Python'a sembolik bağlantı."""
    b = core.bot_dir(name) / "venv" / "bin"
    b.mkdir(parents=True, exist_ok=True)
    if not (b / "python").exists():
        os.symlink(sys.executable, b / "python")
    (core.bot_dir(name) / "venv" / ".req_hash").write_text(core.requirements_hash(name))


def make_bot(name, code, **fields):
    d = config.BOTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(code)
    if db.get_bot(name):
        db.delete_bot(name)
    db.create_bot(name, **fields)
    fake_venv(name)
    return d


def wait_for(cond, timeout=15, step=0.2, sup=None):
    end = time.time() + timeout
    while time.time() < end:
        if sup:
            sup.tick()
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def sup(monkeypatch):
    monkeypatch.setattr(sv, "MAX_BACKOFF", 0)  # testte bekleme olmasın
    s = sv.Supervisor()
    yield s
    for mb in list(s.bots.values()):
        if mb.alive():
            s.stop_bot(mb.name)
    s.pool.shutdown(wait=True)


LOOP = "import time, os\nprint('merhaba', os.environ.get('BOT_NAME'), os.environ.get('SECRET'), flush=True)\n" \
       "while True: time.sleep(0.5)\n"


def test_start_stop_env_and_isolation(sup, monkeypatch):
    monkeypatch.setenv("MYSQL_PASSWORD", "gizli-yonetici-sirri")
    d = make_bot("loopbot", LOOP)
    (d / ".env").write_text("SECRET=abc\n")
    sup.start_bot("loopbot")
    row = db.get_bot("loopbot")
    assert row["status"] == "running" and row["pid"]
    assert wait_for(lambda: "merhaba loopbot abc" in core.log_path("loopbot").read_text())
    p = psutil.Process(row["pid"])
    assert p.cwd() == str(d)
    assert p.nice() >= config.BOT_NICE
    assert "MYSQL_PASSWORD" not in p.environ()
    sup.stop_bot("loopbot")
    assert not psutil.pid_exists(row["pid"]) or psutil.Process(row["pid"]).status() == psutil.STATUS_ZOMBIE
    assert db.get_bot("loopbot")["status"] == "stopped"


def test_sigterm_ignored_gets_sigkill(sup):
    code = ("import signal, time, subprocess, sys\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'])\n"
            "open('child.pid','w').write(str(child.pid))\nprint('hazir', flush=True)\nwhile True: time.sleep(1)\n")
    d = make_bot("stubborn", code)
    sup.start_bot("stubborn")
    assert wait_for(lambda: (d / "child.pid").exists() and "hazir" in core.log_path("stubborn").read_text())
    child = int((d / "child.pid").read_text())
    t = time.time()
    sup.stop_bot("stubborn")
    assert config.STOP_TIMEOUT - 0.5 <= time.time() - t < config.STOP_TIMEOUT + 5
    time.sleep(0.3)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_crash_restart_then_crashloop(sup):
    make_bot("crashy", "import sys\nprint('boom')\nraise RuntimeError('patladi')\n", auto_restart=1)
    sup.start_bot("crashy")
    assert wait_for(lambda: db.get_bot("crashy")["status"] == "crashloop", timeout=30, sup=sup)
    row = db.get_bot("crashy")
    assert row["crash_count"] == 5
    assert row["restart_count"] == 4
    assert "RuntimeError: patladi" in row["last_error"]
    assert row["desired_state"] == "stopped"
    assert db.fetchone("SELECT * FROM audit_log WHERE target='crashy' AND action='stop'")


def test_no_auto_restart(sup):
    make_bot("once", "raise SystemExit(3)\n", auto_restart=0)
    sup.start_bot("once")
    assert wait_for(lambda: db.get_bot("once")["status"] == "crashed", sup=sup)
    assert db.get_bot("once")["last_exit_code"] == 3


def test_ram_limit(sup):
    code = "print('ayiriliyor', flush=True)\nx = bytearray(400 * 1024 * 1024)\nprint('SIGMADI', flush=True)\n"
    make_bot("hungry", code, ram_limit_mb=128, auto_restart=0)
    sup.start_bot("hungry")
    assert wait_for(lambda: db.get_bot("hungry")["status"] == "crashed", sup=sup)
    log = core.log_path("hungry").read_text()
    assert "MemoryError" in log and "SIGMADI" not in log


def test_metrics_sampled(sup):
    make_bot("metric", "x = bytearray(30*1024*1024)\nimport time\nwhile True: time.sleep(0.2)\n")
    sup.start_bot("metric")
    time.sleep(1)
    sup.sample(sup.get("metric"))
    assert db.get_bot("metric")["rss_mb"] > 25


def test_queue_deploy_update_rollback_delete(sup):
    """Gerçek venv oluşturma + deploy + güncelleme (yedek) + geri alma + silme."""
    name = "deployed"
    if db.get_bot(name):
        db.delete_bot(name)
    db.create_bot(name, busy="deploying")

    def upload(version):
        z = config.UPLOADS_DIR / f"{name}_{version}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("src/main.py", LOOP.replace("merhaba", f"surum{version}"))
            zf.writestr("src/requirements.txt", "")
        return str(z)

    def run(action, **payload):
        cid = db.enqueue(action, name, payload)
        assert wait_for(lambda: db.fetchone("SELECT status FROM commands WHERE id=%s", (cid,))["status"]
                        in ("done", "error"), timeout=120, sup=sup)
        r = db.fetchone("SELECT * FROM commands WHERE id=%s", (cid,))
        assert r["status"] == "done", r["result"]
        return r

    run("deploy", zip=upload(1), new=True, token="123456:" + "T" * 35)
    assert core.venv_python(name).exists()
    assert core.env_of(name)["BOT_TOKEN"].startswith("123456:")
    assert wait_for(lambda: "surum1" in core.log_path(name).read_text())
    (core.bot_dir(name) / "data" / "kalici.txt").write_text("veri")

    run("deploy", zip=upload(2))
    assert wait_for(lambda: "surum2" in core.log_path(name).read_text())
    backups = core.list_backups(name)
    assert backups and backups[0]["file"].endswith("_update.zip")
    assert (core.bot_dir(name) / "data" / "kalici.txt").read_text() == "veri"
    assert core.env_of(name)["BOT_TOKEN"].startswith("123456:")  # .env korunur

    run("rollback", file=backups[0]["file"])
    assert "surum1" in (core.bot_dir(name) / "main.py").read_text()
    assert db.get_bot(name)["status"] == "running"

    run("delete")
    assert not core.bot_dir(name).exists() and db.get_bot(name) is None
    assert any(b["file"].endswith("_deleted.zip") for b in core.list_backups(name))


def test_unknown_command_and_bad_zip_path(sup):
    make_bot("qbot", LOOP)
    cid = db.enqueue("deploy", "qbot", {"zip": "/etc/passwd"})
    assert wait_for(lambda: db.fetchone("SELECT status FROM commands WHERE id=%s", (cid,))["status"] == "error",
                    sup=sup)
    cid = db.enqueue("hack", "qbot")
    assert wait_for(lambda: db.fetchone("SELECT status FROM commands WHERE id=%s", (cid,))["status"] == "error",
                    sup=sup)
