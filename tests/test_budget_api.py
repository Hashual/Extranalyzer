"""Onglet Budget : objectifs datés, suivi de période, assistant et conseils (données fictives)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import Settings
from app.main import create_app
from app.store.db import connect
from app.tabs import budget as budget_tab

PASSWORD = "mot-de-passe-test"
H = {"X-Extranalyzer": "1"}
APRIL = {"start": "2025-04-01", "end": "2025-04-30"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Aujourd'hui : 15/04/2025. Données de janvier à avril 2025 sur un compte courant fictif :
    loyer 600 € le 1er, courses 100 € le 5 et le 20 (avril : 150 € le 5 seulement), restaurants le 10
    (50, 150, 100 puis 120 €), salaire 2 000 € le 3."""
    monkeypatch.setattr(budget_tab, "_today", lambda: date(2025, 4, 15))
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1)
    client = TestClient(create_app(settings))
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    conn = connect(settings.data_dir / "extranalyzer.db")
    cat = {name: cid for cid, name in conn.execute("SELECT id, name FROM categories WHERE parent_id IS NULL")}
    cat["Revenus/Salaire"] = conn.execute("SELECT id FROM categories WHERE name = 'Salaire'").fetchone()[0]
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'Courant fictif', 'checking')")
    n = 0

    def tx(day, cents, category, label):
        nonlocal n
        n += 1
        conn.execute(
            "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, category_id,"
            " manual, classified_by) VALUES (1, ?, ?, ?, ?, ?, ?, 1, 'manual')",
            (f"k{n}", day, label, label, cents, cat[category]))

    for month, resto in (("01", 5000), ("02", 15000), ("03", 10000), ("04", 12000)):
        tx(f"2025-{month}-01", -60000, "Logement", "PRLV SEPA LOYER FICTIF")
        tx(f"2025-{month}-03", 200000, "Revenus/Salaire", "VIR SALAIRE FICTIF")
        tx(f"2025-{month}-10", -resto, "Restauration", "CB RESTO FICTIF")
        if month != "04":
            tx(f"2025-{month}-05", -10000, "Courses", "CB MARCHE FICTIF")
            tx(f"2025-{month}-20", -10000, "Courses", "CB MARCHE FICTIF")
    tx("2025-04-05", -15000, "Courses", "CB MARCHE FICTIF")
    conn.commit()
    yield client, conn, cat
    conn.close()


