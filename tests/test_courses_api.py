"""API de l'onglet Courses (tickets Super U fictifs générés, débits fictifs insérés en SQL).

Aucun test ne démarre la relève ni ne touche le réseau : `Settings.mail` vaut None, ou l'application est
créée avec `start_mail_thread=False` et la relève est remplacée par un faux passage.
"""
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

import app.main as main_module
import app.receipts.mailsync as mailsync
from app.auth import hash_password
from app.config import MailSettings, Settings
from app.main import create_app
from app.store.db import connect
from tests.receiptgen import Item, make_superu_receipt

PASSWORD = "mot-de-passe-test"
H = {"X-Extranalyzer": "1"}
JAN = {"start": "2025-01-01", "end": "2025-01-31"}
DEC = {"cstart": "2024-12-01", "cend": "2024-12-31"}
URL = "/api/tabs/courses"
FAKE_MAIL = MailSettings(user="utilisateur@example.invalid", app_password="secret-fictif",
                         labels=(("Libellé fictif", "superu"),))

# Ticket fictif : 30,00 € = CB 18,00 + titres-restaurant 12,00.
TICKET_A = dict(when=datetime(2025, 1, 8, 15, 10, 1), aisles=[
    ("CREMERIE L.S.", [Item("YAOURT FICTIF", 600, quantity=2), Item("FROMAGE FICTIF", 1000)]),
    ("FRUITS ET LEGUMES", [Item("POMMES FICTIVES", 1400, quantity=1.5, unit_cents=933, weighed=True)]),
], payments=[("CB", 1800), ("TITRE RESTAURANT", 1200)])


class Env:
    def __init__(self, tmp_path: Path, mail: MailSettings | None = None):
        self.tmp = tmp_path
        self.settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD),
                                 max_upload_mb=1, mail=mail)
        self.app = create_app(self.settings, start_mail_thread=False)
        self.client = TestClient(self.app)
        assert self.client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
        self.conn = connect(self.settings.data_dir / "extranalyzer.db")
        self.cat = {}
        for cid, name, parent in self.conn.execute(
                "SELECT c.id, c.name, p.name FROM categories c LEFT JOIN categories p ON p.id = c.parent_id"):
            self.cat[f"{parent}/{name}" if parent else name] = cid
        self.conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'Courant fictif', "
                          "'checking')")
        self.conn.commit()
        self.n = 0

    def tx(self, day: str, cents: int, category: str | None = "Courses", label: str = "CB U MAGASIN TEST") -> str:
        self.n += 1
        key = f"k{self.n}"
        self.conn.execute(
            "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, category_id,"
            " manual, classified_by) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, day, label, label.lower(), cents, self.cat[category] if category else None,
             int(category is not None), "manual" if category else None))
        self.conn.commit()
        return key

    def upload(self, name: str, **ticket) -> dict:
        pdf = make_superu_receipt(self.tmp / name, **ticket)
        r = self.client.post(f"{URL}/upload", files=[("files", (name, pdf.read_bytes(), "application/pdf"))],
                             headers=H)
        assert r.status_code == 200, r.text
        [result] = r.json()
        return result

    def get(self, path: str, **params) -> dict:
        r = self.client.get(f"{URL}{path}", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, path: str, body=None):
        return self.client.post(f"{URL}{path}", json=body, headers=H)


@pytest.fixture
def env_courses(tmp_path):
    env = Env(tmp_path)
    env.k_ticket = env.tx("2025-01-09", -1800)                       # débit du ticket A (part CB)
    env.k_alone = env.tx("2025-01-20", -2460)                        # débit Courses sans ticket
    env.k_dec = env.tx("2024-12-10", -1000)                          # débit Courses de décembre sans ticket
    env.a = env.upload("ticket_a.pdf", **TICKET_A)
    assert (env.a["outcome"], env.a["status"]) == ("stored", "ok")
    yield env
    env.conn.close()


