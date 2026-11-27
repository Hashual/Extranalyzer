"""API des onglets Vue d'ensemble, Catégories, Transactions, Épargne, À classer, Relevés et Règles
(données fictives, insérées en SQL)."""
from datetime import date
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.classify.pipeline import reclassify
from app.config import Settings
from app.main import create_app
from app.store.db import connect

PASSWORD = "mot-de-passe-test"
H = {"X-Extranalyzer": "1"}
JAN = {"start": "2025-01-01", "end": "2025-01-31"}
DEC = {"cstart": "2024-12-01", "cend": "2024-12-31"}


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1)
    client = TestClient(create_app(settings))
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    conn = connect(settings.data_dir / "extranalyzer.db")
    cat = {}
    for cid, name, parent in conn.execute(
            "SELECT c.id, c.name, p.name FROM categories c LEFT JOIN categories p ON p.id = c.parent_id"):
        cat[f"{parent}/{name}" if parent else name] = cid
    conn.executescript("""
        INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'Courant fictif', 'checking'),
                                                             (2, 'A2', 'Livret fictif', 'savings');
    """)
    n = 0

    def tx(acc, day, cents, category, label="LIBELLE FICTIF", mirror=None):
        # Une ligne classée l'est par une décision manuelle (sa jambe miroir suit : 'pair') ; sans cela,
        # la chaîne de classement, qui recalcule tout le reste, la remettrait à classer.
        nonlocal n
        n += 1
        by = None if category is None else "pair" if mirror else "manual"
        cur = conn.execute(
            "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, category_id,"
            " mirror_of, manual, classified_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (acc, f"k{n}", day, label, label.lower(), cents, cat[category] if category else None, mirror,
             int(by == "manual"), by))
        return cur.lastrowid

    ids = {
        "salary": tx(1, "2025-01-05", 200000, "Revenus/Salaire", "VIR SALAIRE ALPHA"),
        "courses": tx(1, "2025-01-08", -12000, "Courses", "CB MARCHE BETA"),
        "fuel": tx(1, "2025-01-10", -6000, "Transport/Carburant", "CB STATION GAMMA"),
        "train": tx(1, "2025-01-11", -4000, "Transport/Train", "CB TRAIN DELTA"),
        "transport": tx(1, "2025-01-12", -1000, "Transport", "CB TICKET EPSILON"),
        "unknown": tx(1, "2025-01-20", -2500, None, "CB INCONNU ZETA"),
        "unknown_credit": tx(1, "2025-01-21", 700, None, "VIR INCONNU ETA"),
    }
    ids["saving"] = tx(1, "2025-01-15", -30000, "Épargne/Livret Bleu", "VIR LIVRET BLEU")
    ids["mirror"] = tx(2, "2025-01-15", 30000, "Épargne/Livret Bleu", "VIR LIVRET BLEU", mirror=ids["saving"])
    ids["interest"] = tx(2, "2025-01-31", 450, "Épargne/Intérêts", "INTERETS")
    ids["withdraw"] = tx(1, "2025-01-25", 5000, "Épargne/Livret Bleu", "VIR DEPUIS LIVRET")
    tx(1, "2024-12-08", -8000, "Courses", "CB MARCHE BETA")
    tx(1, "2024-12-15", -10000, "Épargne/Livret Bleu", "VIR LIVRET BLEU")
    conn.commit()
    yield client, conn, cat, ids
    conn.close()


def get(client, path, **params):
    r = client.get(f"/api/tabs{path}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- Paramètres communs ------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/overview/summary", "/categories/tree", "/transactions/list",
                                  "/epargne/summary", "/epargne/dashboard", "/a_classer/list"])
def test_invalid_periods_are_rejected(env, path):
    client = env[0]
    url = f"/api/tabs{path}"
    assert client.get(url, params={"start": "2025-02-01", "end": "2025-01-01"}).status_code == 422
    assert client.get(url, params={"start": "2025-13-01", "end": "2025-12-31"}).status_code == 422
    assert client.get(url, params={"end": "2025-01-31"}).status_code == 422
    assert client.get(url, params={**JAN, "cstart": "2024-12-01"}).status_code == 422
    assert client.get(url, params={**JAN, "cstart": "2024-12-31", "cend": "2024-12-01"}).status_code == 422
    assert client.get(url, params={**JAN, "accounts": "1,x"}).status_code == 422
    assert client.get(url, params={**JAN, "accounts": ""}).status_code == 200


def test_tabs_are_registered_in_order(env):
    assert [t["id"] for t in env[0].get("/api/tabs").json()] == ["overview", "budget", "categories", "courses",
                                                                 "transactions", "epargne", "a_classer", "releves",
                                                                 "banque", "regles"]


# --- Vue d'ensemble ----------------------------------------------------------------------------

def test_overview_summary_totals(env):
    client = env[0]
    s = get(client, "/overview/summary", **JAN)
    # Dépenses : 120 + 60 + 40 + 10 + 25 (à classer) ; revenus : 2000 (le crédit à classer de 7 reste à part).
    # Épargne (compte courant seul) : 300 − 50 = 250 ; solde = 2000 − 255 − 250.
    assert s["totals"] == {"expenses": "255.00", "income": "2000.00", "savings_net": "250.00",
                           "balance": "1495.00", "unclassified_debits": "25.00", "unclassified_credits": "7.00"}
    assert s["compare_totals"] is None
    assert s["by_category"] == [
        {"id": env[2]["Courses"], "name": "Courses", "amount": "120.00"},
        {"id": env[2]["Transport"], "name": "Transport", "amount": "110.00"},
        {"id": None, "name": "À classer", "amount": "25.00"},
    ]
    assert [m["month"] for m in s["monthly"]] == ["2025-01"]
    assert s["monthly"][0]["expenses"] == "255.00"
    assert s["recurring"] == []


def test_overview_recurring_over_three_months(env):
    client, conn, cat, _ = env
    for i, (day, cents) in enumerate([("2024-11-03", -1999), ("2024-12-03", -1999), ("2025-01-03", -2049)]):
        conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
                     "VALUES (1, ?, ?, 'PRLV ABO THETA', 'prlv abo theta', ?)", (f"r{i}", day, cents))
    conn.commit()
    s = get(client, "/overview/summary", start="2024-11-01", end="2025-01-31")
    assert s["recurring"] == [{"label_norm": "prlv abo theta", "avg_amount": "20.16",
                               "months": ["2024-11", "2024-12", "2025-01"]}]


def test_overview_with_comparison_and_accounts(env):
    client = env[0]
    s = get(client, "/overview/summary", **JAN, **DEC)
    assert s["compare_totals"]["expenses"] == "80.00"
    assert s["compare_totals"]["savings_net"] == "100.00"
    assert s["top_increases"] == [{"name": "Transport", "increase": "110.00"},
                                  {"name": "Courses", "increase": "40.00"}]
    both = get(client, "/overview/summary", **JAN, accounts="1,2")
    assert both["totals"]["savings_net"] == "254.50"      # + intérêts du livret, miroir ignoré


# --- Catégories --------------------------------------------------------------------------------

def test_categories_tree(env):
    client, _, cat, _ = env
    tree = get(client, "/categories/tree", **JAN)
    roots = {r["name"]: r for r in tree["roots"]}
    assert roots["Transport"]["amount"] == "110.00"
    assert roots["Transport"]["kind"] == "expense"
    assert {c["name"]: c["amount"] for c in roots["Transport"]["children"]} == {
        "Carburant": "60.00", "Train": "40.00", "Covoiturage": "0.00", "Péage/Parking": "0.00"}
    assert roots["Revenus"]["amount"] == "2000.00"
    assert roots["Épargne"]["amount"] == "250.00"
    assert roots["Loisirs"]["amount"] == "0.00"
    assert tree["unclassified"] == {"debits": "25.00", "credits": "7.00", "count": 2}


