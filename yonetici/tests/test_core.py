import os
import stat
import zipfile

import pytest

import config
import core
from core import UserError
from envfile import dump_env, parse_env_text


def make_zip(path, files, symlink=None):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return path


@pytest.mark.parametrize("name,ok", [
    ("my_bot1", True), ("ab", True), ("a", False), ("Bot", False), ("../x", False), ("a-b", False),
    ("a" * 33, False), ("", False), ("bot adı", False),
])
def test_validate_name(name, ok):
    if ok:
        assert core.validate_name(name) == name
    else:
        with pytest.raises(UserError):
            core.validate_name(name)


@pytest.mark.parametrize("evil", ["../evil.py", "/abs/evil.py", "a/../../evil.py", "C:/evil.py", "..\\evil.py"])
def test_zip_slip_rejected(tmp, evil):
    z = make_zip(tmp / "evil.zip", {"main.py": "print(1)", evil: "x"})
    with pytest.raises(UserError):
        core.safe_extract(z, tmp / "out_evil")
    assert not (tmp / "evil.py").exists()


def test_zip_symlink_rejected(tmp):
    z = make_zip(tmp / "link.zip", {"main.py": "print(1)"}, symlink="passwd")
    with pytest.raises(UserError):
        core.inspect_zip(z)


def test_zip_prefix_strip_and_skip(tmp):
    z = make_zip(tmp / "pref.zip", {
        "proje/main.py": "print(1)", "proje/lib/x.py": "", "proje/venv/bin/python": "",
        "proje/.env": "BOT_TOKEN=123456:" + "A" * 35, "__MACOSX/proje/._main.py": "",
    })
    meta = core.inspect_zip(z)
    assert meta["prefix"] == "proje/"
    assert core.tokens_in_env(meta["env"])
    out = tmp / "out_pref"
    written = core.safe_extract(z, out)
    assert sorted(written) == [".env", "lib/x.py", "main.py"]
    assert not (out / "venv").exists()


def test_zip_requires_main(tmp):
    z = make_zip(tmp / "nomain.zip", {"bot.py": ""})
    with pytest.raises(UserError, match="main.py"):
        core.inspect_zip(z)


def test_zip_bomb_limit(tmp, monkeypatch):
    monkeypatch.setattr(config, "MAX_UNZIPPED_BYTES", 1000)
    z = make_zip(tmp / "big.zip", {"main.py": "x" * 5000})
    with pytest.raises(UserError, match="boyut"):
        core.inspect_zip(z)


def test_env_roundtrip():
    data = {"A": "1", "B": 'x y "z"', "C": "", "D": "a#b", "E": "back\\slash"}
    assert parse_env_text(dump_env(data)) == data
    assert parse_env_text("export X='y'\n# yorum\nbad line\n1X=2\n") == {"X": "y"}


def test_safe_join(tmp):
    with pytest.raises(UserError):
        core.safe_join(tmp / "bots", "../x")
    assert core.safe_join(tmp / "bots", "a/b.py") == (tmp / "bots" / "a" / "b.py").resolve()


def test_rotate_log(tmp):
    p = tmp / "rot.log"
    p.write_bytes(b"x" * 200)
    fh = open(p, "ab")
    assert core.rotate_log(p, max_bytes=100, keep=2)
    fh.write(b"after")
    fh.flush()
    assert p.read_bytes() == b"after"  # O_APPEND: kesildikten sonra dosya başına yazar
    assert (tmp / "rot.log.1").stat().st_size == 200
    p.write_bytes(b"y" * 200)
    core.rotate_log(p, max_bytes=100, keep=2)
    assert (tmp / "rot.log.2").exists()
    fh.close()


def test_filter_and_last_error(tmp):
    d = config.BOTS_DIR / "errbot" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bot.log").write_text("ok\nTraceback (most recent call last):\n  File \"main.py\", line 1\n"
                               "ZeroDivisionError: division by zero\nsonra\n")
    err = core.extract_last_error("errbot")
    assert err.startswith("Traceback") and err.endswith("ZeroDivisionError: division by zero")
    assert "ok" not in core.filter_errors(core.tail_lines(d / "bot.log"))


def test_check_token_offline(monkeypatch):
    monkeypatch.setattr(config, "MAIN_BOT_TOKEN", "999999:" + "M" * 35)
    with pytest.raises(UserError, match="Yönetici"):
        core.check_token("999999:" + "X" * 35, online=False)
    with pytest.raises(UserError, match="biçim"):
        core.check_token("abc", online=False)
    assert core.check_token("123456:" + "X" * 35, online=False)["tg_bot_id"] == 123456


def test_requirements_edit(tmp):
    d = config.BOTS_DIR / "reqbot"
    d.mkdir(parents=True, exist_ok=True)
    (d / "requirements.txt").write_text("requests==2.0\n")
    core.add_requirement("reqbot", "Requests>=2.31")
    core.add_requirement("reqbot", "pyTelegramBotAPI")
    assert core.requirements("reqbot") == ["Requests>=2.31", "pyTelegramBotAPI"]
    with pytest.raises(UserError):
        core.add_requirement("reqbot", "--index-url http://evil")
    assert core.remove_requirement("reqbot", "pytelegrambotapi")
    assert core.requirements("reqbot") == ["Requests>=2.31"]


def test_backup_excludes(tmp):
    d = config.BOTS_DIR / "bkbot"
    for sub in ("venv/bin", "logs", "data", "pkg"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text("v1")
    (d / "pkg" / "m.py").write_text("")
    (d / "data" / "db.sqlite").write_text("d")
    (d / "logs" / "bot.log").write_text("l")
    (d / "venv" / "bin" / "python").write_text("")
    (d / ".env").write_text("A=1")
    p = core.make_backup("bkbot")
    names = sorted(zipfile.ZipFile(p).namelist())
    assert names == [".env", "main.py", "pkg/m.py"]
    assert core.list_backups("bkbot")[0]["file"] == p.name
    with pytest.raises(UserError):
        core.backup_file("bkbot", "../x.zip")
    os.remove(p)
