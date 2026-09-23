# Bot Yöneticisi (PythonAnywhere)

Başka Telegram botlarını çalıştıran, izleyen ve yöneten yönetici bot.

```
┌──────────── Web App (Flask) ────────────┐        ┌──────── Always-on task ────────┐
│ calisan_bot.py  ← webhook (gizli başlık)│        │ supervisor.py                  │
│ miniapp.py      ← /app + /api (initData)│        │  ├─ bots/haber/  (venv, cwd)   │
│   dosya işlemleri, .env, yedek, loglar  │        │  ├─ bots/oyun/   (venv, cwd)   │
└───────────────┬─────────────────────────┘        │  └─ izleme, çökme, RAM/CPU     │
                │     MySQL: commands (kuyruk),    └───────────────┬────────────────┘
                └──── bots, settings, admins, ────────────────────┘
                      audit_log, user_state
```

Web app ile always-on task PythonAnywhere'de **farklı makinelerde** çalışır; ortak olan dosya
sistemi ve MySQL'dir. Bu yüzden başlat/durdur/kur gibi süreç işlemleri web app'ten
`commands` tablosuna yazılır, supervisor her saniye kuyruğu okur. Sistem istatistikleri
(CPU/RAM/disk) de botların çalıştığı makineden, supervisor tarafından yazılır.

## Dosyalar

| Dosya | Görev |
|---|---|
| `config.py`, `envfile.py` | `.env`'den yapılandırma |
| `db.py` | MySQL/SQLite katmanı, şema, komut kuyruğu, ayarlar, adminler, işlem geçmişi |
| `core.py` | isim/token doğrulama, zip slip korumalı açma, yedek, log döndürme/okuma, paketler |
| `supervisor.py` | alt botları başlatır/izler/yeniden açar (always-on task) |
| `_launch.py` | alt bot başlatıcısı: `setrlimit` + `nice`, sonra `exec` |
| `ui.py` | bot ekranları (hem ana bot hem supervisor bildirimleri kullanır) |
| `services.py` | bot ve Mini App'in ortak iş mantığı |
| `calisan_bot.py` | ana bot: Flask + pyTelegramBotAPI, webhook |
| `miniapp.py`, `templates/miniapp.html` | Mini App paneli ve API'si |

## Kurulum

1. **Kod ve sanal ortam** (Bash konsolu):
   ```bash
   git clone <repo> ~/cevenc && cd ~/cevenc/yonetici
   mkvirtualenv yonetici --python=python3.11
   pip install -r requirements.txt
   cp .env.example .env && chmod 600 .env && nano .env
   ```
2. **MySQL**: *Databases* sekmesinde `botyonetici` veritabanını oluşturun
   (tam adı `kullaniciadi$botyonetici` olur), şifreyi `.env`'e yazın.
3. **Web app**: *Web* → *Add a new web app* → *Manual configuration* (Python 3.11).
   - *Virtualenv*: `/home/kullaniciadi/.virtualenvs/yonetici`
   - *WSGI configuration file*:
     ```python
     import sys
     sys.path.insert(0, "/home/kullaniciadi/cevenc/yonetici")
     from calisan_bot import app as application
     ```
   - *Force HTTPS*: açık. Sonra **Reload**.
4. **Tablolar + webhook + admin menüsü**:
   ```bash
   cd ~/cevenc/yonetici && workon yonetici
   python calisan_bot.py setup
   ```
5. **Always-on task** (*Tasks* sekmesi):
   ```
   /home/kullaniciadi/.virtualenvs/yonetici/bin/python /home/kullaniciadi/cevenc/yonetici/supervisor.py
   ```
6. **BotFather**: `/setdomain` gerekmez; Mini App menü düğmesi admin sohbetlerine `setup`
   sırasında otomatik eklenir. Telegram'da ana bota `/start` yazın.

Yerel geliştirme: `.env`'de `DB_BACKEND=sqlite` yapıp `python calisan_bot.py polling` ve
ayrı bir terminalde `python supervisor.py`.

## Alt bot yapısı