def test_category_detail_returns_children(env):
    client, _, cat, ids = env
    d = get(client, f"/categories/{cat['Transport']}", **JAN)
    assert d["name"] == "Transport" and d["kind"] == "expense" and d["amount"] == "110.00"
    assert d["children"] == [
        {"id": cat["Transport/Carburant"], "name": "Carburant", "amount": "60.00"},
        {"id": cat["Transport/Train"], "name": "Train", "amount": "40.00"},
        {"id": cat["Transport"], "name": "Transport (sans sous-catégorie)", "amount": "10.00"},
    ]
    assert d["monthly"] == [{"month": "2025-01", "amount": "110.00"}]
    assert [t["id"] for t in d["transactions"]] == [ids["transport"], ids["train"], ids["fuel"]]
    leaf = get(client, f"/categories/{cat['Transport/Train']}", **JAN)
    assert leaf["parent_id"] == cat["Transport"] and leaf["children"] == []
    assert [t["id"] for t in leaf["transactions"]] == [ids["train"]]


def test_category_detail_unknown_is_404(env):
    assert env[0].get("/api/tabs/categories/9999", params=JAN).status_code == 404


def test_category_reclassify_in_place(env):
    client, conn, cat, ids = env
    r = client.post(f"/api/tabs/categories/transactions/{ids['unknown']}/category",
                    json={"category_id": cat["Loisirs"]}, headers=H)
    assert r.status_code == 200
    assert (r.json()["category_name"], r.json()["manual"]) == ("Loisirs", True)
    tx_key = conn.execute("SELECT tx_key FROM transactions WHERE id = ?", (ids["unknown"],)).fetchone()[0]
    assert conn.execute("SELECT category_id FROM manual_overrides WHERE tx_key = ?",
                        (tx_key,)).fetchone() == (cat["Loisirs"],)
    assert get(client, f"/categories/{cat['Loisirs']}", **JAN)["amount"] == "25.00"


def test_category_detail_lists_the_100_most_recent_transactions(env):
    client, conn, cat, ids = env
    conn.executemany(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, category_id) "
        "VALUES (1, ?, '2025-01-02', 'CB MARCHE BETA', 'cb marche beta', -100, ?)",
        [(f"c{i}", cat["Courses"]) for i in range(105)])
    conn.commit()
    d = get(client, f"/categories/{cat['Courses']}", **JAN)
    assert d["transactions_total"] == 106 and len(d["transactions"]) == 100
    assert d["transactions"][0]["id"] == ids["courses"]          # le plus récent d'abord
    assert d["amount"] == "225.00"                                # le total couvre tout, pas seulement la page



# --- Catégories : création, renommage, suppression --------------------------------------------

def _meta_names(client):
    meta = client.get("/api/meta").json()
    return {r["name"]: [c["name"] for c in r["children"]] for r in meta["categories"]}


def test_create_subcategory_inherits_kind_and_appears_in_meta(env):
    client, conn, cat, _ = env
    r = client.post("/api/tabs/categories", json={"name": "  Vélo  ", "parent_id": cat["Transport"]}, headers=H)
    assert r.status_code == 201, r.text
    assert r.json() == {"id": r.json()["id"], "name": "Vélo", "parent_id": cat["Transport"], "kind": "expense"}
    assert "Vélo" in _meta_names(client)["Transport"]
    # le type demandé est ignoré : une sous-catégorie hérite toujours de sa racine
    r = client.post("/api/tabs/categories", json={"name": "Prime", "parent_id": cat["Revenus"], "kind": "expense"},
                    headers=H)
    assert r.json()["kind"] == "income"


def test_create_root_requires_a_kind(env):
    client, _, _, _ = env
    r = client.post("/api/tabs/categories", json={"name": "Animaux"}, headers=H)
    assert r.status_code == 422 and "type" in r.json()["detail"]
    r = client.post("/api/tabs/categories", json={"name": "Animaux", "kind": "expense"}, headers=H)
    assert r.status_code == 201 and r.json()["parent_id"] is None
    assert _meta_names(client)["Animaux"] == []


def test_create_category_rejects_invalid_input(env):
    client, _, cat, _ = env

    def post(body):
        return client.post("/api/tabs/categories", json=body, headers=H)

    assert post({"name": "   ", "parent_id": cat["Transport"]}).status_code == 422
    assert post({"name": "x" * 61, "parent_id": cat["Transport"]}).status_code == 422
    assert post({"name": "A\x07B", "parent_id": cat["Transport"]}).status_code == 422
    r = post({"name": "Trop profond", "parent_id": cat["Transport/Train"]})
    assert r.status_code == 422 and "deux niveaux" in r.json()["detail"]
    assert post({"name": "Orpheline", "parent_id": 9999}).status_code == 404
    assert post({"name": "Racine", "kind": "autre"}).status_code == 422


def test_duplicate_category_is_a_readable_409(env):
    client, conn, cat, _ = env
    for body in ({"name": "Train", "parent_id": cat["Transport"]},
                 {"name": "TRAIN", "parent_id": cat["Transport"]},        # casse
                 {"name": "Sante", "kind": "expense"}):                   # accents
        r = client.post("/api/tabs/categories", json=body, headers=H)
        assert r.status_code == 409, body
        assert "existe déjà" in r.json()["detail"]
    # même nom sous une autre racine : autorisé
    assert client.post("/api/tabs/categories", json={"name": "Train", "parent_id": cat["Loisirs"]},
                       headers=H).status_code == 201
    r = client.patch(f"/api/tabs/categories/{cat['Transport/Carburant']}", json={"name": "train"}, headers=H)
    assert r.status_code == 409
    assert conn.execute("SELECT name FROM categories WHERE id = ?", (cat["Transport/Carburant"],)).fetchone() == (
        "Carburant",)


def test_rename_category(env):
    client, _, cat, _ = env
    r = client.patch(f"/api/tabs/categories/{cat['Transport/Train']}", json={"name": "Rail"}, headers=H)
    assert r.status_code == 200 and r.json()["name"] == "Rail"
    assert "Rail" in _meta_names(client)["Transport"] and "Train" not in _meta_names(client)["Transport"]
    assert client.patch("/api/tabs/categories/9999", json={"name": "X"}, headers=H).status_code == 404
    assert client.patch(f"/api/tabs/categories/{cat['Courses']}", json={"name": ""}, headers=H).status_code == 422
    # la casse seule peut changer
    r = client.patch(f"/api/tabs/categories/{cat['Courses']}", json={"name": "COURSES"}, headers=H)
    assert r.status_code == 200 and r.json()["name"] == "COURSES"


def test_detail_reports_usage(env):
    client, conn, cat, ids = env
    conn.execute("INSERT INTO rules(pattern, match, category_id, priority) VALUES ('TRAIN', 'contains', ?, 10)",
                 (cat["Transport/Train"],))
    conn.commit()
    d = get(client, f"/categories/{cat['Transport/Train']}", **JAN)
    assert d["subcategories"] == 0 and d["usage"] == {"transactions": 1, "rules": 1, "overrides": 0}
    assert get(client, f"/categories/{cat['Transport']}", **JAN)["subcategories"] == 4


def test_delete_category_with_subcategories_is_refused(env):
    client, conn, cat, _ = env
    r = client.delete(f"/api/tabs/categories/{cat['Transport']}", params={"move_to": cat["Courses"]}, headers=H)
    assert r.status_code == 409 and "4 sous-catégories" in r.json()["detail"]
    assert conn.execute("SELECT 1 FROM categories WHERE id = ?", (cat["Transport"],)).fetchone() is not None


def test_delete_used_category_requires_a_destination(env):
    client, conn, cat, ids = env
    r = client.post(f"/api/tabs/categories/transactions/{ids['unknown']}/category",
                    json={"category_id": cat["Transport/Train"]}, headers=H)
    assert r.status_code == 200
    conn.execute("INSERT INTO rules(pattern, match, category_id, priority) VALUES ('ZZZ', 'contains', ?, 10)",
                 (cat["Transport/Train"],))
    conn.commit()
    r = client.delete(f"/api/tabs/categories/{cat['Transport/Train']}", headers=H)
    assert r.status_code == 409
    assert r.json()["detail"] == ("« Train » est utilisée (2 opérations, 1 règle et 1 reclassement manuel) : "
                                  "choisissez une catégorie vers laquelle les déplacer")
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category_id = ?",
                        (cat["Transport/Train"],)).fetchone() == (2,)


