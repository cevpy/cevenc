"""PythonAnywhere'e sıfırdan kurulum / güncelleme (PythonAnywhere API ile).

Gerekli ortam değişkenleri:
    PA_USERNAME, PA_API_TOKEN          PythonAnywhere → Account → API token
    PA_HOST                            www.pythonanywhere.com (varsayılan) veya eu.pythonanywhere.com
    MAIN_BOT_TOKEN, OWNER_IDS          ana bot token'ı ve sahip Telegram ID(ler)i
    MYSQL_PASSWORD                     Databases sekmesinde belirlenen MySQL şifresi
İsteğe bağlı: MYSQL_DB_NAME (varsayılan botyonetici), PA_PYTHON (varsayılan 3.11)

MySQL veritabanının kendisi API ile oluşturulamaz: Databases sekmesinde
MySQL'i başlatıp `botyonetici` veritabanını bir kez elle oluşturun.

Kullanım (repo kökünden):
    python yonetici/deploy/pa_deploy.py            # kur / güncelle + başlat
    python yonetici/deploy/pa_deploy.py --recreate # mevcut web app'i silip sıfırdan kur
"""
import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]          # repo kökü
APP_REL = "yonetici"
SKIP = ("yonetici/tests/", "yonetici/.env.example")


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Eksik ortam değişkeni: {name}")
    return v


class PA:
    def __init__(self, user, token, host):
        self.user = user
        self.base = f"https://{host}/api/v0/user/{user}"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Token {token}"

    def req(self, method, path, ok=(200, 201, 204), **kw):
        r = self.s.request(method, self.base + path, timeout=60, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
        return r

    def upload(self, remote, data: bytes):
        self.req("POST", f"/files/path{remote}", files={"content": ("f", data)})

    def read(self, remote):
        r = self.req("GET", f"/files/path{remote}", ok=(200, 404))
        return r.content if r.status_code == 200 else None


def remote_env_secret(pa, home):
    old = pa.read(f"{home}/cevenc/{APP_REL}/.env")
    if old:
        for line in old.decode().splitlines():
            if line.startswith("WEBHOOK_SECRET=") and len(line) > 20:
                return line.split("=", 1)[1].strip()
    return secrets.token_urlsafe(32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recreate", action="store_true", help="mevcut web app'i silip yeniden oluştur")
    args = ap.parse_args()

    user, token = need("PA_USERNAME"), need("PA_API_TOKEN")
    host = os.environ.get("PA_HOST", "www.pythonanywhere.com")
    bot_token, owners, mysql_pw = need("MAIN_BOT_TOKEN"), need("OWNER_IDS"), need("MYSQL_PASSWORD")
    pyver = os.environ.get("PA_PYTHON", "3.11")
    db_name = os.environ.get("MYSQL_DB_NAME", "botyonetici")
    region = "eu." if host.startswith("eu.") else ""
    domain = f"{user}.{region}pythonanywhere.com"
    mysql_host = f"{user}.mysql.{region}pythonanywhere-services.com"
    home = f"/home/{user}"
    app_dir = f"{home}/cevenc/{APP_REL}"
    venv = f"{home}/.virtualenvs/yonetici"
    pa = PA(user, token, host)

    print(f"→ Hesap: {user} ({host}), alan adı: {domain}")
    pa.req("GET", "/cpu/")  # token kontrolü

    # 1) Dosyalar
    files = subprocess.run(["git", "ls-files", APP_REL], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.split()
    files = [f for f in files if not f.startswith(SKIP)]
    for f in files:
        pa.upload(f"{home}/cevenc/{f}", (ROOT / f).read_bytes())
    print(f"✓ {len(files)} dosya yüklendi → {app_dir}")

    env = {
        "MAIN_BOT_TOKEN": bot_token, "OWNER_IDS": owners, "WEB_HOST": domain,
        "WEBHOOK_SECRET": remote_env_secret(pa, home),
        "DB_BACKEND": "mysql", "MYSQL_HOST": mysql_host, "MYSQL_USER": user,
        "MYSQL_PASSWORD": mysql_pw, "MYSQL_DB": f"{user}${db_name}",
        "BOT_PYTHON": f"/usr/bin/python{pyver}",
    }
    for k in ("DISK_QUOTA_MB", "DEFAULT_RAM_MB", "MAX_RAM_MB", "RAM_LIMIT_MODE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    pa.upload(f"{app_dir}/.env", "".join(f"{k}={v}\n" for k, v in env.items()).encode())
    pa.req("DELETE", f"/files/path{app_dir}/deploy/.bootstrap_ok", ok=(200, 204, 404))
    print("✓ .env yazıldı (bootstrap chmod 600 yapar)")

    # 2) Always-on task: venv + pip + setup + supervisor
    cmd = f"bash {app_dir}/deploy/pa_bootstrap.sh"
    tasks = pa.req("GET", "/always_on/").json()
    task = next((t for t in tasks if t.get("command") == cmd), None)
    if task:
        pa.req("POST", f"/always_on/{task['id']}/restart/")
        print(f"✓ Always-on task yeniden başlatıldı (#{task['id']})")
    else:
        task = pa.req("POST", "/always_on/", data={"command": cmd, "description": "Bot yöneticisi supervisor",
                                                     "enabled": "true"}).json()
        print(f"✓ Always-on task oluşturuldu (#{task.get('id')})")

    # 3) Bootstrap'ın venv'i kurup setup'ı bitirmesini bekle
    print("… venv/kütüphaneler/setup bekleniyor (birkaç dakika sürebilir)")
    for _ in range(120):
        if pa.read(f"{app_dir}/deploy/.bootstrap_ok"):
            break
        time.sleep(5)
    else:
        sys.exit("✗ Bootstrap 10 dk içinde bitmedi. Tasks sekmesinde always-on task logunu kontrol edin "
                 "(MySQL şifresi / veritabanı adı yanlış olabilir).")
    print("✓ Bootstrap tamam: tablolar, webhook ve admin menüsü hazır")

    # 4) Web app
    webapps = {w["domain_name"]: w for w in pa.req("GET", "/webapps/").json()}
    if domain in webapps and args.recreate:
        pa.req("DELETE", f"/webapps/{domain}/")
        del webapps[domain]
        print("✓ Eski web app silindi")
    if domain not in webapps:
        pa.req("POST", "/webapps/", data={"domain_name": domain, "python_version": f"python{pyver.replace('.', '')}"})
        print("✓ Web app oluşturuldu")
    pa.req("PATCH", f"/webapps/{domain}/", data={"virtualenv_path": venv, "source_directory": app_dir,
                                                  "force_https": "true"})
    wsgi = (f"import sys\nsys.path.insert(0, {app_dir!r})\n"
            "from calisan_bot import app as application  # noqa\n")
    pa.upload(f"/var/www/{domain.replace('.', '_')}_wsgi.py", wsgi.encode())
    pa.req("POST", f"/webapps/{domain}/reload/")
    print("✓ WSGI yazıldı, web app yeniden yüklendi")

    # 5) Doğrulama
    time.sleep(5)
    r = requests.get(f"https://{domain}/", timeout=30)
    print(("✓" if r.ok else "✗") + f" https://{domain}/ → {r.status_code} {r.text[:60]!r}")
    info = requests.get(f"https://api.telegram.org/bot{bot_token}/getWebhookInfo", timeout=15).json()["result"]
    print(f"✓ Webhook: {info.get('url')} (bekleyen {info.get('pending_update_count')}, "
          f"son hata: {info.get('last_error_message') or '-'})")
    print("\nBitti. Telegram'da ana bota /start yazın.")


if __name__ == "__main__":
    main()
