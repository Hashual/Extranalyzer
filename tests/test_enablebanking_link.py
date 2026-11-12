"""Connexion, rattachement des comptes et synchronisation Enable Banking (faux serveur, données fictives)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.classify.pipeline import on_new_transactions
from app.config import BankSettings
from app.sources.enablebanking.client import Client, EnableBankingError
from app.sources.enablebanking.link import (StateError, accept_gap, account_kind, attach, bank_status,
                                            complete_connection, consume_state, list_aspsps, revoke, set_kind,
                                            start_connection, sync)
from app.store.db import connect
from fakebank import FakeBank, booked, rsa_key_pem

NOW = datetime(2025, 3, 10, 8, 0, tzinfo=timezone.utc)
IBAN_CC = "FR7600000000000000000000142"       # contient le numéro fictif 00000000001
IBAN_LIVRET = "FR7600000000000000000000299"   # contient 00000000002
BANK = BankSettings("app-fictive", b"", "http://10.0.0.1:8000/bank/callback")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '00000000001', 'C/C FICTIF', 'checking'), "
              "(2, '00000000002', 'LIVRET BLEU FICTIF', 'savings')")
    c.execute("INSERT INTO documents(id, sha256, stored_path, parser, imported_at) VALUES (1, 's', 's.pdf', 'p', 'x')")
    for account_id, closing in ((1, 100000), (2, 50000)):
        c.execute("INSERT INTO sections(document_id, account_id, statement_date, opening_cents, closing_cents, "
                  "gap_cents, status, content_hash) VALUES (1, ?, '2025-02-28', 0, ?, 0, 'ok', 'h')",
                  (account_id, closing))
    c.commit()
    yield c
    c.close()


def _client(bank):
    return Client("app-fictive", rsa_key_pem(), transport=bank, clock=lambda: NOW.timestamp())


def _account(uid, iban, kind="CACC", name="Compte fictif", id_hash=None):
    return {"uid": uid, "account_id": {"iban": iban}, "name": name, "currency": "EUR", "cash_account_type": kind,
            "identification_hash": id_hash or f"h-{iban}"}


def _session(*accounts, session_id="sess-1", valid_until="2025-09-06T08:00:00+00:00"):
    return {"session_id": session_id, "aspsp": {"name": "Crédit Mutuel", "country": "FR"},
            "access": {"valid_until": valid_until}, "accounts": list(accounts)}


def _external(conn):
    return conn.execute("SELECT uid, account_id, ignored FROM external_accounts ORDER BY id").fetchall()


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, conn, ids):
        assert conn.in_transaction
        self.calls.append(list(ids))


def _run(conn, client, on_new=None, trigger="manual", now=NOW):
    return sync(lambda fn: fn(conn), lambda fn: fn(conn), client, trigger, on_new or Recorder(), lambda: now)


# --- Connexion -------------------------------------------------------------------------------------------------

def test_start_connection_registers_state_and_returns_bank_url(conn):
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [
        {"name": "Autre banque", "country": "FR"},
        {"name": "Credit Mutuel", "country": "FR", "maximum_consent_validity": 15552000}]})
    bank.on("POST", "/auth", {"url": "https://banque.example/auth?x=1", "authorization_id": "a"})
    url = start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW)
    assert url == "https://banque.example/auth?x=1"
    body = bank.calls("POST", "/auth")[0]["body"]
    assert body["aspsp"] == {"name": "Credit Mutuel", "country": "FR"}
    assert body["access"]["valid_until"] == "2025-09-06T07:00:00+00:00"          # 180 jours moins 1 h de marge
    assert body["redirect_url"] == BANK.redirect_url
    assert conn.execute("SELECT state FROM bank_auth_states").fetchone()[0] == body["state"]
    assert len(body["state"]) >= 40


def test_start_connection_unknown_bank(conn):
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [{"name": "Autre banque"}]})
    with pytest.raises(EnableBankingError, match="absente"):
        start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW)


def test_state_is_single_use_and_expires(conn):
    conn.execute("INSERT INTO bank_auth_states(state, created_at) VALUES ('s1', ?), ('s2', ?)",
                 (NOW.isoformat(), (NOW - timedelta(minutes=31)).isoformat()))
    consume_state(conn, "s1", NOW)
    with pytest.raises(StateError):
        consume_state(conn, "s1", NOW)          # rejoué
    with pytest.raises(StateError):
        consume_state(conn, "s2", NOW)          # expiré
    with pytest.raises(StateError):
        consume_state(conn, "inconnu", NOW)


def test_accounts_attached_by_iban_or_created(conn):
    session = _session(_account("u-cc", IBAN_CC), _account("u-liv", IBAN_LIVRET, "SVGS"),
                       _account("u-new", "FR7611111111111111111111111", "SVGS", "Livret neuf"))
    complete_connection(conn, _client(FakeBank()), session, NOW)
    rows = _external(conn)
    assert rows[:2] == [("u-cc", 1, 0), ("u-liv", 2, 0)]
    new_account = rows[2][1]
    assert conn.execute("SELECT number, label, kind, source FROM accounts WHERE id = ?", (new_account,)).fetchone() == \
        ("FR7611111111111111111111111", "Livret neuf", "savings", "enablebanking")


def test_ambiguous_account_left_to_attach(conn):
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (3, '00000000001-B', 'Autre', 'checking')")
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC)), NOW)
    assert _external(conn) == [("u-cc", None, 0)]
    assert bank_status(conn, NOW)["to_attach"] == 1
    attach(conn, 1, 3)
    assert _external(conn) == [("u-cc", 3, 0)]


def test_renewal_keeps_attachment(conn):
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC, id_hash="H1")), NOW)
    attach(conn, 1, 2)          # choix manuel, différent du rattachement automatique
    done = complete_connection(conn, _client(FakeBank()),
                               _session(_account("u-cc-2", IBAN_CC, id_hash="H1"), session_id="sess-2"), NOW)
    assert done.replaced_sessions == ["sess-1"]
    assert _external(conn) == [("u-cc-2", 2, 0)]
    assert conn.execute("SELECT session_id, status FROM bank_links ORDER BY id").fetchall() == \
        [("sess-1", "replaced"), ("sess-2", "active")]


def test_attachment_frozen_once_imported(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    bank = FakeBank().on("GET", "/accounts/u-cc/transactions", {"transactions": [booked("2025-03-03", "5.00", "CB")]})
    bank.on("GET", "/accounts/u-cc/balances", {"balances": []})
    _run(conn, _client(bank))
    with pytest.raises(ValueError):
        attach(conn, 1, 2)
    attach(conn, 1, None, ignore=True)
    assert _external(conn) == [("u-cc", 1, 1)]
    attach(conn, 1, 1)          # reprise
    assert _external(conn) == [("u-cc", 1, 0)]


def test_revoke(conn):
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC)), NOW)
    assert revoke(conn) == ["sess-1"]
    assert bank_status(conn, NOW) is None


# --- Synchronisation ------------------------------------------------------------------------------------------

def _connected(conn, *accounts):
    complete_connection(conn, _client(FakeBank()), _session(*accounts), NOW)
    conn.commit()


def _bank_for(uid, transactions, balances=()):
    bank = FakeBank().on("GET", f"/accounts/{uid}/transactions", {"transactions": transactions})
    return bank.on("GET", f"/accounts/{uid}/balances", {"balances": list(balances)})


def _ops(conn):
    return conn.execute("SELECT account_id, op_date, amount_cents, label FROM transactions ORDER BY op_date, id").fetchall()


def test_sync_imports_booked_after_frontier(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    bank = _bank_for("u-cc", [
        booked("2025-02-27", "1.00", "AVANT FRONTIERE"),
        booked("2025-03-01", "12.50", "CB MAGASIN FICTIF"),
        {**booked("2025-03-02", "3.00", "EN ATTENTE"), "status": "PDNG"},
        {**booked("2025-03-02", "3.00", "DOLLARS"), "transaction_amount": {"amount": "3", "currency": "USD"}},
        booked("2025-03-03", "100.00", "VIR RECU FICTIF", indicator="CRDT"),
    ], [{"balance_type": "CLBD", "balance_amount": {"amount": "1087.50", "currency": "EUR"},
         "reference_date": "2025-03-09"}])
    rec = Recorder()
    summary = _run(conn, _client(bank), rec)
    assert _ops(conn) == [(1, "2025-03-01", -1250, "CB MAGASIN FICTIF"), (1, "2025-03-03", 10000, "VIR RECU FICTIF")]
    assert (summary.added, summary.skipped, summary.errors) == (2, 1, 1)
    assert len(rec.calls) == 1 and len(rec.calls[0]) == 2
    request = bank.calls("GET", "/accounts/u-cc/transactions")[0]["query"]
    assert request == {"date_from": "2025-03-01", "strategy": "longest"}       # frontière + 1, première synchro
    assert conn.execute("SELECT balance_cents, balance_date, balance_gap_cents, last_sync_at IS NOT NULL "
                        "FROM external_accounts").fetchone() == (108750, "2025-03-09", 0, 1)
    assert conn.execute("SELECT trigger, added FROM sync_runs").fetchone() == ("manual", 2)


def test_resync_window_overlaps_and_adds_nothing_twice(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    ops = [booked("2025-03-01", "12.50", "CB MAGASIN FICTIF"), booked("2025-03-05", "2.00", "CB BOULANGERIE")]
    _run(conn, _client(_bank_for("u-cc", ops)))
    bank = _bank_for("u-cc", ops + [booked("2025-03-08", "4.00", "CB NOUVELLE")])
    summary = _run(conn, _client(bank), now=NOW + timedelta(hours=12))
    assert summary.added == 1 and len(_ops(conn)) == 3
    assert bank.calls("GET", "/accounts/u-cc/transactions")[0]["query"] == {"date_from": "2025-03-01",
                                                                             "strategy": "default"}


def test_lookback_window_after_long_history(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    _run(conn, _client(_bank_for("u-cc", [booked("2025-04-20", "1.00", "CB")])))
    bank = _bank_for("u-cc", [])
    _run(conn, _client(bank))
    assert bank.calls("GET", "/accounts/u-cc/transactions")[0]["query"]["date_from"] == "2025-04-10"


def test_balance_gap_and_acceptance(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    balance = [{"balance_type": "CLBD", "balance_amount": {"amount": "990.00", "currency": "EUR"},
                "reference_date": "2025-03-09"}]
    _run(conn, _client(_bank_for("u-cc", [booked("2025-03-01", "12.50", "CB")], balance)))
    assert conn.execute("SELECT balance_gap_cents FROM external_accounts").fetchone()[0] == 250   # 990 - (1000 - 12.50)
    accept_gap(conn, 1)
    conn.commit()
    _run(conn, _client(_bank_for("u-cc", [booked("2025-03-01", "12.50", "CB")], balance)))
    assert conn.execute("SELECT balance_gap_cents, accepted_gap_cents FROM external_accounts").fetchone() == (0, 250)


def test_session_expired_marks_link_and_alerts(conn):
    _connected(conn, _account("u-cc", IBAN_CC), _account("u-liv", IBAN_LIVRET, "SVGS"))
    bank = FakeBank().on("GET", "/accounts/u-cc/transactions", (401, {"error": "EXPIRED_SESSION", "message": "x"}))
    summary = _run(conn, _client(bank))
    assert summary.errors == 1
    assert len(bank.requests) == 1                     # le compte suivant du même lien n'est pas interrogé
    assert conn.execute("SELECT status FROM bank_links").fetchone()[0] == "expired"
    assert bank_status(conn, NOW)["status"] == "expired"


def test_rate_limit_pauses_six_hours(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    limited = FakeBank().on("GET", "/accounts/u-cc/transactions", (429, {"error": "ASPSP_RATE_LIMIT_EXCEEDED"}))
    _run(conn, _client(limited))
    assert conn.execute("SELECT retry_after FROM bank_links").fetchone()[0] == "2025-03-10T14:00:00+00:00"
    later = _bank_for("u-cc", [])
    _run(conn, _client(later), now=NOW + timedelta(hours=1))
    assert later.requests == []                        # en pause
    _run(conn, _client(later), now=NOW + timedelta(hours=6))
    assert len(later.calls("GET", "/accounts/u-cc/transactions")) == 1
    assert conn.execute("SELECT retry_after, last_error FROM bank_links").fetchone() == (None, None)


def test_failure_on_one_account_keeps_the_others(conn):
    _connected(conn, _account("u-cc", IBAN_CC), _account("u-liv", IBAN_LIVRET, "SVGS"))
    bank = FakeBank().on("GET", "/accounts/u-cc/transactions", (500, {"message": "panne"}))
    bank.on("GET", "/accounts/u-liv/transactions", {"transactions": [booked("2025-03-02", "20", "VIR", "CRDT")]})
    bank.on("GET", "/accounts/u-liv/balances", {"balances": []})
    summary = _run(conn, _client(bank))
    assert _ops(conn) == [(2, "2025-03-02", 2000, "VIR")]
    assert summary.errors == 1
    assert conn.execute("SELECT last_error IS NOT NULL FROM external_accounts ORDER BY id").fetchall() == [(1,), (0,)]
    assert bank_status(conn, NOW)["status"] == "error"


def test_ignored_and_unattached_accounts_are_not_synced(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    attach(conn, 1, None, ignore=True)
    conn.commit()
    bank = _bank_for("u-cc", [booked("2025-03-02", "1", "X")])
    _run(conn, _client(bank))
    assert bank.requests == [] and _ops(conn) == []


def test_internal_transfer_between_synced_accounts_is_paired(conn):
    from app.classify.seed import load_seed
    from importlib import resources
    with resources.as_file(resources.files("app.classify").joinpath("rules.seed.toml")) as seed:
        load_seed(conn, seed, with_rules=False)
    conn.commit()
    _connected(conn, _account("u-cc", IBAN_CC), _account("u-liv", IBAN_LIVRET, "SVGS"))
    bank = FakeBank().on("GET", "/accounts/u-cc/transactions",
                         {"transactions": [booked("2025-03-04", "200.00", "VIR LIVRET FICTIF")]})
    bank.on("GET", "/accounts/u-liv/transactions",
            {"transactions": [booked("2025-03-04", "200.00", "VIR DEPUIS COMPTE", "CRDT")]})
    bank.on("GET", "/accounts/u-cc/balances", {"balances": []}).on("GET", "/accounts/u-liv/balances", {"balances": []})
    _run(conn, _client(bank), on_new_transactions)
    source, mirror = conn.execute("SELECT id, mirror_of, classified_by FROM transactions ORDER BY account_id").fetchall()
    assert mirror[1] == source[0] and source[2] == "pair"


# --- Statut -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("valid_until, status, days", [
    ("2025-09-06T08:00:00+00:00", "ok", 180),
    ("2025-03-24T08:00:00+00:00", "expiring", 14),
    ("2025-03-09T08:00:00+00:00", "expired", 0),
])
def test_bank_status_expiry(conn, valid_until, status, days):
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC), valid_until=valid_until), NOW)
    found = bank_status(conn, NOW)
    assert (found["status"], found["days_left"]) == (status, days)


def test_bank_status_without_link(conn):
    assert bank_status(conn, NOW) is None
    assert date(2025, 3, 10)        # garde l'import utilisé


# --- Configuration ----------------------------------------------------------------------------------------------

def test_bank_settings_from_environment(tmp_path, monkeypatch):
    from app.config import ConfigError, _bank_settings, _sync_hours
    for name in ("EXTRANALYZER_EB_APP_ID", "EXTRANALYZER_EB_REDIRECT_URL", "EXTRANALYZER_EB_KEY_FILE",
                 "EXTRANALYZER_EB_ASPSP", "EXTRANALYZER_SYNC_HOURS"):
        monkeypatch.delenv(name, raising=False)
    assert _bank_settings() is None and _sync_hours() == 12
    key = tmp_path / "eb.pem"
    key.write_bytes(rsa_key_pem())
    monkeypatch.setenv("EXTRANALYZER_EB_APP_ID", "app-fictive")
    monkeypatch.setenv("EXTRANALYZER_EB_KEY_FILE", str(key))
    assert _bank_settings() is None                     # URL de retour absente : désactivé
    monkeypatch.setenv("EXTRANALYZER_EB_REDIRECT_URL", "http://10.0.0.1:8000/bank/callback")
    found = _bank_settings()
    assert (found.app_id, found.key_pem, found.aspsp_name) == ("app-fictive", rsa_key_pem(), "Crédit Mutuel")
    assert "BEGIN" not in repr(found)                   # la clé n'apparaît jamais dans une représentation
    monkeypatch.setenv("EXTRANALYZER_EB_KEY_FILE", str(tmp_path / "absente.pem"))
    assert _bank_settings() is None
    monkeypatch.setenv("EXTRANALYZER_SYNC_HOURS", "-1")
    with pytest.raises(ConfigError):
        _sync_hours()


def test_failed_auth_request_drops_state(conn):
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [{"name": "Crédit Mutuel"}]})
    bank.on("POST", "/auth", (500, {"message": "panne"}))
    with pytest.raises(EnableBankingError):
        start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW)
    assert conn.execute("SELECT COUNT(*) FROM bank_auth_states").fetchone()[0] == 0


def test_renewal_restarts_with_longest_history(conn):
    _connected(conn, _account("u-cc", IBAN_CC, id_hash="H1"))
    _run(conn, _client(_bank_for("u-cc", [booked("2025-03-03", "1.00", "CB")])))
    complete_connection(conn, _client(FakeBank()),
                        _session(_account("u-cc-2", IBAN_CC, id_hash="H1"), session_id="sess-2"), NOW)
    conn.commit()
    bank = _bank_for("u-cc-2", [])
    _run(conn, _client(bank))
    assert bank.calls("GET", "/accounts/u-cc-2/transactions")[0]["query"]["strategy"] == "longest"


def test_old_window_uses_longest(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    _run(conn, _client(_bank_for("u-cc", [booked("2025-03-03", "1.00", "CB")])))
    bank = _bank_for("u-cc", [])
    _run(conn, _client(bank), now=NOW + timedelta(days=120))
    assert bank.calls("GET", "/accounts/u-cc/transactions")[0]["query"]["strategy"] == "longest"


def test_manual_sync_during_pause_says_so(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    _run(conn, _client(FakeBank().on("GET", "/accounts/u-cc/transactions", (429, {"error": "ASPSP_RATE_LIMIT"}))))
    summary = _run(conn, _client(_bank_for("u-cc", [])), now=NOW + timedelta(hours=1))
    assert summary.added == 0 and "limite de consultations" in summary.message


def test_expiry_of_replaced_link_is_ignored(conn):
    from app.sources.enablebanking.client import SessionExpired
    from app.sources.enablebanking.link import Fetched, write_sync
    _connected(conn, _account("u-cc", IBAN_CC))
    complete_connection(conn, _client(FakeBank()), _session(_account("u-2", IBAN_CC), session_id="sess-2"), NOW)
    write_sync(conn, [Fetched(1, 1, None, SessionExpired(401, "EXPIRED_SESSION", "x"))], "planifiée", Recorder(),
               NOW, NOW)
    assert conn.execute("SELECT session_id, status FROM bank_links ORDER BY id").fetchall() == \
        [("sess-1", "replaced"), ("sess-2", "active")]


@pytest.mark.parametrize("maximum, expected", [
    (7776000, "2025-06-08T07:00:00+00:00"),       # banque limitée à 90 jours
    (15552000, "2025-09-06T07:00:00+00:00"),      # 180 jours
    (31536000, "2025-09-06T07:00:00+00:00"),      # au-delà de 180 jours : 180 jours souhaités suffisent
    ("7776000", "2025-06-08T07:00:00+00:00"),     # nombre transmis en texte
    (None, "2025-06-08T07:00:00+00:00"),          # maximum absent : 90 jours
])
def test_consent_validity_respects_bank_maximum(conn, maximum, expected):
    aspsp = {"name": "Crédit Mutuel", "country": "FR"}
    if maximum is not None:
        aspsp["maximum_consent_validity"] = maximum
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [aspsp]})
    bank.on("POST", "/auth", {"url": "https://banque.example/auth", "authorization_id": "a"})
    start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW)
    valid_until = bank.calls("POST", "/auth")[0]["body"]["access"]["valid_until"]
    assert valid_until == expected
    limit = datetime.fromisoformat(valid_until) - NOW
    assert limit.total_seconds() < int(maximum or 7776000)        # toujours strictement sous le maximum


def test_alert_follows_validity_granted_by_bank(conn):
    # la banque accorde moins que demandé : l'alerte suit la date réellement accordée (renvoyée par /sessions)
    complete_connection(conn, _client(FakeBank()),
                        _session(_account("u-cc", IBAN_CC), valid_until="2025-03-20T08:00:00+00:00"), NOW)
    assert bank_status(conn, NOW)["status"] == "expiring"
    assert bank_status(conn, NOW)["days_left"] == 10


def test_retries_once_under_limit_stated_by_bank(conn):
    # /aspsps annonce 180 jours mais la banque refuse au-delà de 90 : une nouvelle demande sous cette limite
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [{"name": "Crédit Mutuel", "maximum_consent_validity": 15552000}]})
    bank.on("POST", "/auth",
            (422, {"error": "WRONG_REQUEST_PARAMETERS",
                   "message": "ASPSP does not support consent validity more than 7776000 seconds in the future"}),
            {"url": "https://banque.example/auth", "authorization_id": "a"})
    assert start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW) == "https://banque.example/auth"
    sent = [c["body"]["access"]["valid_until"] for c in bank.calls("POST", "/auth")]
    assert sent == ["2025-09-06T07:00:00+00:00", "2025-06-08T07:00:00+00:00"]
    assert conn.execute("SELECT COUNT(*) FROM bank_auth_states").fetchone()[0] == 1


def test_refusal_without_shorter_limit_is_reported(conn):
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [{"name": "Crédit Mutuel", "maximum_consent_validity": 7776000}]})
    bank.on("POST", "/auth", (422, {"error": "WRONG_REQUEST_PARAMETERS",
                                    "message": "consent validity more than 7776000 seconds in the future"}))
    with pytest.raises(EnableBankingError):
        start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW)
    assert len(bank.calls("POST", "/auth")) == 1      # même limite : pas de nouvelle tentative inutile
    assert conn.execute("SELECT COUNT(*) FROM bank_auth_states").fetchone()[0] == 0


# --- Plusieurs banques ----------------------------------------------------------------------------------------

IBAN_OTHER = "FR7699999000000000000000777"


def _other_session(*accounts, session_id="sess-b", valid_until="2025-06-08T08:00:00+00:00"):
    return {**_session(*accounts, session_id=session_id, valid_until=valid_until),
            "aspsp": {"name": "Banque Fictive", "country": "FR"}}


def _links(conn):
    return conn.execute("SELECT session_id, aspsp_name, status FROM bank_links ORDER BY id").fetchall()


def test_list_aspsps_unique_sorted_and_country_checked():
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [
        {"name": "Crédit Mutuel"}, {"name": "Banque Fictive"}, {"name": "banque fictive"}, {"name": ""}, "x"]})
    assert list_aspsps(_client(bank), "fr") == [{"name": "Banque Fictive", "country": "FR"},
                                                {"name": "Crédit Mutuel", "country": "FR"}]
    assert bank.calls("GET", "/aspsps")[0]["query"]["country"] == "FR"
    with pytest.raises(ValueError):
        list_aspsps(_client(bank), "FRA")


def test_start_connection_with_chosen_bank(conn):
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [{"name": "Banque Fictive", "country": "BE"}]})
    bank.on("POST", "/auth", {"url": "https://banque.example/auth"})
    start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW, "banque fictive", "be")
    assert bank.calls("POST", "/auth")[0]["body"]["aspsp"] == {"name": "Banque Fictive", "country": "BE"}
    with pytest.raises(ValueError):
        start_connection(lambda fn: fn(conn), _client(bank), BANK, NOW, "Banque Fictive", "Belgique")


def test_second_bank_does_not_replace_first(conn):
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC)), NOW)
    done = complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER)), NOW)
    assert done.replaced_sessions == []
    assert _links(conn) == [("sess-1", "Crédit Mutuel", "active"), ("sess-b", "Banque Fictive", "active")]
    # compte « API seulement » créé pour la nouvelle banque
    assert conn.execute("SELECT number, kind, source FROM accounts WHERE number = ?", (IBAN_OTHER,)).fetchone() == \
        (IBAN_OTHER, "checking", "enablebanking")


def test_same_bank_without_accounts_is_replaced(conn):
    complete_connection(conn, _client(FakeBank()), _other_session(session_id="sess-b"), NOW)
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC)), NOW)
    done = complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER),
                                                                         session_id="sess-c"), NOW)
    assert done.replaced_sessions == ["sess-b"]
    assert [status for _, _, status in _links(conn)] == ["replaced", "active", "active"]


def test_renewing_expired_link_replaces_it(conn):
    complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER, id_hash="HO")), NOW)
    conn.execute("UPDATE bank_links SET status = 'expired'")
    done = complete_connection(conn, _client(FakeBank()),
                               _other_session(_account("u-o2", IBAN_OTHER, id_hash="HO"), session_id="sess-c"), NOW)
    assert done.replaced_sessions == ["sess-b"]
    assert bank_status(conn, NOW)["status"] == "ok"


@pytest.mark.parametrize("kind, name, expected", [
    ("SVGS", "Compte", "savings"), ("CACC", "Livret A", "savings"), ("CACC", "LDDS fictif", "savings"),
    ("CACC", "Compte Épargne", "savings"), ("CACC", "Compte chèques", "checking"), (None, None, "checking"),
    ("CARD", "Carte", "checking")])
def test_account_kind(kind, name, expected):
    assert account_kind(kind, name) == expected


def test_set_kind_only_for_api_accounts(conn):
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC)), NOW)
    complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER)), NOW)
    set_kind(conn, 2, "savings")
    assert conn.execute("SELECT kind FROM accounts WHERE number = ?", (IBAN_OTHER,)).fetchone()[0] == "savings"
    with pytest.raises(ValueError):
        set_kind(conn, 1, "savings")          # compte des relevés PDF
    with pytest.raises(ValueError):
        set_kind(conn, 2, "exchange")
    with pytest.raises(LookupError):
        set_kind(conn, 99, "savings")


def test_bank_status_takes_all_links_into_account(conn):
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC)), NOW)        # 180 jours
    complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER),
                                                                  valid_until="2025-03-20T08:00:00+00:00"), NOW)
    found = bank_status(conn, NOW)
    assert (found["status"], found["aspsp_name"], found["days_left"], found["links"]) == \
        ("expiring", "Banque Fictive", 10, 2)
    conn.execute("UPDATE bank_links SET status = 'expired' WHERE session_id = 'sess-b'")
    assert bank_status(conn, NOW)["status"] == "expired"
    revoke(conn, 2)
    assert bank_status(conn, NOW)["status"] == "ok" and bank_status(conn, NOW)["links"] == 1


def test_revoke_one_link(conn):
    complete_connection(conn, _client(FakeBank()), _session(_account("u-cc", IBAN_CC)), NOW)
    complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER)), NOW)
    assert revoke(conn, 2) == ["sess-b"]
    assert _links(conn) == [("sess-1", "Crédit Mutuel", "active"), ("sess-b", "Banque Fictive", "revoked")]
    with pytest.raises(LookupError):
        revoke(conn, 2)


def test_errors_of_one_bank_do_not_block_the_other(conn, monkeypatch):
    _connected(conn, _account("u-cc", IBAN_CC))
    complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER)), NOW)
    conn.commit()
    bank = FakeBank().on("GET", "/accounts/u-cc/transactions", (401, {"error": "EXPIRED_SESSION"}))
    bank.on("GET", "/accounts/u-o/transactions", {"transactions": [booked("2025-03-03", "5.00", "CB AUTRE BANQUE")]})
    bank.on("GET", "/accounts/u-o/balances", {"balances": []})
    summary = _run(conn, _client(bank))
    assert (summary.added, summary.errors) == (1, 1)
    assert [status for _, _, status in _links(conn)] == ["expired", "active"]
    # une exception imprévue reste limitée à son compte : la synchro aboutit et l'enregistre
    from app.sources.enablebanking import link as link_module
    monkeypatch.setattr(link_module, "pick_balance", lambda balances: 1 / 0)
    summary = _run(conn, _client(bank), now=NOW + timedelta(hours=1))
    assert summary.errors == 1 and "ZeroDivisionError" in summary.message


def test_sync_one_link_only(conn):
    _connected(conn, _account("u-cc", IBAN_CC))
    complete_connection(conn, _client(FakeBank()), _other_session(_account("u-o", IBAN_OTHER)), NOW)
    conn.commit()
    bank = _bank_for("u-o", [booked("2025-03-03", "5.00", "CB")])
    summary = sync(lambda fn: fn(conn), lambda fn: fn(conn), _client(bank), "manuelle", Recorder(), lambda: NOW,
                   link_id=2)
    assert summary.added == 1 and not bank.calls("GET", "/accounts/u-cc/transactions")
