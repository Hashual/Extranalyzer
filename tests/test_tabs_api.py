"""API des onglets Vue d'ensemble, Catégories, Transactions et Épargne (données fictives, insérées en SQL)."""
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
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
        nonlocal n
        n += 1
        cur = conn.execute(
            "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, category_id,"
            " mirror_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (acc, f"k{n}", day, label, label.lower(), cents, cat[category] if category else None, mirror))
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
                                  "/epargne/summary"])
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
    assert [t["id"] for t in env[0].get("/api/tabs").json()] == ["overview", "categories", "transactions",
                                                                 "epargne"]


# --- Vue d'ensemble ----------------------------------------------------------------------------

def test_overview_summary_totals(env):
    client = env[0]
    s = get(client, "/overview/summary", **JAN)
    # Dépenses : 120 + 60 + 40 + 10 + 25 (à classer) ; revenus : 2000 + 7 (à classer).
    # Épargne (compte courant seul) : 300 − 50 = 250 ; solde = 2007 − 255 − 250.
    assert s["totals"] == {"expenses": "255.00", "income": "2007.00", "savings_net": "250.00",
                           "balance": "1502.00", "unclassified_debits": "25.00", "unclassified_credits": "7.00"}
    assert s["compare_totals"] is None
    assert s["by_category"] == [
        {"id": env[2]["Courses"], "name": "Courses", "amount": "120.00"},
        {"id": env[2]["Transport"], "name": "Transport", "amount": "110.00"},
        {"id": None, "name": "À classer", "amount": "25.00"},
    ]
    assert [m["month"] for m in s["monthly"]] == ["2025-01"]
    assert s["monthly"][0]["expenses"] == "255.00"
    assert s["recurring"] == []


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
    assert tree["unclassified"] == {"debits": "25.00", "credits": "7.00"}


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
    assert r.json()["category_name"] == "Loisirs"


# --- Transactions ------------------------------------------------------------------------------

def test_transactions_list_shape_and_default_sort(env):
    client, _, cat, ids = env
    page = get(client, "/transactions/list", **JAN)
    assert page["total"] == 9 and page["page"] == 1 and page["pages"] == 1     # comptes courants seuls
    first = page["items"][0]
    assert first == {"id": ids["withdraw"], "op_date": "2025-01-25", "label": "VIR DEPUIS LIVRET",
                     "amount": "50.00", "account_id": 1, "category_id": cat["Épargne/Livret Bleu"],
                     "category_name": "Livret Bleu", "manual": False, "mirror": False}
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
