from datetime import date
from decimal import Decimal

import pytest

from app.analytics.periods import Period, previous_period
from app.analytics.recurring import recurring
from app.analytics.totals import by_category, monthly_series, top_increases, totals
from app.store.db import connect

P = Period(date(2025, 1, 1), date(2025, 1, 31))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.executescript(
        """
        INSERT INTO accounts(id,number,label,kind) VALUES (1,'A1','Courant','checking'),
                                                          (2,'A2','Livret','savings');
        INSERT INTO categories(id,name,parent_id,kind) VALUES
          (1,'Salaire',NULL,'income'),(2,'Alimentation',NULL,'expense'),
          (3,'Courses',2,'expense'),(4,'Épargne',NULL,'savings'),
          (5,'Loisirs',NULL,'expense');
        """
    )
    return c


_n = 0


def tx(conn, acc, day, cents, cat, label="x", mirror=None):
    global _n
    _n += 1
    cur = conn.execute(
        "INSERT INTO transactions(account_id,tx_key,op_date,label,label_norm,amount_cents,"
        "category_id,mirror_of) VALUES (?,?,?,?,?,?,?,?)",
        (acc, f"k{_n}", day, label, label.lower(), cents, cat, mirror),
    )
    return cur.lastrowid


def seed(conn):
    tx(conn, 1, "2025-01-05", 150000, 1)
    tx(conn, 1, "2025-01-10", -10000, 3)
    tx(conn, 1, "2025-01-12", 3000, 3)
    transfer = tx(conn, 1, "2025-01-15", -20000, 4)
    tx(conn, 2, "2025-01-15", 20000, 4, mirror=transfer)  # miroir : ignoré
    tx(conn, 2, "2025-01-31", 500, 4)


def test_totals_all_accounts(conn):
    seed(conn)
    t = totals(conn, P, [1, 2])
    assert (t.expenses, t.income, t.savings_net, t.balance) == (
        Decimal("70"), Decimal("1500"), Decimal("205"), Decimal("1225"))


def test_totals_default_view_and_unclassified(conn):
    seed(conn)
    tx(conn, 1, "2025-01-20", -1000, None)
    tx(conn, 1, "2025-01-21", 400, None)
    t = totals(conn, P, None)
    assert t.savings_net == Decimal("200")
    assert t.unclassified_debits == Decimal("10")
    assert t.unclassified_credits == Decimal("4")
    assert t.expenses == Decimal("80") and t.income == Decimal("1504")


def test_empty_period_and_empty_accounts(conn):
    seed(conn)
    z = Decimal("0")
    t = totals(conn, Period(date(2024, 1, 1), date(2024, 1, 31)), None)
    assert (t.expenses, t.income, t.savings_net, t.balance) == (z, z, z, z)
    assert totals(conn, P, []).income == z
    assert by_category(conn, P, [], "expense", "parent") == []


def test_previous_period():
    assert previous_period(Period(date(2025, 1, 1), date(2025, 3, 31))) == Period(
        date(2024, 10, 3), date(2024, 12, 31))


def test_by_category_levels(conn):
    seed(conn)
    tx(conn, 1, "2025-01-22", -500, 5)
    assert by_category(conn, P, None, "expense", "parent") == [
        (2, "Alimentation", Decimal("70.00")), (5, "Loisirs", Decimal("5.00"))]
    assert by_category(conn, P, None, "expense", "child", parent_id=2) == [
        (3, "Courses", Decimal("70.00"))]
    assert by_category(conn, P, [1, 2], "savings", "parent") == [(4, "Épargne", Decimal("205.00"))]


def test_monthly_series_includes_empty_month(conn):
    tx(conn, 1, "2025-01-20", -1000, 5)
    tx(conn, 1, "2025-03-05", -2000, 5)
    s = monthly_series(conn, Period(date(2025, 1, 15), date(2025, 3, 10)), None)
    assert [m["month"] for m in s] == ["2025-01", "2025-02", "2025-03"]
    assert s[1]["expenses"] == Decimal("0") and s[1]["by_category"] == {}
    assert s[2]["by_category"] == {"Loisirs": Decimal("20.00")}