def family_id(env: Env, name: str) -> int:
    return env.conn.execute("SELECT id FROM aisle_families WHERE name = ?", (name,)).fetchone()[0]


def test_tabs_order_includes_courses(env_courses):
    tabs = [t["id"] for t in env_courses.client.get("/api/tabs").json()]
    assert tabs == ["overview", "categories", "courses", "transactions", "epargne", "a_classer", "releves",
                    "banque", "regles"]


def test_upload_links_and_summary_counts_whole_consumption(env_courses):
    env = env_courses
    receipts = env.get("/receipts", **JAN)
    assert [(r["id"], r["link"], r["total"], r["card"]) for r in receipts] == [
        (env.a["receipt_id"], "auto", "30.00", "18.00")]
    s = env.get("/summary", **JAN)
    assert s["undetailed"] == "24.60" and s["total"] == "54.60"
    assert s["by_payment"] == {"card": "42.60", "meal_voucher": "12.00", "voucher": "0.00", "cash": "0.00",
                               "other": "0.00"}
    assert [(f["name"], f["amount"], f["compare"]) for f in s["families"]] == [
        ("Crèmerie", "16.00", None), ("Fruits & légumes", "14.00", None)]
    assert s["undetailed_compare"] is None
    assert s["counts"] == {"to_link": 0, "broken": 0, "unsorted_aisles": 0, "errors": 0, "pending_parser": 0}
    assert s["out_of_courses"] == []


def test_summary_ignores_account_selector_for_tickets(env_courses):
    s = env_courses.get("/summary", **JAN, accounts="999")
    assert s["undetailed"] == "0.00" and s["total"] == "30.00"       # tickets non liés à un compte
    assert s["by_payment"]["card"] == "18.00"


def test_family_drilldown_products(env_courses):
    env = env_courses
    env.upload("ticket_b.pdf", when=datetime(2025, 1, 15, 10, 0, 0),
               aisles=[("CREMERIE L.S.", [Item("YAOURT FICTIF", 300)])], payments=[("TITRE RESTAURANT", 300)])
    d = env.get(f"/family/{family_id(env, 'Crèmerie')}", **JAN)
    [aisle] = d["aisles"]
    assert (aisle["name"], aisle["store"], aisle["amount"], aisle["compare"]) == (
        "CREMERIE L.S.", "superu", "19.00", None)
    assert aisle["products"] == [
        {"label": "FROMAGE FICTIF", "quantity": 1, "amount": "10.00", "avg_unit": "10.00", "purchases": 1},
        {"label": "YAOURT FICTIF", "quantity": 3, "amount": "9.00", "avg_unit": "3.00", "purchases": 2},
    ]
    fruits = env.get(f"/family/{family_id(env, 'Fruits & légumes')}", **JAN)["aisles"][0]["products"]
    assert fruits == [{"label": "POMMES FICTIVES", "quantity": 1.5, "amount": "14.00", "avg_unit": "9.33",
                       "purchases": 1}]
    assert env.client.get(f"{URL}/family/9999", params=JAN).status_code == 404