def test_delete_moves_transactions_rules_and_overrides(env):
    client, conn, cat, ids = env
    client.post(f"/api/tabs/categories/transactions/{ids['train']}/category",
                json={"category_id": cat["Transport/Train"]}, headers=H)       # reclassement manuel mémorisé
    rule_id = conn.execute("INSERT INTO rules(pattern, match, category_id, priority) "
                           "VALUES ('ZZZ', 'contains', ?, 10)", (cat["Transport/Train"],)).lastrowid
    conn.commit()
    r = client.delete(f"/api/tabs/categories/{cat['Transport/Train']}", params={"move_to": cat["Transport"]},
                      headers=H)
    assert r.status_code == 200, r.text
    assert r.json() == {"moved": {"transactions": 1, "rules": 1, "overrides": 1}}
    assert conn.execute("SELECT 1 FROM categories WHERE id = ?", (cat["Transport/Train"],)).fetchone() is None
    assert conn.execute("SELECT category_id, manual, classified_by FROM transactions WHERE id = ?",
                        (ids["train"],)).fetchone() == (cat["Transport"], 1, "manual")
    assert conn.execute("SELECT category_id FROM rules WHERE id = ?", (rule_id,)).fetchone() == (cat["Transport"],)
    tx_key = conn.execute("SELECT tx_key FROM transactions WHERE id = ?", (ids["train"],)).fetchone()[0]
    assert conn.execute("SELECT category_id FROM manual_overrides WHERE tx_key = ?", (tx_key,)).fetchone() == (
        cat["Transport"],)
    assert "Train" not in _meta_names(client)["Transport"]
    roots = {r["name"]: r for r in get(client, "/categories/tree", **JAN)["roots"]}
    assert roots["Transport"]["amount"] == "110.00"                      # rien n'est perdu


def test_delete_destination_must_be_valid(env):
    client, _, cat, _ = env
    train = cat["Transport/Train"]
    r = client.delete(f"/api/tabs/categories/{train}", params={"move_to": cat["Revenus"]}, headers=H)
    assert r.status_code == 422 and "même type" in r.json()["detail"]
    assert client.delete(f"/api/tabs/categories/{train}", params={"move_to": train}, headers=H).status_code == 422
    assert client.delete(f"/api/tabs/categories/{train}", params={"move_to": 9999}, headers=H).status_code == 404
    assert client.delete("/api/tabs/categories/9999", headers=H).status_code == 404


def test_delete_unused_category_forgets_its_suggestions(env):
    client, conn, cat, ids = env
    conn.execute("UPDATE transactions SET suggested_category_id = ?, confidence = 0.5, suggestion_reason = 'r' "
                 "WHERE id = ?", (cat["Loisirs"], ids["unknown"]))
    conn.commit()
    r = client.delete(f"/api/tabs/categories/{cat['Loisirs']}", headers=H)
    assert r.status_code == 200 and r.json()["moved"] == {"transactions": 0, "rules": 0, "overrides": 0}
    assert "Loisirs" not in _meta_names(client)
    assert conn.execute("SELECT suggested_category_id FROM transactions WHERE id = ?",
                        (ids["unknown"],)).fetchone() != (cat["Loisirs"],)


def test_renamed_savings_root_still_hosts_paired_savings(env):
    from app.classify.savings_target import savings_category_for_account
    client, conn, cat, _ = env
    assert client.patch(f"/api/tabs/categories/{cat['Épargne']}", json={"name": "Mon épargne"},
                        headers=H).status_code == 200
    child = savings_category_for_account(conn, 2)
    assert conn.execute("SELECT parent_id FROM categories WHERE id = ?", (child,)).fetchone() == (cat["Épargne"],)
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE parent_id IS NULL AND kind = 'savings'"
                        ).fetchone() == (1,)


def _login(settings):
    client = TestClient(create_app(settings))       # chaque appel est un démarrage : le seed repasse
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return client


def test_deleted_or_renamed_seed_categories_do_not_come_back(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1)
    client = _login(settings)
    ids = {}
    for root in client.get("/api/meta").json()["categories"]:
        ids[root["name"]] = root["id"]
        ids.update({f"{root['name']}/{c['name']}": c["id"] for c in root["children"]})
    api = "/api/tabs/categories"
    assert client.delete(f"{api}/{ids['Frais bancaires']}", params={"move_to": ids["Divers"]},
                         headers=H).status_code == 200                # sa règle du seed part dans Divers
    assert client.patch(f"{api}/{ids['Cadeaux']}", json={"name": "Présents"}, headers=H).status_code == 200
    assert client.delete(f"{api}/{ids['Transport/Covoiturage']}", headers=H).status_code == 200
    assert client.patch(f"{api}/{ids['Revenus']}", json={"name": "Entrées"}, headers=H).status_code == 200
    conn = connect(settings.data_dir / "extranalyzer.db")
    conn.execute("DELETE FROM rules")           # installation de nouveau « neuve » : les règles du seed reviennent
    conn.commit()
    conn.close()

    names = _meta_names(_login(settings))
    assert "Frais bancaires" not in names and "Cadeaux" not in names and "Revenus" not in names
    assert names["Présents"] == [] and "Salaire" in names["Entrées"]
    assert "Covoiturage" not in names["Transport"] and "Train" in names["Transport"]
    conn = connect(settings.data_dir / "extranalyzer.db")
    patterns = {p for (p,) in conn.execute("SELECT pattern FROM rules")}
    conn.close()
    assert "INTERETS" in patterns and "F COTIS" not in patterns     # règle d'une catégorie écartée : ignorée

    # recréer une catégorie écartée la rend de nouveau au seed (et ne crée pas de doublon au démarrage)
    assert _login(settings).post(api, json={"name": "Cadeaux", "kind": "expense"}, headers=H).status_code == 201
    assert list(_meta_names(_login(settings))).count("Cadeaux") == 1

# --- Transactions ------------------------------------------------------------------------------

def test_transactions_list_shape_and_default_sort(env):
    client, _, cat, ids = env
    page = get(client, "/transactions/list", **JAN)
    assert page["total"] == 9 and page["page"] == 1 and page["pages"] == 1     # comptes courants seuls
    first = page["items"][0]
    assert first == {"id": ids["withdraw"], "op_date": "2025-01-25", "label": "VIR DEPUIS LIVRET",
                     "amount": "50.00", "account_id": 1, "category_id": cat["Épargne/Livret Bleu"],
                     "category_name": "Livret Bleu", "manual": True, "mirror": False,
                     "classified_by": "manual", "confidence": None, "suggestion_reason": None,
                     "receipt_id": None}
    dates = [i["op_date"] for i in page["items"]]
    assert dates == sorted(dates, reverse=True)


def test_transactions_list_filters_and_sorts(env):
    client, _, cat, ids = env
    assert [i["id"] for i in get(client, "/transactions/list", **JAN, q="marche")["items"]] == [ids["courses"]]
    assert [i["id"] for i in get(client, "/transactions/list", **JAN, category="none")["items"]] == [
        ids["unknown_credit"], ids["unknown"]]
    assert {i["id"] for i in get(client, "/transactions/list", **JAN, category=cat["Transport"])["items"]} == {
        ids["fuel"], ids["train"], ids["transport"]}
    amounts = [i["amount"] for i in get(client, "/transactions/list", **JAN, sort="amount_asc")["items"]]
    assert amounts[0] == "-300.00" and amounts[-1] == "2000.00"
    assert get(client, "/transactions/list", **JAN, sort="amount_desc")["items"][0]["id"] == ids["salary"]
    assert get(client, "/transactions/list", **JAN, sort="date_asc")["items"][0]["id"] == ids["salary"]
    with_mirror = get(client, "/transactions/list", **JAN, accounts="2")["items"]
    assert {(i["id"], i["mirror"]) for i in with_mirror} == {(ids["mirror"], True), (ids["interest"], False)}
    for bad in ({"sort": "random"}, {"category": "abc"}, {"page": "0"}):
        assert client.get("/api/tabs/transactions/list", params={**JAN, **bad}).status_code == 422


def test_transactions_pagination(env):
    client, conn, cat, _ = env
    conn.executemany(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
        "VALUES (1, ?, '2025-01-02', 'CB PAGE', 'cb page', -100)", [(f"p{i}",) for i in range(52)])
    conn.commit()
    p1 = get(client, "/transactions/list", **JAN)
    p2 = get(client, "/transactions/list", **JAN, page=2)
    assert p1["total"] == 61 and p1["pages"] == 2 and len(p1["items"]) == 50
    assert len(p2["items"]) == 11 and p2["page"] == 2
    assert get(client, "/transactions/list", **JAN, page=5)["items"] == []


