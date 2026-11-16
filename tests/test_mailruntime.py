"""Runtime Gmail : source des identifiants, connexion testée, fichier 600, une seule relève (faux IMAP, aucun réseau)."""
import imaplib
import json
import os
import stat
import threading

import pytest

import app.receipts.mailruntime as mailruntime
from app.config import MailSettings
from app.receipts.mailruntime import (MailCredentialsInvalid, MailManagedByServer, MailNotConfigured, MailRuntime,
                                      MailUnreachable, mask_address, normalize)
from tests.test_mailsync import FakeImap

USER = "lecteur.fictif@example.test"
PASSWORD = "abcd efgh ijkl mnop"
LABELS = (("Libellé fictif", "superu"),)
SERVER = MailSettings(user="serveur@example.test", app_password="secret-serveur", labels=LABELS)


class FakeSyncer:
    """Fil de relève factice : enregistre démarrage, arrêt et passages."""

    def __init__(self, cfg, lock, log):
        self.cfg, self.lock, self.log = cfg, lock, log
        self.started = self.stopped = False
        log.append(self)

    def start(self):
        assert not any(s.started and not s.stopped for s in self.log if s is not self), "deux relèves à la fois"
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self.started and not self.stopped

    def run_once(self):
        with self.lock:
            return {"messages": 0, "files": 0, "error": None, "user": self.cfg.user}


def runtime(tmp_path, server=None, imap=None, start=True):
    log = []
    rt = MailRuntime(server, lambda cfg, lock: FakeSyncer(cfg, lock, log), tmp_path / "gmail.json", labels=LABELS,
                     interval_min=5, connect=imap or FakeImap({"INBOX": (1, {})}))
    if start:
        rt.start()
    return rt, log


def test_mask_and_normalize():
    assert mask_address("lo.fictif@gmail.com") == "lo…@gmail.com"
    assert mask_address("ab@x.test") == "a…@x.test"
    assert mask_address(None) is None
    assert normalize("  a.b@example.test ", " abcd efgh\tijkl\nmnop ") == ("a.b@example.test", "abcdefghijklmnop")
    for user, pw in [("pas-une-adresse", "abcd"), ("a@b", "abcd"), ("a@example.test", "   "), ("", "x")]:
        with pytest.raises(MailCredentialsInvalid):
            normalize(user, pw)


def test_disabled_without_credentials(tmp_path):
    rt, log = runtime(tmp_path)
    assert (rt.source, rt.enabled, rt.syncer, rt.thread, rt.address) == (None, False, None, None, None)
    assert rt.labels == LABELS and log == []
    with pytest.raises(MailNotConfigured):
        rt.run_once()


def test_server_settings_take_priority(tmp_path):
    (tmp_path / "gmail.json").write_text(json.dumps({"user": USER, "app_password": "x"}), encoding="utf-8")
    rt, log = runtime(tmp_path, server=SERVER)
    assert rt.source == "server" and rt.settings is SERVER and log[0].cfg is SERVER and log[0].started
    with pytest.raises(MailManagedByServer):
        rt.connect(USER, PASSWORD)
    with pytest.raises(MailManagedByServer):
        rt.disconnect()
    assert (tmp_path / "gmail.json").exists() and len(log) == 1


def test_connect_tests_login_then_writes_file_and_starts(tmp_path):
    imap = FakeImap({"INBOX": (1, {})})
    rt, log = runtime(tmp_path, imap=imap)
    rt.connect(f"  {USER} ", PASSWORD)
    assert imap.commands == [("connect",), ("login",), ("select", "INBOX", True), ("logout",)]
    path = tmp_path / "gmail.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"user": USER, "app_password": "abcdefghijklmnop"}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "gmail.json.tmp").exists()
    assert rt.source == "app" and rt.enabled and rt.address == "le…@example.test"
    [syncer] = log
    assert syncer.started and rt.thread is syncer and syncer.cfg.labels == LABELS and syncer.cfg.interval_min == 5
    assert rt.run_once()["user"] == USER