def test_unsorted_aisle_counts_and_can_be_assigned(env_courses):
    env = env_courses
    env.upload("ticket_b.pdf", when=datetime(2025, 1, 15, 10, 0, 0),
               aisles=[("RAYON TEST INCONNU", [Item("OBJET FICTIF", 500)])], payments=[("TITRE RESTAURANT", 500)])
    s = env.get("/summary", **JAN)
    assert s["counts"]["unsorted_aisles"] == 1
    assert {"id": 0, "name": "À ranger", "amount": "5.00", "compare": None} in s["families"]
    [unsorted] = env.get("/family/0", **JAN)["aisles"]
    assert unsorted["name"] == "RAYON TEST INCONNU"

    listing = env.get("/aisles")
    assert [f["name"] for f in listing["families"]][:2] == ["Crèmerie", "Fruits & légumes"]
    [row] = [a for a in listing["aisles"] if a["name"] == "RAYON TEST INCONNU"]
    assert (row["family_id"], row["lines"], row["store"]) == (None, 1, "superu")

    created = env.post("/families", {"name": "Bazar fictif"})
    assert created.status_code == 200 and created.json()["name"] == "Bazar fictif"
    assert env.post("/families", {"name": "Bazar fictif"}).status_code == 409
    fid = created.json()["id"]
    r = env.post(f"/aisles/{row['id']}", {"family_id": fid})
    assert r.status_code == 200 and r.json()["family_id"] == fid
    s = env.get("/summary", **JAN)
    assert s["counts"]["unsorted_aisles"] == 0
    assert {"id": fid, "name": "Bazar fictif", "amount": "5.00", "compare": None} in s["families"]

    assert env.post(f"/families/{fid}", {"name": "Divers fictif"}).json()["name"] == "Divers fictif"
    assert env.post(f"/families/{fid}", {"name": "Crèmerie"}).status_code == 409
    assert env.post("/families/9999", {"name": "X"}).status_code == 404
    assert env.post(f"/aisles/{row['id']}", {"family_id": 9999}).status_code == 404
    assert env.post("/aisles/9999", {"family_id": None}).status_code == 404


def test_receipt_detail(env_courses):
    env = env_courses
    d = env.get(f"/receipts/{env.a['receipt_id']}")
    assert (d["store"], d["shop"], d["purchased_at"], d["status"], d["link"]) == (
        "superu", "MAGASIN TEST", "2025-01-08T15:10:01", "ok", "auto")
    assert [(g["aisle"], g["family"], [line["label"] for line in g["lines"]]) for g in d["lines"]] == [
        ("CREMERIE L.S.", "Crèmerie", ["YAOURT FICTIF", "FROMAGE FICTIF"]),
        ("FRUITS ET LEGUMES", "Fruits & légumes", ["POMMES FICTIVES"])]
    assert d["payments"] == [{"method": "card", "amount": "18.00"}, {"method": "meal_voucher", "amount": "12.00"}]
    assert d["tx"] == {"tx_key": env.k_ticket, "op_date": "2025-01-09", "label": "CB U MAGASIN TEST",
                       "amount": "-18.00"}
    assert d["candidates"][0]["tx_key"] == env.k_ticket
    assert env.client.get(f"{URL}/receipts/9999").status_code == 404


def test_manual_link_and_conflict(env_courses):
    env = env_courses
    c = env.upload("ticket_c.pdf", when=datetime(2025, 1, 19, 9, 0, 0),
                   aisles=[("CREMERIE L.S.", [Item("BEURRE FICTIF", 1000)])])
    rid = c["receipt_id"]
    assert env.get(f"/receipts/{rid}")["link"] == "to_link"
    assert env.get("/summary", **JAN)["counts"]["to_link"] == 1
    r = env.post(f"/receipts/{rid}/link", {"tx_key": env.k_alone})
    assert r.status_code == 200
    assert (r.json()["link"], r.json()["tx"]["tx_key"]) == ("manual", env.k_alone)
    assert env.get("/summary", **JAN)["undetailed"] == "0.00"
    conflict = env.post(f"/receipts/{rid}/link", {"tx_key": env.k_ticket})       # débit du ticket A
    assert conflict.status_code == 409
    assert env.post(f"/receipts/{rid}/link", {"tx_key": "absent"}).status_code == 404
    assert env.post("/receipts/9999/link", {"tx_key": None}).status_code == 404
    unlinked = env.post(f"/receipts/{rid}/link", {"tx_key": None}).json()
    assert (unlinked["link"], unlinked["tx"]) == ("to_link", None)


