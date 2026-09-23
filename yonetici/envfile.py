"""Basit .env okuma/yazma (python-dotenv bağımlılığı olmadan)."""
import re
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_text(text: str) -> dict:
    """KEY=VALUE satırlarını sözlüğe çevirir. Yorum/boş satırlar atlanır."""
    data = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not KEY_RE.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        data[key] = value
    return data


def read_env(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    return parse_env_text(p.read_text(encoding="utf-8", errors="replace"))


def _quote(value: str) -> str:
    if value == "" or re.search(r"[\s#\"']", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def dump_env(data: dict) -> str:
    return "".join(f"{k}={_quote(str(v))}\n" for k, v in data.items())


def write_env(path, data: dict):
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(dump_env(data), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)