def test_manual_reclassification_updates_summary_and_creates_override(env):
    client, conn, cat, ids = env
    r = client.post(f"/api/tabs/transactions/{ids['unknown']}/category",
                    json={"category_id": cat["Courses"]}, headers=H)
    assert r.status_code == 200
    row = r.json()
    assert (row["id"], row["category_id"], row["category_name"], row["manual"]) == (
        ids["unknown"], cat["Courses"], "Courses", True)
    tx_key = conn.execute("SELECT tx_key FROM transactions WHERE id = ?", (ids["unknown"],)).fetchone()[0]
    assert conn.execute("SELECT category_id FROM manual_overrides WHERE tx_key = ?",
                        (tx_key,)).fetchone() == (cat["Courses"],)
    s = get(client, "/overview/summary", **JAN)
    assert s["by_category"] == [
        {"id": cat["Courses"], "name": "Courses", "amount": "145.00"},
        {"id": cat["Transport"], "name": "Transport", "amount": "110.00"},
    ]
    assert s["totals"]["unclassified_debits"] == "0.00"


def test_reclassification_back_to_unclassified_and_errors(env):
    client, _, cat, ids = env
    url = f"/api/tabs/transactions/{ids['courses']}/category"
    r = client.post(url, json={"category_id": None}, headers=H)
    assert r.status_code == 200 and r.json()["manual"] is False
    assert client.post(url, json={"category_id": 9999}, headers=H).status_code == 404
    assert client.post("/api/tabs/transactions/9999/category", json={"category_id": cat["Courses"]},
                       headers=H).status_code == 404
    assert client.post(url, json={"category_id": cat["Courses"]}).status_code == 403   # en-tête CSRF


def test_reclassifying_a_mirror_moves_its_source(env):
    client, conn, cat, ids = env
    r = client.post(f"/api/tabs/transactions/{ids['mirror']}/category",
                    json={"category_id": cat["Épargne/CEL"]}, headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == ids["mirror"] and r.json()["category_name"] == "CEL"
    assert conn.execute("SELECT category_id, manual FROM transactions WHERE id = ?",
                        (ids["saving"],)).fetchone() == (cat["Épargne/CEL"], 1)


# --- Épargne -----------------------------------------------------------------------------------

def test_epargne_summary(env):
    client, _, cat, _ = env
    s = get(client, "/epargne/summary", start="2024-12-01", end="2025-01-31", accounts="1,2")
    dest = {d["name"]: d for d in s["destinations"]}
    assert dest["Livret Bleu"] == {"id": cat["Épargne/Livret Bleu"], "name": "Livret Bleu",
                                   "deposits": "400.00", "withdrawals": "50.00", "net": "350.00"}
    assert dest["Intérêts"]["deposits"] == "4.50" and dest["Intérêts"]["net"] == "4.50"
    assert dest["CEL"]["net"] == "0.00"
    assert s["total"] == {"deposits": "404.50", "withdrawals": "50.00", "net": "354.50"}
    assert s["monthly"] == [{"month": "2024-12", "net": "100.00", "net_cumulative": "100.00"},
                            {"month": "2025-01", "net": "254.50", "net_cumulative": "354.50"}]
    checking = get(client, "/epargne/summary", **JAN)
    assert checking["total"]["net"] == "250.00"


def test_epargne_dashboard_shape(env):
    client, conn, _, _ = env
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (3, 'A3', 'Livret vide fictif', 'savings')")
    conn.execute("INSERT INTO documents(sha256, filename, stored_path, parser, statement_date, imported_at) "
                 "VALUES ('shaD', 'f', 'p', 'x', '2025-01-31', '2025-02-01')")
    conn.execute("INSERT INTO sections(document_id, account_id, statement_date, closing_cents, gap_cents, status,"
                 " content_hash) VALUES (last_insert_rowid(), 2, '2025-01-31', 123456, 0, 'ok', 'h')")
    conn.commit()
    d = get(client, "/epargne/dashboard", **JAN)
    assert {"as_of", "total", "savings", "crypto", "places", "allocation", "series"} <= d.keys()
    assert d["as_of"] == "2025-01-31"
    assert d["savings"]["count"] == 2 and d["savings"]["unknown_count"] == 1
    assert d["savings"]["value"] == "1234.56"
    assert d["total"]["value"] == d["savings"]["value"]
    assert isinstance(d["savings"]["value"], str) and "." in d["savings"]["value"]
    unknown = next(p for p in d["places"] if p["name"] == "Livret vide fictif")
    assert unknown["value"] is None and unknown["unknown"] is True
    assert d["crypto"]["value"] is None and d["crypto"]["tracked_since"] is None
    assert all(isinstance(p["date"], str) for p in d["series"])


def test_epargne_dashboard_empty_db(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1)
    client = TestClient(create_app(settings))
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    d = get(client, "/epargne/dashboard", **JAN)
    assert d["places"] == [] and d["total"]["value"] is None
    assert d["series"] and all(p["savings"] is None and p["crypto"] is None for p in d["series"])


def test_epargne_dashboard_ignores_accounts_param(env):
    client = env[0]
    assert get(client, "/epargne/dashboard", **JAN) == get(client, "/epargne/dashboard", **JAN, accounts="1")


def test_epargne_dashboard_bad_period_422(env):
    client = env[0]
    url = "/api/tabs/epargne/dashboard"
    assert client.get(url, params={"start": "2025-02-01", "end": "2025-01-01"}).status_code == 422
    assert client.get(url, params={"end": "2025-01-31"}).status_code == 422


def test_paired_unclassified_transfer_is_savings_in_overview_and_epargne(env):
    client, conn, _, _ = env
    src = _add_unknown(conn, "p1", "2025-01-26", -7000, "VIR VERS LIVRET")
    conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, mirror_of) "
                 "VALUES (2, 'p2', '2025-01-26', 'VIR DEPUIS CC', 'vir depuis cc', 7000, ?)", (src,))
    conn.commit()
    totals = get(client, "/overview/summary", **JAN)["totals"]
    assert totals["unclassified_debits"] == "25.00" and totals["savings_net"] == "320.00"
    s = get(client, "/epargne/summary", **JAN)
    internal = next(d for d in s["destinations"] if d["id"] is None)
    assert internal == {"id": None, "name": "Virements internes (non classés)",
                        "deposits": "70.00", "withdrawals": "0.00", "net": "70.00"}
    assert s["total"]["net"] == "320.00" == s["monthly"][0]["net"]
    labels = [g["label_norm"] for g in get(client, "/a_classer/list", **JAN)["groups"]]
    assert "vir vers livret" in labels                           # toujours à classer (destination à choisir)



def _paired_unclassified(conn):
    src = _add_unknown(conn, "p1", "2025-01-26", -7000, "VIR VERS LIVRET")
    conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, mirror_of) "
                 "VALUES (2, 'p2', '2025-01-26', 'VIR DEPUIS CC', 'vir depuis cc', 7000, ?)", (src,))
    conn.commit()
    return src


def test_categories_tree_shows_paired_unclassified_transfer_under_epargne(env):
    client, conn, cat, _ = env
    _paired_unclassified(conn)
    savings_net = get(client, "/overview/summary", **JAN)["totals"]["savings_net"]
    tree = get(client, "/categories/tree", **JAN)
    epargne = next(r for r in tree["roots"] if r["name"] == "Épargne")
    assert epargne["amount"] == savings_net == "320.00"
    assert {"id": None, "name": "Virements internes (non classés)", "amount": "70.00"} in epargne["children"]
    assert all(r["id"] is not None for r in tree["roots"])
    detail = get(client, f"/categories/{cat['Épargne']}", **JAN)
    assert detail["amount"] == "320.00" and detail["monthly"] == [{"month": "2025-01", "amount": "320.00"}]
    assert {"id": None, "name": "Virements internes (non classés)", "amount": "70.00"} in detail["children"]


def test_categories_tree_counts_paired_rows_left_to_classify(env):
    client, conn, _, ids = env
    conn.execute("UPDATE transactions SET category_id = (SELECT id FROM categories WHERE name = 'Courses') "
                 "WHERE id IN (?, ?)", (ids["unknown"], ids["unknown_credit"]))
    conn.commit()
    assert get(client, "/categories/tree", **JAN)["unclassified"] == {"debits": "0.00", "credits": "0.00", "count": 0}
    _paired_unclassified(conn)
    assert get(client, "/categories/tree", **JAN)["unclassified"] == {"debits": "0.00", "credits": "0.00", "count": 1}