def get(client, path, **params):
    r = client.get(f"/api/tabs/budget{path}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def set_targets(client, cat, from_month, **by_name):
    r = client.put("/api/tabs/budget/targets", headers=H, json={
        "from_month": from_month,
        "targets": [{"category_id": cat[name], "amount": amount} for name, amount in by_name.items()]})
    assert r.status_code == 200, r.text
    return r.json()


def rows_by_name(summary):
    return {r["name"]: r for r in summary["rows"]}


def test_tab_is_listed_after_overview(env):
    client = env[0]
    ids = [t["id"] for t in client.get("/api/tabs").json()]
    assert ids.index("budget") == ids.index("overview") + 1


def test_summary_without_targets(env):
    client = env[0]
    s = get(client, "/summary", **APRIL)
    rows = rows_by_name(s)
    assert rows["Logement"]["spent"] == "600.00" and rows["Logement"]["status"] == "none"
    assert rows["Logement"]["target"] is None and rows["Logement"]["percent"] is None
    assert s["totals"]["target"] is None and s["totals"]["spent_unbudgeted"] == "870.00"
    assert s["history_months"] == ["2025-01", "2025-02", "2025-03"]
    assert "À classer" not in rows                      # aucune dépense à classer : pas de ligne


def test_summary_follows_habits(env):
    client, _, cat = env
    set_targets(client, cat, "2025-01", Logement="600", Courses="180")
    set_targets(client, cat, "2025-04", Restauration="80")
    s = get(client, "/summary", **APRIL)
    rows = rows_by_name(s)
    # Loyer : tout part le 1er d'habitude, attendu à ce jour = objectif entier, pas de dépense restante.
    assert (rows["Logement"]["expected"], rows["Logement"]["projection"], rows["Logement"]["status"]) == \
        ("600.00", "600.00", "ok")
    # Courses : la moitié d'habitude avant le 15 -> 90 € attendus, 150 € dépensés, 100 € d'habitude ensuite.
    assert (rows["Courses"]["expected"], rows["Courses"]["projection"], rows["Courses"]["status"]) == \
        ("90.00", "250.00", "risk")
    assert rows["Courses"]["percent"] == pytest.approx(150 / 180)
    assert rows["Restauration"]["status"] == "over"
    assert s["in_progress"] and s["days_left"] == 16
    t = s["totals"]
    assert (t["target"], t["spent_budgeted"], t["spent_unbudgeted"]) == ("860.00", "870.00", "0.00")
    assert t["per_day"] == "-0.62"                     # (860 − 870) / 16 jours, au centime pair
    april = next(m for m in s["monthly"] if m["month"] == "2025-04")
    assert (april["target"], april["spent"]) == ("860.00", "870.00")
    jan = next(m for m in s["monthly"] if m["month"] == "2025-01")
    assert (jan["target"], jan["spent"], jan["spent_all"]) == ("780.00", "800.00", "850.00")


def test_target_history_keeps_past_months(env):
    client, conn, cat = env
    set_targets(client, cat, "2025-01", Courses="180")
    set_targets(client, cat, "2025-06", Courses="300")
    set_targets(client, cat, "2025-04", Courses="250")       # remplace aussi le changement de juin
    assert conn.execute("SELECT effective_from, monthly_cents FROM budget_targets ORDER BY 1").fetchall() == [
        ("2025-01", 18000), ("2025-04", 25000)]
    march = rows_by_name(get(client, "/summary", start="2025-03-01", end="2025-03-31"))
    assert march["Courses"]["target"] == "180.00" and march["Courses"]["status"] == "over"
    assert march["Courses"]["expected"] is None              # période terminée : ni attendu ni projection
    q = rows_by_name(get(client, "/summary", start="2025-01-01", end="2025-03-31"))
    assert q["Courses"]["target"] == "540.00"
    t = get(client, "/targets", month="2025-05")
    courses = next(c for c in t["categories"] if c["name"] == "Courses")
    assert (courses["amount"], courses["since"]) == ("250.00", "2025-04")
    assert [(c["month"], c["amount"], c["previous"]) for c in t["changes"]] == [
        ("2025-04", "250.00", "180.00"), ("2025-01", "180.00", None)]


def test_saving_same_value_or_removing_target(env):
    client, conn, cat = env
    set_targets(client, cat, "2025-01", Courses="180")
    assert set_targets(client, cat, "2025-03", Courses="180,00")["changed"] == 0
    assert set_targets(client, cat, "2025-03", Courses="")["changed"] == 1       # plus d'objectif dès mars
    assert conn.execute("SELECT effective_from, monthly_cents FROM budget_targets ORDER BY 1").fetchall() == [
        ("2025-01", 18000), ("2025-03", None)]
    assert set_targets(client, cat, "2025-03", Courses="180")["changed"] == 1    # retour : la ligne disparaît
    assert conn.execute("SELECT COUNT(*) FROM budget_targets").fetchone()[0] == 1
    feb = rows_by_name(get(client, "/summary", start="2025-02-01", end="2025-02-28"))
    assert feb["Courses"]["target"] == "180.00"


def test_partial_month_target_uses_habits(env):
    client, _, cat = env
    set_targets(client, cat, "2025-01", Courses="200")
    rows = rows_by_name(get(client, "/summary", start="2025-03-01", end="2025-03-10"))
    assert rows["Courses"]["target"] == "100.00"             # la moitié des courses tombe avant le 10


@pytest.mark.parametrize("body, message", [
    ({"from_month": "2025-13", "targets": []}, "AAAA-MM"),
    ({"from_month": "2025-01", "targets": [{"category_id": 0, "amount": "10"}]}, "budgétable"),
    ({"from_month": "2025-01", "targets": [{"category_id": "COURSES", "amount": "abc"}]}, "invalide"),
    ({"from_month": "2025-01", "targets": [{"category_id": "COURSES", "amount": "-5"}]}, "invalide"),
    ({"from_month": "2025-01", "targets": [{"category_id": "COURSES", "amount": "1.234"}]}, "invalide"),
    ({"from_month": "2025-01", "targets": [{"category_id": "SALAIRE", "amount": "10"}]}, "budgétable"),
])
def test_put_targets_rejects_invalid_input(env, body, message):
    client, _, cat = env
    for t in body["targets"]:
        t["category_id"] = {"COURSES": cat["Courses"], "SALAIRE": cat["Revenus/Salaire"]}.get(t["category_id"],
                                                                                            t["category_id"])
    r = client.put("/api/tabs/budget/targets", headers=H, json=body)
    assert r.status_code == 422 and message in r.json()["detail"]


def test_deleting_a_category_drops_its_targets(env):
    client, conn, cat = env
    set_targets(client, cat, "2025-01", Divers="50")
    assert client.delete(f"/api/tabs/categories/{cat['Divers']}", headers=H).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM budget_targets").fetchone()[0] == 0


def test_suggest_keeps_fixed_costs_and_trims_variable_ones(env):
    client = env[0]
    s = get(client, "/suggest", savings="1000")
    cats = {c["name"]: c for c in s["categories"]}
    assert s["income_average"] == "2000.00" and s["income"] == "2000.00"
    assert cats["Logement"]["fixed"] and cats["Logement"]["suggested"] == "600.00"
    assert cats["Courses"]["fixed"] and cats["Courses"]["suggested"] == "200.00"
    assert not cats["Restauration"]["fixed"] and cats["Restauration"]["suggested"] == "90.00"   # 100 − 10 %
    assert cats["Shopping"]["suggested"] is None                  # rien dépensé : pas d'objectif proposé
    assert s["feasible"] and s["total"] == "890.00" and s["projected_savings"] == "1110.00"


def test_suggest_reports_unreachable_savings(env):
    client = env[0]
    s = get(client, "/suggest", savings="1350", effort="medium")
    cats = {c["name"]: c for c in s["categories"]}
    assert not s["feasible"]
    assert cats["Restauration"]["suggested"] == "50.00"           # jamais sous la moitié de la moyenne
    assert get(client, "/suggest", income="3000", savings="1350")["feasible"]
    assert client.get("/api/tabs/budget/suggest", params={"income": "beaucoup"}).status_code == 422
    assert client.get("/api/tabs/budget/suggest", params={"effort": "max"}).status_code == 422


def test_insights(env):
    client, _, cat = env
    set_targets(client, cat, "2025-01", Courses="180")
    i = get(client, "/insights", start="2025-01-01", end="2025-04-30")
    assert i["merchants"][0] == {"label": "PRLV SEPA LOYER FICTIF", "name": "LOYER FICTIF", "total": "2400.00",
                                 "count": 4}
    assert {s["name"] for s in i["subscriptions"]} >= {"LOYER FICTIF"}
    loyer = next(s for s in i["subscriptions"] if s["name"] == "LOYER FICTIF")
    assert (loyer["monthly"], loyer["yearly"]) == ("600.00", "7200.00")
    texts = [t["text"] for t in i["tips"]]
    assert any("« Courses » a dépassé son objectif ces 3 derniers mois" in t for t in texts)
    assert any(t.startswith("Courses (") for t in texts)          # astuce de catégorie : poids lourd
    assert any("épargné 0 % de tes revenus" in t for t in texts)  # aucune épargne : sous le repère de 20 %


def test_empty_account_selection(env):
    client = env[0]
    s = get(client, "/summary", **APRIL, accounts="99")
    assert all(r["spent"] == "0.00" for r in s["rows"])
    i = get(client, "/insights", **APRIL, accounts="99")
    assert i["merchants"] == [] and i["subscriptions"] == []