def test_connect_without_thread_start_keeps_manual_pass(tmp_path):
    rt, log = runtime(tmp_path, start=False)
    rt.connect(USER, PASSWORD)
    assert rt.thread is None and not log[0].started and rt.run_once()["error"] is None


@pytest.mark.parametrize("imap, error", [
    (FakeImap({"INBOX": (1, {})}, login_error=imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid")),
     MailCredentialsInvalid),
    (FakeImap({}), MailCredentialsInvalid),                         # INBOX refusée
    (FakeImap({"INBOX": (1, {})}, login_error=imaplib.IMAP4.abort("coupure")), MailUnreachable),
])
def test_connect_failure_writes_nothing(tmp_path, imap, error):
    rt, log = runtime(tmp_path, imap=imap)
    with pytest.raises(error) as info:
        rt.connect(USER, PASSWORD)
    assert "abcd" not in str(info.value) and USER not in str(info.value)
    assert not (tmp_path / "gmail.json").exists() and not (tmp_path / "gmail.json.tmp").exists()
    assert rt.source is None and log == []
    assert imap.commands[-1] == ("logout",)


def test_connect_network_failure(tmp_path):
    def unreachable(*args, **kwargs):
        raise OSError("réseau indisponible")
    rt, log = runtime(tmp_path, imap=unreachable)
    with pytest.raises(MailUnreachable):
        rt.connect(USER, PASSWORD)
    assert not (tmp_path / "gmail.json").exists() and log == []


def test_write_failure_leaves_no_tmp_and_keeps_previous(tmp_path, monkeypatch):
    rt, log = runtime(tmp_path)
    rt.connect(USER, PASSWORD)

    def broken_replace(src, dst):
        raise OSError("disque plein")
    monkeypatch.setattr(mailruntime.os, "replace", broken_replace)
    with pytest.raises(OSError):
        rt.connect("autre.fictif@example.test", "qrst uvwx yzab cdef")
    assert not (tmp_path / "gmail.json.tmp").exists()
    assert json.loads((tmp_path / "gmail.json").read_text(encoding="utf-8"))["user"] == USER
    assert rt.settings.user == USER and len(log) == 1 and not log[0].stopped


def test_reconnect_stops_old_thread_before_starting_new(tmp_path):
    rt, log = runtime(tmp_path)
    rt.connect(USER, PASSWORD)
    rt.connect("autre.fictif@example.test", "qrst uvwx yzab cdef")
    old, new = log
    assert old.stopped and new.started and rt.thread is new
    assert old.lock is new.lock            # verrou de passage commun : jamais deux passages simultanés
    assert isinstance(new.lock, type(threading.Lock()))


def test_disconnect_stops_and_deletes(tmp_path):
    rt, log = runtime(tmp_path)
    rt.connect(USER, PASSWORD)
    rt.disconnect()
    assert log[0].stopped and rt.thread is None and rt.syncer is None
    assert (rt.source, rt.enabled) == (None, False)
    assert not (tmp_path / "gmail.json").exists()
    with pytest.raises(MailNotConfigured):
        rt.run_once()
    rt.disconnect()                       # sans identifiants : sans effet


def test_restart_loads_saved_file(tmp_path):
    rt, _ = runtime(tmp_path)
    rt.connect(USER, PASSWORD)
    rt.stop()
    again, log = runtime(tmp_path)
    assert again.source == "app" and again.settings.user == USER and log[0].started


def test_unreadable_file_is_ignored(tmp_path):
    (tmp_path / "gmail.json").write_text("{pas du json", encoding="utf-8")
    rt, log = runtime(tmp_path)
    assert rt.source is None and log == []


def test_stop_prevents_further_start(tmp_path):
    rt, log = runtime(tmp_path)
    rt.connect(USER, PASSWORD)
    rt.stop()
    assert log[0].stopped and rt.thread is None
    rt.connect("autre.fictif@example.test", "qrst uvwx yzab cdef")
    assert not log[1].started and rt.thread is None