def test_receipts_list_includes_to_link_of_other_periods(env_courses):
    env = env_courses
    dec = env.upload("ticket_dec.pdf", when=datetime(2024, 11, 5, 9, 0, 0),
                     aisles=[("CREMERIE L.S.", [Item("BEURRE FICTIF", 777)])])
    meal = env.upload("ticket_tr.pdf", when=datetime(2024, 11, 6, 9, 0, 0),
                      aisles=[("CREMERIE L.S.", [Item("BEURRE FICTIF", 500)])], payments=[("TITRE RESTAURANT", 500)])
    ids = {r["id"]: r["link"] for r in env.get("/receipts", **JAN)}
    assert ids == {env.a["receipt_id"]: "auto", dec["receipt_id"]: "to_link"}      # sans débit carte : hors liste
    assert env.get(f"/receipts/{meal['receipt_id']}")["link"] == "no_card"


def test_pdf_route_serves_stored_file(env_courses):
    env = env_courses
    r = env.client.get(f"{URL}/receipts/{env.a['receipt_id']}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content == (env.tmp / "ticket_a.pdf").read_bytes()
    assert env.client.get(f"{URL}/receipts/9999/pdf").status_code == 404


def test_upload_rejects_non_pdf_and_unknown_store(env_courses):
    env = env_courses
    other = env.tmp / "autre.pdf"
    c = canvas.Canvas(str(other))
    c.drawString(100, 700, "DOCUMENT FICTIF SANS ENSEIGNE")
    c.save()
    r = env.client.post(f"{URL}/upload", headers=H, files=[
        ("files", ("note.txt", b"bonjour", "text/plain")),
        ("files", ("autre.pdf", other.read_bytes(), "application/pdf")),
        ("files", ("ticket_a.pdf", (env.tmp / "ticket_a.pdf").read_bytes(), "application/pdf")),
    ])
    assert r.status_code == 200
    assert [(f["name"], f["outcome"], f["message"]) for f in r.json()] == [
        ("note.txt", "rejected", "le fichier n'est pas un PDF"),
        ("autre.pdf", "rejected", "enseigne pas encore prise en charge"),
        ("ticket_a.pdf", "duplicate", None)]
    big = env.client.post(f"{URL}/upload", headers=H,
                          files=[("files", ("gros.pdf", b"%PDF-" + b"0" * (1024 * 1024 + 70_000)))])
    assert big.status_code == 413


def test_sync_disabled_returns_409(env_courses):
    env = env_courses
    assert env.get("/sync") == {"enabled": False, "labels": []}
    assert env.post("/sync").status_code == 409
    assert env.app.state.mail.thread is None and env.app.state.mail.enabled is False


def test_sync_enabled_runs_a_pass_then_matches(tmp_path, monkeypatch):
    env = Env(tmp_path, mail=FAKE_MAIL)
    try:
        assert env.app.state.mail.enabled is True and env.app.state.mail.thread is None
        rid = env.upload("ticket_c.pdf", when=datetime(2025, 1, 19, 9, 0, 0),
                         aisles=[("CREMERIE L.S.", [Item("BEURRE FICTIF", 1000)])])["receipt_id"]
        env.tx("2025-01-20", -1000)              # le débit arrive après le ticket
        calls = []

        def fake_sync_once(cfg, write, receipts_dir, parsers, connect=None):
            calls.append(cfg)
            return {"messages": 0, "files": 0, "error": None}

        monkeypatch.setattr(mailsync, "sync_once", fake_sync_once)
        r = env.post("/sync")
        assert r.status_code == 200 and r.json() == {"messages": 0, "files": 0, "error": None}
        assert calls == [FAKE_MAIL]
        assert env.get(f"/receipts/{rid}")["link"] == "auto"
        env.conn.execute("INSERT INTO mail_sync(label, store, last_error) VALUES ('Libellé fictif', 'superu', "
                         "'erreur fictive')")
        env.conn.commit()
        assert env.get("/sync") == {"enabled": True, "labels": [
            {"label": "Libellé fictif", "store": "superu", "last_success_at": None, "last_attempt_at": None,
             "last_error": "erreur fictive"}]}
    finally:
        env.conn.close()


def test_mail_thread_started_only_when_enabled_and_requested(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(main_module.MailSyncThread, "start", lambda self: started.append(self))
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), mail=FAKE_MAIL)
    app = create_app(settings)
    assert started == [app.state.mail.thread] and app.state.mail.enabled is True
    create_app(Settings(data_dir=tmp_path / "data2", password_hash=hash_password(PASSWORD)))
    assert len(started) == 1