# --- À classer ---------------------------------------------------------------------------------

def _unclassified_count(client):
    return client.get("/api/meta").json()["unclassified_count"]


def _add_unknown(conn, key, day, cents, label="CB INCONNU ZETA", account=1):
    return conn.execute(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)", (account, key, day, label, label.lower(), cents)).lastrowid


def test_a_classer_groups_by_label(env):
    client, conn, _, _ = env
    _add_unknown(conn, "u1", "2025-01-22", -1000)
    _add_unknown(conn, "u2", "2024-12-22", -500)              # hors période
    _add_unknown(conn, "u3", "2025-01-23", -900, account=2)   # livret : hors vue par défaut
    conn.commit()
    data = get(client, "/a_classer/list", **JAN)
    no_suggestion = {"suggested_category_id": None, "suggested_name": None, "confidence": None, "reason": None,
                     "support": None}
    assert data["groups"] == [
        {"label_norm": "cb inconnu zeta", "count": 2, "total": "-35.00", "sample_label": "CB INCONNU ZETA",
         "first_date": "2025-01-20", "last_date": "2025-01-22", "sign": "debit", **no_suggestion},
        {"label_norm": "vir inconnu eta", "count": 1, "total": "7.00", "sample_label": "VIR INCONNU ETA",
         "first_date": "2025-01-21", "last_date": "2025-01-21", "sign": "credit", **no_suggestion},
    ]
    assert get(client, "/a_classer/list", **JAN, accounts="1,2")["groups"][0]["count"] == 3


def test_badge_count_equals_a_classer_list_over_full_range_and_all_accounts(env):
    client, conn, _, ids = env
    _add_unknown(conn, "u2", "2024-12-22", -500)
    _add_unknown(conn, "u3", "2025-01-23", -900, account=2)
    src = _add_unknown(conn, "p1", "2025-01-26", -700, "VIR VERS LIVRET")
    conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, mirror_of) "
                 "VALUES (2, 'p2', '2025-01-26', 'VIR DEPUIS CC', 'vir depuis cc', 700, ?)", (src,))
    conn.commit()
    meta = client.get("/api/meta").json()
    everything = {"start": meta["data_range"]["min"], "end": meta["data_range"]["max"],
                  "accounts": ",".join(str(a["id"]) for a in meta["accounts"])}
    groups = get(client, "/a_classer/list", **everything)["groups"]
    assert sum(g["count"] for g in groups) == meta["unclassified_count"] == 5


def test_a_classer_mixed_sign_and_sort(env):
    client, conn, _, _ = env
    _add_unknown(conn, "m1", "2025-01-03", -100, "VIR MIXTE IOTA")
    _add_unknown(conn, "m2", "2025-01-04", 200, "VIR MIXTE IOTA")
    conn.commit()
    groups = get(client, "/a_classer/list", **JAN)["groups"]
    # nombre décroissant, puis |total| décroissant
    assert [(g["label_norm"], g["sign"]) for g in groups] == [
        ("vir mixte iota", "mixed"), ("cb inconnu zeta", "debit"), ("vir inconnu eta", "credit")]


