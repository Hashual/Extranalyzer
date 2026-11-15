import email.message
import imaplib

import pytest

from app.config import MailSettings
from app.receipts.mailsync import MailSyncThread, fetch_new, sync_once
from app.store.db import connect

LABEL = "Super U/Tickets de caisses"
OTHER = "intermarche/Tickets de caisses"


def make_mail(n: int, message_id: str | None = None) -> bytes:
    msg = email.message.EmailMessage()
    msg["Message-ID"] = message_id or f"<m{n}@test>"
    msg["Subject"] = "Votre ticket"
    msg.set_content("corps")
    stamp = f"0{n}012025-10101{n}"
    msg.add_attachment(b"%PDF-1.4\nticket" + str(n).encode(), maintype="application", subtype="pdf",
                       filename=f"Ticket {stamp}.pdf")
    msg.add_attachment(b"%PDF-1.4\nslip" + str(n).encode(), maintype="application", subtype="pdf",
                       filename=f"Ticket de CB_{stamp}.pdf")
    msg.add_attachment(b"texte", maintype="text", subtype="plain", filename="notes.txt")
    return msg.as_bytes()


class FakeImap:
    """Faux serveur IMAP en mémoire : {libellé: (uidvalidity, {uid: message brut})}."""

    def __init__(self, boxes, login_error=None):
        self.boxes, self.login_error, self.commands, self.current = boxes, login_error, [], None

    def __call__(self, *args, **kwargs):      # sert de fabrique `connect`
        self.commands.append(("connect",))
        return self

    def login(self, user, password):
        self.commands.append(("login",))
        if self.login_error:
            raise self.login_error
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))
        name = mailbox.strip('"')
        if name not in self.boxes:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        self.current = name
        return "OK", [str(len(self.boxes[name][1])).encode()]

    def response(self, code):
        self.commands.append(("response", code))
        return code, [str(self.boxes[self.current][0]).encode()]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        messages = self.boxes[self.current][1]
        if command == "SEARCH":
            start = int(args[0].split()[1].split(":")[0])
            found = [u for u in sorted(messages) if u >= start] or sorted(messages)[-1:]   # comme Gmail
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            return "OK", [(b"%s (UID %s BODY[] {10}" % (args[0].encode(), args[0].encode()),
                           messages[int(args[0])]), b")"]
        raise AssertionError(f"commande interdite : {command}")

    def logout(self):
        self.commands.append(("logout",))
        return "BYE", [b""]


def cfg(labels=((LABEL, "superu"),)):
    return MailSettings(user="u@example.test", app_password="secret", labels=tuple(labels))


@pytest.fixture
def write(tmp_path):
    def _write(fn):
        conn = connect(tmp_path / "t.db")
        try:
            return fn(conn)
        finally:
            conn.close()
    return _write


def rows(write, sql):
    return write(lambda c: c.execute(sql).fetchall())


def test_sync_is_incremental_and_idempotent(tmp_path, write):
    imap = FakeImap({LABEL: (7, {1: make_mail(1), 2: make_mail(2)})})
    first = sync_once(cfg(), write, tmp_path / "r", [], imap)
    assert first == {"messages": 2, "files": 4, "error": None}
    assert rows(write, "SELECT store, status FROM receipts") == [("superu", "pending_parser")] * 2
    assert rows(write, "SELECT COUNT(*) FROM receipt_slips WHERE receipt_id IS NOT NULL") == [(2,)]
    assert rows(write, "SELECT last_uid, uidvalidity, last_error FROM mail_sync") == [(2, 7, None)]
    assert rows(write, "SELECT last_success_at IS NOT NULL FROM mail_sync") == [(1,)]
    imap.commands.clear()
    second = sync_once(cfg(), write, tmp_path / "r", [], imap)
    assert second == {"messages": 0, "files": 0, "error": None}
    assert ("SEARCH", "UID 3:*") in imap.commands
    assert not [c for c in imap.commands if c[0] == "FETCH"]      # le dernier message renvoyé par Gmail est ignoré
    assert rows(write, "SELECT COUNT(*) FROM receipts") == [(2,)]


def test_sync_never_writes_flags(tmp_path, write):
    imap = FakeImap({LABEL: (7, {1: make_mail(1)})})
    sync_once(cfg(), write, tmp_path / "r", [], imap)
    selects = [c for c in imap.commands if c[0] == "select"]
    assert selects == [("select", f'"{LABEL}"', True)]
    fetches = [c for c in imap.commands if c[0] == "FETCH"]
    assert fetches and all(c[2] == "(BODY.PEEK[])" for c in fetches)
    assert {c[0] for c in imap.commands} <= {"connect", "login", "select", "response", "SEARCH", "FETCH", "logout"}


def test_uidvalidity_change_restarts_from_zero(tmp_path, write):
    boxes = {LABEL: (7, {1: make_mail(1), 2: make_mail(2)})}
    sync_once(cfg(), write, tmp_path / "r", [], FakeImap(boxes))
    imap = FakeImap({LABEL: (8, {1: make_mail(1), 2: make_mail(2), 3: make_mail(3)})})
    result = sync_once(cfg(), write, tmp_path / "r", [], imap)
    assert ("SEARCH", "UID 1:*") in imap.commands
    assert result["messages"] == 1 and result["error"] is None
    assert rows(write, "SELECT COUNT(*) FROM receipts") == [(3,)]
    assert rows(write, "SELECT COUNT(*) FROM receipt_slips") == [(3,)]
    assert rows(write, "SELECT last_uid, uidvalidity FROM mail_sync") == [(3, 8)]


