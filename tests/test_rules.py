from pathlib import Path

import pytest

from app.classify.rules import (apply_rules, create_rule, default_priority, delete_rule,
                                set_manual_category, update_rule)
from app.classify.seed import load_seed
from app.importer.creditmutuel import CreditMutuelParser
from app.importer.service import import_pdf
from app.store.db import connect

SEED = Path(__file__).resolve().parent.parent / "app" / "classify" / "rules.seed.toml"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def pdf_dir(tmp_path):
    return tmp_path / "pdfs"


@pytest.fixture
def cats(conn):
    ids = {}
    for name, kind in [("Courses", "expense"), ("Restauration", "expense"), ("Salaire", "income"),
                       ("Divers", "expense")]:
        ids[name] = conn.execute("INSERT INTO categories(name, kind) VALUES (?, ?)", (name, kind)).lastrowid
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'CC', 'checking')")
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (2, '2', 'LIV', 'savings')")
    return ids


def add_tx(conn, label_norm, cents, account_id=1, manual=0, category_id=None):
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return conn.execute(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, manual, "
        "category_id) VALUES (?, ?, '2024-03-11', ?, ?, ?, ?, ?)",
        (account_id, f"k{n}", label_norm, label_norm, cents, manual, category_id)).lastrowid


def cat_of(conn, tx_id):
    return conn.execute("SELECT category_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]


def test_default_priority():
    assert default_priority("ABC", "contains", None, None) == 30
    assert default_priority("ABC", "startswith", "debit", 1) == 30 + 5 + 5 + 3
    assert default_priority("ABC", "regex", "credit", None) == 35


def test_longer_pattern_wins(conn, cats):
    t = add_tx(conn, "PAIEMENT CB MAGASIN ALPHA", -250)
    create_rule(conn, "MAGASIN", "contains", cats["Divers"])
    create_rule(conn, "MAGASIN ALPHA", "contains", cats["Courses"])
    assert apply_rules(conn) == 1
    assert cat_of(conn, t) == cats["Courses"]


def test_tie_oldest_wins(conn, cats):
    t = add_tx(conn, "ABC", -100)
    create_rule(conn, "ABC", "contains", cats["Divers"])
    create_rule(conn, "ABC", "contains", cats["Courses"], apply_to="all_non_manual")
    assert cat_of(conn, t) == cats["Divers"]


def test_direction_respected(conn, cats):
    debit, credit = add_tx(conn, "VIR ALPHA", -1000), add_tx(conn, "VIR ALPHA", 1000)
    create_rule(conn, "VIR ALPHA", "contains", cats["Salaire"], direction="credit")
    assert cat_of(conn, debit) is None
    assert cat_of(conn, credit) == cats["Salaire"]


def test_refund_without_direction_goes_to_expense_category(conn, cats):
    refund = add_tx(conn, "VIR REMBOURSEMENT MAGASIN ALPHA", 1500)
    create_rule(conn, "MAGASIN ALPHA", "contains", cats["Courses"])
    assert cat_of(conn, refund) == cats["Courses"]


def test_account_startswith_and_regex(conn, cats):
    a, b = add_tx(conn, "PRLV BETA", -100, account_id=1), add_tx(conn, "PRLV BETA", -100, account_id=2)
    create_rule(conn, "PRLV", "startswith", cats["Divers"], account_id=2)
    assert cat_of(conn, a) is None and cat_of(conn, b) == cats["Divers"]
    c = add_tx(conn, "ACHAT X 12", -5)
    create_rule(conn, r"X \d+$", "regex", cats["Courses"])
    assert cat_of(conn, c) == cats["Courses"]


def test_invalid_regex_rejected(conn, cats):
    with pytest.raises(ValueError):
        create_rule(conn, "(", "regex", cats["Divers"])
    rid = create_rule(conn, "A", "contains", cats["Divers"])
    with pytest.raises(ValueError):
        update_rule(conn, rid, pattern="(", match="regex")


def test_manual_never_touched(conn, cats):
    t = add_tx(conn, "MAGASIN ALPHA", -100, manual=1, category_id=cats["Divers"])
    create_rule(conn, "MAGASIN", "contains", cats["Courses"], apply_to="all_non_manual")
    apply_rules(conn)
    apply_rules(conn, [t])
    assert cat_of(conn, t) == cats["Divers"]


def test_no_rule_stays_null(conn, cats):
    t = add_tx(conn, "INCONNU", -100)
    create_rule(conn, "MAGASIN", "contains", cats["Courses"])
    assert apply_rules(conn) == 0
    assert cat_of(conn, t) is None


def test_create_rule_unclassified_vs_all(conn, cats):
    t = add_tx(conn, "MAGASIN ALPHA", -100)
    create_rule(conn, "MAGASIN", "contains", cats["Divers"])
    assert cat_of(conn, t) == cats["Divers"]
    create_rule(conn, "MAGASIN ALPHA", "contains", cats["Courses"])          # unclassified only
    assert cat_of(conn, t) == cats["Divers"]
    create_rule(conn, "MAGASIN ALPHA", "contains", cats["Restauration"], priority=1000,
                apply_to="all_non_manual")
    assert cat_of(conn, t) == cats["Restauration"]


def test_update_rule_reclassifies(conn, cats):
    t = add_tx(conn, "MAGASIN ALPHA", -100)
    rid = create_rule(conn, "MAGASIN", "contains", cats["Divers"])
    update_rule(conn, rid, category_id=cats["Courses"])
    assert cat_of(conn, t) == cats["Courses"]
    update_rule(conn, rid, pattern="AUTRE")
    assert cat_of(conn, t) is None


def test_update_rule_unknown_field(conn, cats):
    rid = create_rule(conn, "A", "contains", cats["Divers"])
    with pytest.raises(ValueError):
        update_rule(conn, rid, bogus=1)


def test_delete_rule(conn, cats):
    t1, t2 = add_tx(conn, "MAGASIN ALPHA", -100), add_tx(conn, "MAGASIN BETA", -100)
    shared = create_rule(conn, "MAGASIN", "contains", cats["Divers"])
    specific = create_rule(conn, "MAGASIN ALPHA", "contains", cats["Courses"], apply_to="all_non_manual")
    assert cat_of(conn, t1) == cats["Courses"]
    delete_rule(conn, specific)
    assert cat_of(conn, t1) == cats["Divers"]       # retombe sur l'autre règle
    delete_rule(conn, shared)
    assert cat_of(conn, t1) is None and cat_of(conn, t2) is None
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0


def test_rules_do_not_commit(conn, cats):
    conn.commit()
    add_tx(conn, "MAGASIN", -1)
    create_rule(conn, "MAGASIN", "contains", cats["Divers"])
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0


def test_set_manual_category(conn, cats):
    t = add_tx(conn, "MAGASIN ALPHA", -100)
    create_rule(conn, "MAGASIN", "contains", cats["Divers"])
    set_manual_category(conn, t, cats["Courses"])
    assert conn.execute("SELECT category_id, manual, rule_id FROM transactions WHERE id=?", (t,)).fetchone() \
        == (cats["Courses"], 1, None)
    assert conn.execute("SELECT category_id FROM manual_overrides").fetchall() == [(cats["Courses"],)]
    set_manual_category(conn, t, cats["Restauration"])       # mise à jour
    assert conn.execute("SELECT category_id FROM manual_overrides").fetchall() == [(cats["Restauration"],)]
    apply_rules(conn)
    assert cat_of(conn, t) == cats["Restauration"]
    set_manual_category(conn, t, None)
    assert conn.execute("SELECT category_id, manual FROM transactions WHERE id=?", (t,)).fetchone() \
        == (cats["Divers"], 0)
    assert conn.execute("SELECT COUNT(*) FROM manual_overrides").fetchone()[0] == 0


def _counts(conn):
    return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("categories", "rules")]