def test_top_increases(conn):
    tx(conn, 1, "2024-12-10", -1000, 5)
    tx(conn, 1, "2025-01-10", -3000, 5)
    tx(conn, 1, "2025-01-11", -500, 3)
    ref = Period(date(2024, 12, 1), date(2024, 12, 31))
    assert top_increases(conn, P, ref, None) == [
        ("Loisirs", Decimal("20.00")), ("Alimentation", Decimal("5.00"))]


def test_recurring(conn):
    for m in ("01", "02", "03"):
        tx(conn, 1, f"2025-{m}-05", -599, 5, label="Streamix")
    for m in ("01", "02"):
        tx(conn, 1, f"2025-{m}-07", -1500, 5, label="Gym")
    tx(conn, 1, "2025-01-09", -100, 5, label="Var")
    tx(conn, 1, "2025-02-09", -900, 5, label="Var")
    tx(conn, 1, "2025-03-09", -100, 5, label="Var")
    r = recurring(conn, Period(date(2025, 1, 1), date(2025, 3, 31)), None)
    assert r == [{"label_norm": "streamix", "avg_amount": Decimal("5.99"),
                  "months": ["2025-01", "2025-02", "2025-03"]}]


def test_child_drilldown_adds_up_to_parent(conn):
    seed(conn)
    tx(conn, 1, "2025-01-25", -3000, 2)  # directement sur la racine
    parent = by_category(conn, P, None, "expense", "parent")
    assert parent == [(2, "Alimentation", Decimal("100.00"))]
    kids = by_category(conn, P, None, "expense", "child", parent_id=2)
    assert kids == [
        (3, "Courses", Decimal("70.00")),
        (2, "Alimentation (sans sous-catégorie)", Decimal("30.00")),
    ]
    assert sum(k[2] for k in kids) == parent[0][2]


def test_by_category_unclassified_and_income(conn):
    seed(conn)
    tx(conn, 1, "2025-01-20", -1000, None)
    tx(conn, 1, "2025-01-21", 400, None)
    assert (None, "À classer", Decimal("10.00")) in by_category(conn, P, None, "expense", "parent")
    assert by_category(conn, P, None, "income", "parent") == [
        (1, "Salaire", Decimal("1500.00")), (None, "À classer", Decimal("4.00"))]


def test_start_bound_excluded_with_month_clipping(conn):
    tx(conn, 1, "2025-01-10", -1000, 5)
    tx(conn, 1, "2025-01-15", -2000, 5)
    s = monthly_series(conn, Period(date(2025, 1, 15), date(2025, 1, 31)), None)
    assert len(s) == 1 and s[0]["expenses"] == Decimal("20.00")


def test_top_increases_ignores_unclassified(conn):
    tx(conn, 1, "2025-01-10", -3000, None)
    ref = Period(date(2024, 12, 1), date(2024, 12, 31))
    assert top_increases(conn, P, ref, None) == []


def test_recurring_prefers_most_recent_price(conn):
    for m in ("01", "02", "03"):
        tx(conn, 1, f"2025-{m}-05", -999, 5, label="Streamix")
    for m in ("04", "05", "06"):
        tx(conn, 1, f"2025-{m}-05", -1399, 5, label="Streamix")
    r = recurring(conn, Period(date(2025, 1, 1), date(2025, 6, 30)), None)
    assert r == [{"label_norm": "streamix", "avg_amount": Decimal("13.99"),
                  "months": ["2025-04", "2025-05", "2025-06"]}]


def test_recurring_sums_month_gap_and_exclusions(conn):
    first = tx(conn, 1, "2025-01-01", -100, 5, label="Seed")
    # deux opérations dans un même mois : sommées
    for m in ("01", "02", "03"):
        tx(conn, 1, f"2025-{m}-05", -500, 5, label="Cantine")
        tx(conn, 1, f"2025-{m}-20", -500, 5, label="Cantine")
    # un trou casse la série
    for m in ("01", "02", "04", "05"):
        tx(conn, 1, f"2025-{m}-08", -700, 5, label="Trou")
    # miroirs et épargne exclus
    for m in ("01", "02", "03"):
        tx(conn, 1, f"2025-{m}-09", -800, 5, label="Miroir", mirror=first)
        tx(conn, 1, f"2025-{m}-10", -900, 4, label="Epargne")
    r = recurring(conn, Period(date(2025, 1, 1), date(2025, 5, 31)), None)
    assert r == [{"label_norm": "cantine", "avg_amount": Decimal("10.00"),
                  "months": ["2025-01", "2025-02", "2025-03"]}]