def test_known_message_id_is_skipped(tmp_path, write):
    sync_once(cfg(), write, tmp_path / "r", [], FakeImap({LABEL: (7, {1: make_mail(1, "<same>")})}))
    boxes = {LABEL: (7, {1: make_mail(1, "<same>"), 2: make_mail(2, "<same>")})}
    result = sync_once(cfg(), write, tmp_path / "r", [], FakeImap(boxes))
    assert result["messages"] == 0
    assert rows(write, "SELECT COUNT(*) FROM receipts") == [(1,)]
    assert rows(write, "SELECT last_uid FROM mail_sync") == [(2,)]


def test_sync_auth_error_is_recorded_not_raised(tmp_path, write):
    imap = FakeImap({LABEL: (7, {1: make_mail(1)})},
                    login_error=imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"))
    result = sync_once(cfg(), write, tmp_path / "r", [], imap)
    assert result["error"] and result["messages"] == 0
    assert "u@example.test" not in result["error"]
    assert rows(write, "SELECT last_error IS NOT NULL, last_success_at, last_attempt_at IS NOT NULL "
                       "FROM mail_sync") == [(1, None, 1)]


def test_missing_label_does_not_stop_other_label(tmp_path, write):
    imap = FakeImap({LABEL: (7, {1: make_mail(1)})})
    result = sync_once(cfg([(OTHER, "intermarche"), (LABEL, "superu")]), write, tmp_path / "r", [], imap)
    assert result["messages"] == 1 and result["error"]
    got = {r[0]: r[1:] for r in rows(write, "SELECT label, last_error IS NOT NULL, last_success_at IS NOT NULL, "
                                            "last_uid FROM mail_sync")}
    assert got == {OTHER: (1, 0, 0), LABEL: (0, 1, 1)}


def test_network_error_is_recorded(tmp_path, write):
    def boom(*a, **k):
        raise OSError("réseau coupé")
    result = sync_once(cfg(), write, tmp_path / "r", [], boom)
    assert result["error"].startswith("OSError")


def test_fetch_new_yields_only_pdf_attachments():
    imap = FakeImap({LABEL: (7, {1: make_mail(1)})})
    [msg] = list(fetch_new(cfg(), {}, imap))
    assert (msg.label, msg.store, msg.uidvalidity, msg.uid, msg.message_id) == (LABEL, "superu", 7, 1, "<m1@test>")
    assert [n for n, _ in msg.attachments] == ["Ticket 01012025-101011.pdf", "Ticket de CB_01012025-101011.pdf"]


def test_thread_runs_at_start_and_on_trigger(tmp_path, write):
    imap = FakeImap({LABEL: (7, {1: make_mail(1)})})
    thread = MailSyncThread(cfg(), write, tmp_path / "r", [], imap)
    thread.start()
    try:
        assert thread.wait_pass(5)
        assert rows(write, "SELECT last_uid FROM mail_sync") == [(1,)]
        imap.boxes[LABEL][1][2] = make_mail(2)
        thread.trigger()
        assert thread.wait_pass(5)
        assert rows(write, "SELECT last_uid FROM mail_sync") == [(2,)]
    finally:
        thread.stop()
        thread.join(5)
    assert not thread.is_alive()


def test_message_is_atomic_when_ingestion_fails_midway(tmp_path, write, monkeypatch):
    import sqlite3

    import app.receipts.mailsync as mailsync

    real, calls = mailsync.ingest_file, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 4:             # 2e pièce jointe du 2e message
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)
    monkeypatch.setattr(mailsync, "ingest_file", flaky)
    imap = FakeImap({LABEL: (7, {1: make_mail(1), 2: make_mail(2)})})
    result = sync_once(cfg(), write, tmp_path / "r", [], imap)
    assert result["error"] and result["messages"] == 1
    assert rows(write, "SELECT COUNT(*) FROM receipts") == [(1,)]       # rien du message 2
    assert rows(write, "SELECT COUNT(*) FROM receipt_slips") == [(1,)]
    assert rows(write, "SELECT last_uid, last_success_at FROM mail_sync") == [(1, None)]
    monkeypatch.setattr(mailsync, "ingest_file", real)                  # reprise : le message 2 est rejoué
    again = sync_once(cfg(), write, tmp_path / "r", [], FakeImap({LABEL: (7, {1: make_mail(1), 2: make_mail(2)})}))
    assert again == {"messages": 1, "files": 2, "error": None}
    assert rows(write, "SELECT COUNT(*) FROM receipts") == [(2,)]


def test_unexpected_error_is_recorded_not_raised(tmp_path, write):
    def boom(*a, **k):
        raise ValueError("inattendu")
    result = sync_once(cfg(), write, tmp_path / "r", [], boom)
    assert result["error"].startswith("ValueError")
    assert rows(write, "SELECT last_error FROM mail_sync")[0][0].startswith("ValueError")