def test_load_seed_idempotent(conn):
    load_seed(conn, SEED)
    counts = _counts(conn)
    load_seed(conn, SEED)
    assert _counts(conn) == counts
    assert counts[0] > 25 and counts[1] >= 2
    kinds = dict(conn.execute("SELECT name, kind FROM categories").fetchall())
    assert kinds["Salaire"] == "income" and kinds["CEL"] == "savings" and kinds["Carburant"] == "expense"
    assert conn.execute("SELECT parent_id FROM categories WHERE name='Courses'").fetchone()[0] is None
    assert conn.execute("SELECT p.name FROM categories c JOIN categories p ON p.id=c.parent_id "
                        "WHERE c.name='Intérêts'").fetchone()[0] == "Épargne"


def test_seed_rules_classify_generic_labels(conn):
    load_seed(conn, SEED)
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'CC', 'checking')")
    t1, t2 = add_tx(conn, "INTERETS CREDITEURS", 100), add_tx(conn, "F COTIS EUROCOMPTE", -300)
    apply_rules(conn)
    names = dict(conn.execute("SELECT id, name FROM categories").fetchall())
    assert names[cat_of(conn, t1)] == "Intérêts" and names[cat_of(conn, t2)] == "Frais bancaires"


def test_seed_interest_rule_only_matches_credits(conn):
    load_seed(conn, SEED)
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'CC', 'checking')")
    overdraft, earned = add_tx(conn, "INTERETS DEBITEURS", -420), add_tx(conn, "INTERETS CREDITEURS", 150)
    apply_rules(conn)
    names = dict(conn.execute("SELECT id, name FROM categories").fetchall())
    assert cat_of(conn, overdraft) is None
    assert names[cat_of(conn, earned)] == "Intérêts"


def test_load_seed_without_rules_creates_only_categories(conn):
    load_seed(conn, SEED, with_rules=False)
    categories, rules = _counts(conn)
    assert categories > 25 and rules == 0


def test_load_seed_commits_when_not_in_transaction(tmp_path):
    c = connect(tmp_path / "s.db")
    load_seed(c, SEED)
    assert not c.in_transaction
    c.close()
    c = connect(tmp_path / "s.db")
    assert c.execute("SELECT COUNT(*) FROM categories").fetchone()[0] > 0
    c.close()


def test_apply_rules_as_import_callback(conn, tmp_path, pdf_dir):
    from test_import_service import _cc_section, _livret_section, _pdf
    cat = conn.execute("INSERT INTO categories(name, kind) VALUES ('Divers', 'expense')").lastrowid
    create_rule(conn, "PRLV SEPA BETA", "contains", cat)
    conn.commit()
    report = import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
                        [CreditMutuelParser()], apply_rules)
    assert report.error is None
    rows = dict(conn.execute("SELECT label_norm, category_id FROM transactions").fetchall())
    assert rows["PRLV SEPA BETA"] == cat
    assert rows["VIR DE M TEST"] is None