def test_a_classer_classify_with_rule_clears_the_group(env):
    client, conn, cat, _ = env
    _add_unknown(conn, "u1", "2025-01-22", -1000)
    old = _add_unknown(conn, "u2", "2024-12-22", -500)    # hors période : la règle la classe aussi
    conn.commit()
    before = _unclassified_count(client)
    r = client.post("/api/tabs/a_classer/classify", params=JAN, headers=H,
                    json={"label_norm": "cb inconnu zeta", "category_id": cat["Loisirs"], "create_rule": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["classified"] == 2                           # opérations du groupe affiché (janvier)
    assert body["unclassified_count"] == before - 3 == _unclassified_count(client)
    rule = conn.execute("SELECT pattern, match, direction, category_id FROM rules WHERE id = ?",
                        (body["rule_id"],)).fetchone()
    assert rule == ("cb inconnu zeta", "contains", None, cat["Loisirs"])
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE label_norm = 'cb inconnu zeta' "
                        "AND (category_id IS NOT ? OR rule_id IS NOT ? OR manual)",
                        (cat["Loisirs"], body["rule_id"])).fetchone()[0] == 0
    assert conn.execute("SELECT rule_id FROM transactions WHERE id = ?", (old,)).fetchone() == (body["rule_id"],)
    assert [g["label_norm"] for g in get(client, "/a_classer/list", **JAN)["groups"]] == ["vir inconnu eta"]


def test_a_classer_classify_with_custom_pattern(env):
    client, conn, cat, _ = env
    r = client.post("/api/tabs/a_classer/classify", params=JAN, headers=H,
                    json={"label_norm": "cb inconnu zeta", "category_id": cat["Loisirs"], "create_rule": True,
                          "pattern": " INCONNU ", "match": "contains", "direction": "debit"})
    assert r.status_code == 200, r.text
    assert r.json()["classified"] == 1                    # le crédit « vir inconnu eta » reste à classer
    assert conn.execute("SELECT pattern, direction FROM rules WHERE id = ?",
                        (r.json()["rule_id"],)).fetchone() == ("INCONNU", "debit")


def test_a_classer_classify_without_rule_is_manual_and_scoped(env):
    client, conn, cat, ids = env
    _add_unknown(conn, "u1", "2025-01-22", -1000)
    old = _add_unknown(conn, "u2", "2024-12-22", -500)
    conn.commit()
    rules_before = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    r = client.post("/api/tabs/a_classer/classify", params=JAN, headers=H,
                    json={"label_norm": "cb inconnu zeta", "category_id": cat["Courses"], "create_rule": False})
    assert r.status_code == 200, r.text
    assert r.json()["classified"] == 2 and r.json()["rule_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == rules_before
    assert conn.execute("SELECT category_id, manual FROM transactions WHERE id = ?",
                        (ids["unknown"],)).fetchone() == (cat["Courses"], 1)
    assert conn.execute("SELECT category_id, manual FROM transactions WHERE id = ?", (old,)).fetchone() == (None, 0)


def test_a_classer_classify_errors(env):
    client, conn, cat, _ = env
    url = "/api/tabs/a_classer/classify"
    base = {"label_norm": "cb inconnu zeta", "category_id": cat["Loisirs"], "create_rule": True}
    assert client.post(url, params=JAN, headers=H, json={**base, "category_id": 9999}).status_code == 404
    assert client.post(url, params=JAN, headers=H, json={**base, "label_norm": "absent"}).status_code == 404
    bad_regex = client.post(url, params=JAN, headers=H, json={**base, "pattern": "(", "match": "regex"})
    assert bad_regex.status_code == 422 and "expression régulière" in bad_regex.json()["detail"]
    assert client.post(url, params=JAN, headers=H, json={**base, "pattern": "autre chose"}).status_code == 422
    assert client.post(url, params=JAN, headers=H, json={**base, "match": "fuzzy"}).status_code == 422
    assert client.post(url, json=base, headers=H).status_code == 422                # période requise
    assert client.post(url, params=JAN, json=base).status_code == 403             # en-tête CSRF
    assert conn.execute("SELECT COUNT(*) FROM rules WHERE pattern IN ('(', 'autre chose', 'cb inconnu zeta')"
                        ).fetchone()[0] == 0
    assert _unclassified_count(client) == 2


def _teach(conn, cat, label, category, examples, unknowns, cents=-1500, day="2025-01-1"):
    """`examples` décisions manuelles puis `unknowns` opérations à classer de même libellé ; un passage de la
    chaîne. Renvoie les ids des opérations à classer."""
    key = label.lower().replace(" ", "-")
    for i in range(examples):
        conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, "
                     "category_id, manual, classified_by) VALUES (1, ?, ?, ?, ?, ?, ?, 1, 'manual')",
                     (f"{key}-m{i}", f"2024-12-1{i}", label, label.lower(), cents, cat[category]))
    new = [_add_unknown(conn, f"{key}-{day}{i}", f"{day}{i}", cents, label) for i in range(unknowns)]
    reclassify(conn)
    conn.commit()
    return new


def test_transactions_list_exposes_classified_by_and_confidence(env):
    client, conn, cat, ids = env
    [auto] = _teach(conn, cat, "PRLV ABONNEMENT OMEGA", "Loisirs", 3, 1)
    items = {i["id"]: i for i in get(client, "/transactions/list", **JAN)["items"]}
    assert items[auto]["classified_by"] == "model" and items[auto]["category_id"] == cat["Loisirs"]
    assert 0.9 <= items[auto]["confidence"] <= 1
    assert items[auto]["suggestion_reason"] == "3 opération(s) similaire(s) classée(s) ainsi"
    assert items[auto]["manual"] is False
    assert items[ids["courses"]]["classified_by"] == "manual" and items[ids["courses"]]["confidence"] is None
    detail = get(client, f"/categories/{cat['Loisirs']}", **JAN)["transactions"]
    assert [(t["id"], t["classified_by"]) for t in detail] == [(auto, "model")]
    assert detail[0]["confidence"] == items[auto]["confidence"]


def test_a_classer_group_has_suggestion(env):
    client, conn, cat, _ = env
    suggested = _teach(conn, cat, "PRLV CLUB SIGMA", "Transport/Train", 2, 2)     # 2 voisins : suggestion seule
    groups = {g["label_norm"]: g for g in get(client, "/a_classer/list", **JAN)["groups"]}
    group = groups["prlv club sigma"]
    assert group["count"] == 2
    assert group["suggested_category_id"] == cat["Transport/Train"]
    assert group["suggested_name"] == "Transport › Train"
    assert group["confidence"] == pytest.approx(1.0)
    assert group["reason"] == "2 opération(s) similaire(s) classée(s) ainsi"
    assert group["support"] == 2
    # le groupe prend la suggestion de sa ligne la plus sûre (support compris)
    conn.execute("UPDATE transactions SET suggested_category_id = ?, confidence = 0.5, suggestion_reason = 'x', "
                 "suggestion_support = 1 WHERE id = ?", (cat["Courses"], suggested[0]))
    conn.commit()
    group = {g["label_norm"]: g for g in get(client, "/a_classer/list", **JAN)["groups"]}["prlv club sigma"]
    assert (group["suggested_category_id"], group["suggested_name"]) == (cat["Transport/Train"], "Transport › Train")
    assert group["support"] == 2
    for label_norm, g in groups.items():
        if g["suggested_category_id"] is None:
            assert g["suggested_name"] is g["confidence"] is g["reason"] is g["support"] is None


ACCEPT = "/api/tabs/a_classer/accept_suggestions"


def _overrides(conn):
    return set(conn.execute("SELECT tx_key, category_id FROM manual_overrides").fetchall())


def _tx_keys(conn, tx_ids):
    return [conn.execute("SELECT tx_key FROM transactions WHERE id = ?", (i,)).fetchone()[0] for i in tx_ids]


def test_accept_suggestions_creates_manual_decisions(env):
    client, conn, cat, ids = env
    in_scope = _teach(conn, cat, "PRLV CLUB SIGMA", "Transport/Train", 2, 2)
    [out_of_scope] = _teach(conn, cat, "PRLV CLUB SIGMA", "Transport/Train", 0, 1, day="2024-11-1")
    rules_before = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    overrides_before = _overrides(conn)
    before = _unclassified_count(client)
    r = client.post(ACCEPT, params=JAN, headers=H, json={"items": [
        {"label_norm": "prlv club sigma", "category_id": cat["Transport/Train"]},
        {"label_norm": "cb inconnu zeta", "category_id": cat["Courses"]}]})
    assert r.status_code == 200, r.text
    # 4 exemples manuels désormais : le passage unique de la chaîne classe aussi l'opération hors période
    assert r.json() == {"classified": 3, "unclassified_count": before - 4}
    assert _unclassified_count(client) == before - 4
    for tx_id in in_scope:
        assert conn.execute("SELECT category_id, manual, classified_by FROM transactions WHERE id = ?",
                            (tx_id,)).fetchone() == (cat["Transport/Train"], 1, "manual")
    assert _overrides(conn) - overrides_before == {
        *((key, cat["Transport/Train"]) for key in _tx_keys(conn, in_scope)),
        (_tx_keys(conn, [ids["unknown"]])[0], cat["Courses"])}
    assert conn.execute("SELECT manual, classified_by FROM transactions WHERE id = ?",
                        (out_of_scope,)).fetchone() == (0, "model")
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == rules_before
    assert [g["label_norm"] for g in get(client, "/a_classer/list", **JAN)["groups"]] == ["vir inconnu eta"]


def test_accept_suggestions_applies_the_edited_category(env):
    client, conn, cat, _ = env
    in_scope = _teach(conn, cat, "PRLV CLUB SIGMA", "Transport/Train", 2, 2)     # suggestion : Train
    r = client.post(ACCEPT, params=JAN, headers=H,
                    json={"items": [{"label_norm": "prlv club sigma", "category_id": cat["Loisirs"]}]})
    assert r.status_code == 200, r.text
    assert r.json()["classified"] == 2
    for tx_id in in_scope:
        assert conn.execute("SELECT category_id, classified_by FROM transactions WHERE id = ?",
                            (tx_id,)).fetchone() == (cat["Loisirs"], "manual")


def test_accept_suggestions_applies_the_displayed_category_to_the_whole_group(env):
    client, conn, cat, _ = env
    first, second = _teach(conn, cat, "PRLV CLUB SIGMA", "Transport/Train", 2, 2)
    conn.execute("UPDATE transactions SET suggested_category_id = ?, confidence = 0.4 WHERE id = ?",
                 (cat["Loisirs"], second))          # suggestions stockées différentes dans le groupe
    conn.commit()
    shown = {g["label_norm"]: g for g in get(client, "/a_classer/list", **JAN)["groups"]}["prlv club sigma"]
    assert shown["suggested_category_id"] == cat["Transport/Train"]
    r = client.post(ACCEPT, params=JAN, headers=H,
                    json={"items": [{"label_norm": "prlv club sigma", "category_id": shown["suggested_category_id"]}]})
    assert r.status_code == 200, r.text
    assert r.json()["classified"] == 2
    assert {conn.execute("SELECT category_id FROM transactions WHERE id = ?", (i,)).fetchone()[0]
            for i in (first, second)} == {cat["Transport/Train"]}


def test_accept_suggestions_errors(env):
    client, conn, cat, _ = env
    item = {"label_norm": "cb inconnu zeta", "category_id": cat["Courses"]}
    before = _unclassified_count(client)
    assert client.post(ACCEPT, params=JAN, headers=H, json={"items": []}).status_code == 422
    assert client.post(ACCEPT, params=JAN, headers=H, json={}).status_code == 422
    unknown = client.post(ACCEPT, params=JAN, headers=H, json={"items": [item, {**item, "label_norm": "vir inconnu eta",
                                                                                 "category_id": 9999}]})
    assert unknown.status_code == 404
    assert client.post(ACCEPT, headers=H, json={"items": [item]}).status_code == 422          # période requise
    assert client.post(ACCEPT, params=JAN, json={"items": [item]}).status_code == 403        # en-tête CSRF
    assert _unclassified_count(client) == before
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE manual = 1 AND category_id IS NULL").fetchone() == (0,)


# --- Relevés -----------------------------------------------------------------------------------

def _statements(conn, ids):
    conn.executescript("""
        UPDATE accounts SET number = '00012345678901' WHERE id = 1;
        INSERT INTO documents(id, sha256, filename, stored_path, parser, statement_date, imported_at) VALUES
            (1, 'h1', 'releve-janvier.pdf', 'h1.pdf', 'creditmutuel', '2025-01-31', '2025-02-01T10:00:00'),
            (2, 'h2', 'releve 00012345678901.pdf', 'h2.pdf', 'creditmutuel', '2025-02-28', '2025-03-01T10:00:00');
        INSERT INTO sections(id, document_id, account_id, statement_date, opening_cents, closing_cents, gap_cents,
                             status, content_hash) VALUES
            (10, 1, 1, '2025-01-31', 0, 100, 0, 'ok', 'c10'),
            (11, 1, 2, '2025-01-31', 0, 100, 0, 'conflict', 'c11'),
            (20, 2, 1, '2025-02-28', 0, 100, -1234, 'error', 'c20');
    """)
    conn.execute("UPDATE transactions SET section_id = 10 WHERE id IN (?, ?)", (ids["salary"], ids["courses"]))
    conn.commit()


def test_releves_list(env):
    client, conn, _, ids = env
    _statements(conn, ids)
    r = client.get("/api/tabs/releves/list")
    assert r.status_code == 200
    assert "12345678901" not in r.text                          # ni compte ni nom de fichier en clair
    docs = r.json()["documents"]
    assert [d["id"] for d in docs] == [2, 1]                    # le plus récent d'abord
    assert docs[0]["filename"] == "releve ••8901.pdf"
    assert {k: docs[1][k] for k in ("filename", "statement_date", "imported_at")} == {
        "filename": "releve-janvier.pdf", "statement_date": "2025-01-31", "imported_at": "2025-02-01T10:00:00"}
    assert docs[1]["sections"] == [
        {"id": 10, "account_label": "Courant fictif", "number_tail": "8901", "statement_date": "2025-01-31",
         "status": "ok", "gap": "0.00", "tx_count": 2, "other_ok": False, "message": None},
        {"id": 11, "account_label": "Livret fictif", "number_tail": "A2", "statement_date": "2025-01-31",
         "status": "conflict", "gap": "0.00", "tx_count": 0, "other_ok": False, "message": None},
    ]
    error = docs[0]["sections"][0]
    assert (error["status"], error["gap"], error["message"]) == ("error", "-12.34", "écart de -12,34 €")


def test_releves_shows_stored_section_message(env):
    client, conn, _, ids = env
    _statements(conn, ids)
    conn.execute("UPDATE sections SET message = 'opérations déjà importées avec le relevé du compte "
                 "00012345678901 au 2025-01-31' WHERE id = 20")
    conn.commit()
    error = client.get("/api/tabs/releves/list").json()["documents"][0]["sections"][0]
    assert error["message"] == "opérations déjà importées avec le relevé du compte ••8901 au 2025-01-31"


def test_releves_conflict_is_resolved_by_the_core_route(env):
    client, conn, _, ids = env
    _statements(conn, ids)
    r = client.post("/api/sections/11/resolve", json={"accept": False}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "superseded"
    docs = client.get("/api/tabs/releves/list").json()["documents"]
    assert docs[1]["sections"][1]["status"] == "superseded"


# --- Règles ------------------------------------------------------------------------------------

def _rule(client, **fields):
    r = client.post("/api/tabs/regles", json=fields, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def test_regles_create_and_list(env):
    client, conn, cat, ids = env
    created = _rule(client, pattern="inconnu", match="contains", category_id=cat["Transport/Train"])
    assert created["category_path"] == "Transport › Train" and created["tx_count"] == 2
    assert created["priority"] == 70 and created["direction"] is None and created["account_id"] is None
    assert conn.execute("SELECT category_id, rule_id FROM transactions WHERE id = ?",
                        (ids["unknown"],)).fetchone() == (cat["Transport/Train"], created["id"])
    low = _rule(client, pattern="^cb", match="regex", category_id=cat["Divers"], priority=-1, direction="debit")
    rules = client.get("/api/tabs/regles").json()["rules"]
    assert rules[-1]["id"] == low["id"]
    assert [r["priority"] for r in rules] == sorted((r["priority"] for r in rules), reverse=True)
    assert next(r for r in rules if r["id"] == created["id"]) == created
    seed = next(r for r in rules if r["pattern"] == "INTERETS")
    assert seed["category_path"] == "Épargne › Intérêts" and seed["tx_count"] == 0   # classée sans règle


def test_regles_list_auto_info(env, monkeypatch):
    client, conn, cat, _ = env
    assert client.get("/api/tabs/regles").json()["auto"] == {"count": 0, "threshold": 0.9}
    _teach(conn, cat, "PRLV ABONNEMENT OMEGA", "Loisirs", 3, 2)
    monkeypatch.setenv("EXTRANALYZER_AUTO_THRESHOLD", "0.95")
    assert client.get("/api/tabs/regles").json()["auto"] == {"count": 2, "threshold": 0.95}


def test_regles_create_errors(env):
    client, _, cat, _ = env
    url = "/api/tabs/regles"
    assert client.post(url, json={"pattern": "x", "match": "contains", "category_id": 9999},
                       headers=H).status_code == 404
    assert client.post(url, json={"pattern": "x", "match": "contains", "category_id": cat["Divers"],
                                  "account_id": 99}, headers=H).status_code == 404
    r = client.post(url, json={"pattern": "[", "match": "regex", "category_id": cat["Divers"]}, headers=H)
    assert r.status_code == 422 and "expression régulière" in r.json()["detail"]
    assert client.post(url, json={"pattern": "  ", "match": "contains", "category_id": cat["Divers"]},
                       headers=H).status_code == 422


def test_regles_patch_reclassifies(env):
    client, conn, cat, ids = env
    rule = _rule(client, pattern="inconnu", match="contains", category_id=cat["Loisirs"])
    r = client.patch(f"/api/tabs/regles/{rule['id']}", json={"category_id": cat["Courses"]}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["category_path"] == "Courses" and r.json()["tx_count"] == 2
    assert conn.execute("SELECT category_id FROM transactions WHERE id = ?",
                        (ids["unknown"],)).fetchone() == (cat["Courses"],)
    # sens « débit » : le crédit « vir inconnu eta » retourne à classer
    r = client.patch(f"/api/tabs/regles/{rule['id']}", json={"direction": "debit"}, headers=H)
    assert r.json()["direction"] == "debit" and r.json()["tx_count"] == 1
    assert conn.execute("SELECT category_id, rule_id FROM transactions WHERE id = ?",
                        (ids["unknown_credit"],)).fetchone() == (None, None)


def test_regles_patch_and_delete_errors(env):
    client, conn, cat, _ = env
    rule = _rule(client, pattern="inconnu", match="contains", category_id=cat["Loisirs"])
    url = f"/api/tabs/regles/{rule['id']}"
    r = client.patch(url, json={"pattern": "(", "match": "regex"}, headers=H)
    assert r.status_code == 422 and "expression régulière" in r.json()["detail"]
    assert conn.execute("SELECT pattern, match FROM rules WHERE id = ?", (rule["id"],)).fetchone() == (
        "inconnu", "contains")
    assert client.patch(url, json={"pattern": None}, headers=H).status_code == 422
    assert client.patch(url, json={"category_id": 9999}, headers=H).status_code == 404
    assert client.patch(url, json={"account_id": 99}, headers=H).status_code == 404
    assert client.patch("/api/tabs/regles/9999", json={"pattern": "x"}, headers=H).status_code == 404
    assert client.delete("/api/tabs/regles/9999", headers=H).status_code == 404
    assert client.delete(url).status_code == 403                                  # en-tête CSRF


def test_regles_delete_returns_transactions_to_unclassified(env):
    client, conn, cat, _ = env
    rule = _rule(client, pattern="inconnu", match="contains", category_id=cat["Loisirs"])
    before = _unclassified_count(client)
    r = client.delete(f"/api/tabs/regles/{rule['id']}", headers=H)
    assert r.status_code == 204 and r.content == b""
    assert _unclassified_count(client) == before + 2
    assert conn.execute("SELECT COUNT(*) FROM rules WHERE id = ?", (rule["id"],)).fetchone()[0] == 0


def test_regles_test_preview(env):
    client, conn, _, _ = env
    url = "/api/tabs/regles/test"
    r = client.post(url, json={"pattern": "VIR LIVRET", "match": "contains"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json() == {"labels": ["vir livret bleu"], "total": 1, "tx_count": 2}   # miroir exclu
    starts = client.post(url, json={"pattern": "cb ", "match": "startswith"}, headers=H).json()
    assert starts["total"] == 5 and starts["labels"][0] == "cb marche beta"         # le plus fréquent d'abord
    credit = client.post(url, json={"pattern": "inconnu", "match": "contains", "direction": "credit"},
                         headers=H).json()
    assert credit["labels"] == ["vir inconnu eta"]
    conn.executemany("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
                     "VALUES (1, ?, '2025-01-02', ?, ?, -100)",
                     [(f"t{i}", f"CB LOT {i:02d}", f"cb lot {i:02d}") for i in range(25)])
    conn.commit()
    many = client.post(url, json={"pattern": "^cb lot", "match": "regex"}, headers=H).json()
    assert len(many["labels"]) == 20 and many["total"] == 25
    bad = client.post(url, json={"pattern": "(", "match": "regex"}, headers=H)
    assert bad.status_code == 422 and "expression régulière" in bad.json()["detail"]


# --- Modules -----------------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["overview", "budget", "categories", "courses", "transactions", "epargne", "a_classer",
                                    "releves", "regles"])
def test_tab_module_imports_alone_without_app_main(module):
    code = (f"import sys, app.tabs.{module} as m; "
            f"assert m.TAB.id == {module!r}; assert 'app.main' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_epargne_dashboard_crypto_amounts_are_euro_strings(env):
    client, conn, cat, _ = env
    category = cat["Épargne/Livret Bleu"]
    conn.execute("INSERT INTO wallets(id, address, label, savings_category_id, created_at) "
                 "VALUES (1, 'addrfictive', 'Portefeuille fictif', ?, '2025-01-01')", (category,))
    for sid, taken, cents in ((1, "2024-12-31T10:00:00+00:00", 50000), (2, "2025-01-31T10:00:00+00:00", 80000)):
        conn.execute("INSERT INTO wallet_snapshots(id, wallet_id, taken_at, status, total_eur_cents, unpriced_count)"
                     " VALUES (?, 1, ?, 'ok', ?, 0)", (sid, taken, cents))
    conn.commit()
    d = get(client, "/epargne/dashboard", **JAN)
    c = d["crypto"]
    assert c["value"] == "800.00" and c["compared_value"] == "800.00"
    assert c["deposited"] == "350.00" and c["gap"] == "450.00"
    assert c["compared_partial"] is False
    wallet = next(p for p in d["places"] if p["kind"] == "wallet")
    assert wallet["change"] == "300.00"
    assert wallet["deposited"] == c["deposited"] and wallet["gap"] == c["gap"]
    assert isinstance(wallet["gap"], str) and "." in wallet["gap"]
    assert d["series"][-1]["crypto"] == "800.00"


def test_epargne_manual_balance_route(env, monkeypatch):
    client, conn, _, _ = env
    monkeypatch.setattr("app.tabs.epargne._today", lambda: date(2025, 1, 31))
    conn.execute("INSERT INTO documents(sha256, filename, stored_path, parser, statement_date, imported_at) "
                 "VALUES ('shaM', 'f', 'p', 'x', '2025-01-15', '2025-02-01')")
    conn.execute("INSERT INTO sections(document_id, account_id, statement_date, closing_cents, gap_cents, status,"
                 " content_hash) VALUES (last_insert_rowid(), 2, '2025-01-15', 100000, 0, 'ok', 'h')")
    conn.commit()
    url = "/api/tabs/epargne/balance/2"
    assert client.put(url, json={"amount": "1 234,56"}, headers=H).status_code == 200
    d = get(client, "/epargne/dashboard", **JAN)
    livret = next(p for p in d["places"] if p["id"] == 2 and p["kind"] == "savings")
    assert livret["value"] == "1234.56" and livret["balance_source"] == "manual"
    assert livret["balance_as_of"] == "2025-01-31"
    assert client.put(url, json={"amount": "-5"}, headers=H).status_code == 422
    assert client.put(url, json={"amount": "abc"}, headers=H).status_code == 422
    assert client.put(url, json={"amount": "10", "day": "2025-02-02"}, headers=H).status_code == 422   # futur
    assert client.put("/api/tabs/epargne/balance/1", json={"amount": "10"}, headers=H).status_code == 404  # courant
    assert client.put(url, json={"amount": "10"}).status_code == 403                                      # CSRF
    assert client.delete(url, headers=H).status_code == 200
    d = get(client, "/epargne/dashboard", **JAN)
    livret = next(p for p in d["places"] if p["id"] == 2 and p["kind"] == "savings")
    assert livret["value"] != "1234.56" and livret["balance_source"] in ("statement", "estimated")


@pytest.mark.parametrize("raw, cents", [("12345,67", 1234567), ("12 345,67", 1234567), ("12.345,67", 1234567),
                                        ("12345.67", 1234567), ("1234", 123400), ("1 234 €", 123400)])
def test_epargne_amount_parsing(raw, cents):
    from app.tabs.epargne import _amount_cents
    assert _amount_cents(raw) == cents


@pytest.mark.parametrize("raw", ["1.234", "12.345.678", "-3", "abc", "1,2,3"])
def test_epargne_amount_parsing_rejects(raw):
    from fastapi import HTTPException

    from app.tabs.epargne import _amount_cents
    with pytest.raises(HTTPException):
        _amount_cents(raw)


def test_epargne_manual_balance_before_last_statement_422(env, monkeypatch):
    client, conn, _, _ = env
    monkeypatch.setattr("app.tabs.epargne._today", lambda: date(2025, 1, 31))
    conn.execute("INSERT INTO documents(sha256, filename, stored_path, parser, statement_date, imported_at) "
                 "VALUES ('shaN', 'f', 'p', 'x', '2025-01-15', '2025-02-01')")
    conn.execute("INSERT INTO sections(document_id, account_id, statement_date, closing_cents, gap_cents, status,"
                 " content_hash) VALUES (last_insert_rowid(), 2, '2025-01-15', 100000, 0, 'ok', 'h')")
    conn.commit()
    r = client.put("/api/tabs/epargne/balance/2", json={"amount": "10", "day": "2025-01-10"}, headers=H)
    assert r.status_code == 422


def test_epargne_crypto_group_route(env):
    client, conn, _, _ = env
    conn.execute("INSERT INTO wallets(id, address, label, created_at) VALUES (1, 'addrfictive', 'W', '2025-01-01')")
    conn.execute("INSERT INTO wallet_snapshots(id, wallet_id, taken_at, status, total_eur_cents, unpriced_count)"
                 " VALUES (1, 1, '2025-01-31T10:00:00+00:00', 'ok', 3000, 0)")
    for mint, symbol, cents in (("mA", "AAA", 2000), ("mB", "BBB", 1000)):
        conn.execute("INSERT INTO wallet_positions(snapshot_id, kind, mint, symbol, amount, value_eur_cents)"
                     " VALUES (1, 'token', ?, ?, '1', ?)", (mint, symbol, cents))
    conn.commit()
    base = "/api/tabs/epargne/crypto"
    assert client.put(f"{base}/group/mA", json={"group": "Altcoins"}, headers=H).status_code == 200
    assert client.put(f"{base}/group/mB", json={"group": "Altcoins"}, headers=H).status_code == 200
    cats = get(client, "/epargne/dashboard", **JAN)["crypto_categories"]
    assert cats == [{"key": "group:Altcoins", "name": "Altcoins", "group": True, "value": "30.00", "members": [{"key": "mA", "name": "AAA", "value": "20.00"},
                                                  {"key": "mB", "name": "BBB", "value": "10.00"}]}]
    assert client.put(f"{base}/group/mA", json={"group": " "}, headers=H).status_code == 422
    assert client.put(f"{base}/group/mA", json={"group": None}).status_code == 403          # CSRF
    assert client.put(f"{base}/group/mA", json={"group": None}, headers=H).status_code == 200
    cats = {c["key"]: c for c in get(client, "/epargne/dashboard", **JAN)["crypto_categories"]}
    assert cats["mA"]["group"] is False and cats["mB"]["group"] is False        # groupe d'un seul membre : dissous
    assert client.post(f"{base}/merge", json={"source": "mB", "target": "mA", "name": "AAA"},
                       headers=H).status_code == 200
    cats = {c["key"]: c for c in get(client, "/epargne/dashboard", **JAN)["crypto_categories"]}
    assert set(cats) == {"group:AAA"} and cats["group:AAA"]["value"] == "30.00"
    assert client.post(f"{base}/merge", json={"source": "mA", "target": "mA", "name": "x"},
                       headers=H).status_code == 422
    assert client.post(f"{base}/merge", json={"source": "mA", "target": "mB", "name": " "},
                       headers=H).status_code == 422
    assert client.post(f"{base}/merge", json={"source": "mA", "target": "mB", "name": "B"}).status_code == 403
