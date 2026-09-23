#!/bin/bash
# PythonAnywhere always-on task giriş noktası.
# venv yoksa oluşturur, requirements değiştiyse kurar, tabloları/webhook'u ayarlar,
# sonra supervisor'ı başlatır. Always-on task komutu:
#   bash /home/KULLANICI/cevenc/yonetici/deploy/pa_bootstrap.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/.virtualenvs/yonetici"
PY_BIN="${PY_BIN:-python3.11}"
cd "$APP_DIR"
chmod 600 .env 2>/dev/null || true

if [ ! -x "$VENV/bin/python" ]; then
    echo "[bootstrap] venv oluşturuluyor: $VENV"
    "$PY_BIN" -m venv "$VENV"
fi

REQ_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ "$(cat "$VENV/.req_hash" 2>/dev/null || true)" != "$REQ_HASH" ]; then
    echo "[bootstrap] kütüphaneler kuruluyor"
    "$VENV/bin/pip" install --disable-pip-version-check -q -r requirements.txt
    echo "$REQ_HASH" > "$VENV/.req_hash"
fi

echo "[bootstrap] veritabanı + webhook + admin menüsü"
"$VENV/bin/python" calisan_bot.py setup
date +%s > deploy/.bootstrap_ok

echo "[bootstrap] supervisor başlıyor"
exec "$VENV/bin/python" supervisor.py
