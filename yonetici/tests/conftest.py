import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="yonetici_test_"))
os.environ.update({
    "DB_BACKEND": "sqlite",
    "SQLITE_PATH": str(_TMP / "test.sqlite3"),
    "BOTS_DIR": str(_TMP / "bots"),
    "BACKUPS_DIR": str(_TMP / "backups"),
    "UPLOADS_DIR": str(_TMP / "uploads"),
    "SUPERVISOR_LOG": str(_TMP / "supervisor.log"),
    "MAIN_BOT_TOKEN": "",
    "OWNER_IDS": "111",
    "STOP_TIMEOUT": "2",
    "WEBHOOK_SECRET": "test_secret",
    "WEB_HOST": "example.test",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402
import db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init():
    core.ensure_dirs()
    db.init_db()
    yield


@pytest.fixture
def tmp():
    return _TMP