def test_reread_rereads_every_receipt(env_courses):
    env = env_courses
    env.conn.execute("DELETE FROM receipt_lines")
    env.conn.commit()
    r = env.post("/reread")
    assert r.status_code == 200 and r.json() == {"ok": 1}
    assert env.get("/summary", **JAN)["total"] == "54.60"


def test_transactions_rows_expose_receipt_id(env_courses):
    env = env_courses
    r = env.client.get("/api/tabs/transactions/list", params=JAN)
    rows = {row["label"] + row["op_date"]: row["receipt_id"] for row in r.json()["items"]}
    assert rows == {"CB U MAGASIN TEST2025-01-09": env.a["receipt_id"], "CB U MAGASIN TEST2025-01-20": None}


def test_out_of_courses_flag(env_courses):
    env = env_courses
    key = env.tx("2025-01-22", -700, category="Transport", label="CB U STATION TEST")
    d = env.upload("ticket_d.pdf", when=datetime(2025, 1, 22, 8, 0, 0),
                   aisles=[("CREMERIE L.S.", [Item("BEURRE FICTIF", 700)])])
    assert env.get(f"/receipts/{d['receipt_id']}")["tx"]["tx_key"] == key
    s = env.get("/summary", **JAN)
    assert s["out_of_courses"] == [{"receipt_id": d["receipt_id"], "tx_label": "CB U STATION TEST",
                                    "category_name": "Transport"}]
    assert s["total"] == "61.60" and s["undetailed"] == "24.60"


def test_comparison_period(env_courses):
    env = env_courses
    env.upload("ticket_dec.pdf", when=datetime(2024, 12, 5, 9, 0, 0),
               aisles=[("CREMERIE L.S.", [Item("BEURRE FICTIF", 300)])])
    s = env.get("/summary", **JAN, **DEC)
    assert {f["name"]: f["compare"] for f in s["families"]} == {"Crèmerie": "3.00", "Fruits & légumes": "0.00"}
    assert s["undetailed_compare"] == "10.00"
    assert s["total_compare"] == "13.00"
    [aisle] = env.get(f"/family/{family_id(env, 'Crèmerie')}", **JAN, **DEC)["aisles"]
    assert (aisle["amount"], aisle["compare"]) == ("16.00", "3.00")


def test_startup_rereads_pending_matches_and_keeps_user_families(env_courses):
    env = env_courses
    rid = env.a["receipt_id"]
    # ticket revenu « en attente de lecteur », sans lien ; une famille renommée, une autre supprimée
    env.conn.execute("UPDATE receipts SET status = 'pending_parser', tx_key = NULL, link_mode = NULL WHERE id = ?",
                     (rid,))
    env.conn.execute("DELETE FROM receipt_lines")
    env.conn.execute("UPDATE aisle_families SET name = 'Laitages' WHERE name = 'Crèmerie'")
    env.conn.execute("DELETE FROM aisle_families WHERE name = 'Animaux'")
    env.conn.commit()
    create_app(env.settings, start_mail_thread=False)                 # redémarrage
    assert env.get(f"/receipts/{rid}")["link"] == "auto"
    names = [f["name"] for f in env.get("/aisles")["families"]]
    assert "Laitages" in names and "Crèmerie" not in names and "Animaux" not in names