```
bots/<ad>/            ← süreç bu klasörde çalışır (cwd)
  main.py             ← giriş noktası (zorunlu)
  requirements.txt
  .env                ← ortam değişkeni olarak da verilir (chmod 600)
  venv/               ← bota özel, supervisor oluşturur
  data/               ← kalıcı veri (BOT_DATA_DIR); güncelleme/geri almada korunur
  logs/bot.log        ← stdout+stderr, 5 MB'ta döndürülür (bot.log.1..3)
  logs/install.log    ← pip çıktısı
backups/<ad>/*.zip    ← kod + .env + requirements (venv/logs/data hariç)
```

Alt bota verilen ortam sıfırdan kurulur: `PATH`, `HOME`, dil ayarları, `BOT_NAME`,
`BOT_DATA_DIR` ve botun kendi `.env`'i. Yöneticinin MySQL şifresi ve ana token'ı **aktarılmaz**.
Göreli yol kullanan botlar (`sqlite3.connect("veri.db")`) kendi klasörlerine yazar; çakışma olmaz.

## Güvenlik ve sınırlar

- **Bot adı**: yalnızca `a-z 0-9 _`, 2–32 karakter; tüm yollar bu doğrulamadan geçer.
- **Zip**: mutlak yol, `..`, sembolik bağlantı, şifreli zip reddedilir; dosya sayısı ve açılmış
  boyut sınırlıdır (zip bombası). Tek üst klasörlü zip'ler otomatik düzleştirilir.
- **Token**: biçim, ana botun token'ı (yasak), başka bir alt botta kullanımı, `getMe` (401) ve
  `getUpdates` ile **409 Conflict** (başka yerde çalışıyor) kontrol edilir. Kontrol bekleyen
  güncellemeleri silmez.
- **Webhook**: `X-Telegram-Bot-Api-Secret-Token` başlığı eşleşmezse 403.
- **Mini App**: her API isteğinde `initData` HMAC-SHA256 imzası ve 24 saatlik yaş kontrolü;
  kullanıcı admin değilse 403.
- **RAM**: `setrlimit(RLIMIT_AS)` (veya `RAM_LIMIT_MODE=DATA`) + psutil ile RSS izleme ve uyarı.
  128 MB AS sınırında `telebot` + `requests` + birkaç thread sorunsuz açılır (test edildi).
- **CPU**: `nice +10`; psutil ile süreç ağacının CPU'su izlenir. `cpu_warn_percent` üstünde
  `cpu_window_sec` boyunca kalırsa uyarı, `cpu_kill_percent` (varsayılan kapalı) üstünde durdurma.
- **Çökme**: artan bekleme (1, 2, 4, 8 … en fazla 300 sn). 120 sn sağlıklı çalışınca sayaç
  sıfırlanır. `crash_window_sec` (60) içinde `crash_limit` (5) çökme → durdur + bildir.
- **Durdurma**: tüm süreç grubuna SIGTERM, 10 sn sonra SIGKILL (botun açtığı alt süreçler dahil).
- **Supervisor yeniden başlarken** önceki çalıştırmadan kalan bot süreçleri bulunup kapatılır,
  `autostart` açık botlar yeniden başlatılır. Aynı anda tek supervisor çalışabilir (dosya kilidi).

## Telegram özellikleri

- Tek mesajda gezinme (mesaj düzenlenir); yazı/dosya girdisinden sonra panel en alta taşınır.
- Admin'e özel komut menüsü (`BotCommandScopeChat`) ve Mini App menü düğmesi; admin olmayanlar
  komut görmez.
- Loglar ve hatalar `<blockquote expandable>` içinde; **📋 kopyala** düğmesi (`copy_text`).
- Girdilere tepki: 👌 kabul, 👎 hata, ✍ dosya işleniyor. Token/.env içeren mesajlar silinir.
- `/durum`: sabitlenen ve supervisor tarafından dakikada bir güncellenen canlı durum mesajı.
- Supervisor bir komutu bitirince komutu veren panel mesajını güncel bot kartıyla yeniler.

## Testler

```bash
pip install pytest
python -m pytest tests -q
```

Testler SQLite modunda çalışır; supervisor testleri gerçek alt süreçler başlatır (çökme döngüsü,
SIGTERM'i yok sayan bot, RAM sınırı, gerçek venv ile deploy → güncelleme → geri alma → silme).
