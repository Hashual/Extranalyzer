import pytest

from app.classify.pipeline import on_new_transactions
from app.classify.rules import apply_rules, create_rule, set_manual_category, sync_mirrors
from app.classify.transfers import classify_paired_sources, pair_internal_transfers
from app.classify.savings_target import savings_category_for_account
from app.store.db import connect


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'CC', 'checking')")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (2, '2', 'LIV', 'savings')")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (3, '3', 'CC2', 'checking')")
    c.execute("INSERT INTO categories(id, name, kind) VALUES (1, 'Epargne', 'savings')")
    c.execute("INSERT INTO categories(id, name, kind) VALUES (2, 'Divers', 'expense')")
    yield c
    c.close()


def add_tx(conn, account_id, op_date, cents, label="VIREMENT"):
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return conn.execute(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)", (account_id, f"k{n}", op_date, label, label, cents)).lastrowid


def row(conn, tx_id):
    return conn.execute("SELECT mirror_of, category_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()


def test_basic_pair(conn):
    out = add_tx(conn, 1, "2024-03-12", -10000)
    inn = add_tx(conn, 2, "2024-03-13", 10000)
    conn.execute("UPDATE transactions SET category_id = 1 WHERE id = ?", (out,))
    assert pair_internal_transfers(conn) == 1
    assert row(conn, inn) == (out, 1)
    assert row(conn, out)[0] is None
    assert pair_internal_transfers(conn) == 0          # jamais réapparié


def test_pair_savings_to_checking(conn):
    out = add_tx(conn, 2, "2024-03-12", -5000)
    inn = add_tx(conn, 1, "2024-03-12", 5000)
    assert pair_internal_transfers(conn) == 1
    assert row(conn, out)[0] == inn and row(conn, inn)[0] is None


def test_two_identical_transfers_same_day(conn):
    o1, o2 = add_tx(conn, 1, "2024-03-12", -100), add_tx(conn, 1, "2024-03-12", -100)
    i1, i2 = add_tx(conn, 2, "2024-03-12", 100), add_tx(conn, 2, "2024-03-12", 100)
    assert pair_internal_transfers(conn) == 2
    assert row(conn, i1)[0] == o1 and row(conn, i2)[0] == o2


def test_closest_date_then_smallest_id(conn):
    far = add_tx(conn, 1, "2024-03-13", -100)
    near = add_tx(conn, 1, "2024-03-10", -100)
    inn = add_tx(conn, 2, "2024-03-08", 100)
    assert pair_internal_transfers(conn) == 1
    assert row(conn, inn)[0] == near and row(conn, far)[0] is None


def test_equal_gap_takes_smallest_id(conn):
    first = add_tx(conn, 1, "2024-03-11", -100)
    second = add_tx(conn, 1, "2024-03-13", -100)
    inn = add_tx(conn, 2, "2024-03-12", 100)
    assert pair_internal_transfers(conn) == 1
    assert row(conn, inn)[0] == first and row(conn, second)[0] is None


def test_gap_limits(conn):
    add_tx(conn, 1, "2024-03-01", -100)
    add_tx(conn, 2, "2024-03-09", 100)
    add_tx(conn, 1, "2024-04-01", -300)
    add_tx(conn, 2, "2024-04-07", 300)                 # 6 jours
    assert pair_internal_transfers(conn) == 0
    ok = add_tx(conn, 1, "2024-03-20", -200)
    inn = add_tx(conn, 2, "2024-03-25", 200)
    assert pair_internal_transfers(conn) == 1
    assert row(conn, inn)[0] == ok


def test_two_checking_accounts_not_paired(conn):
    add_tx(conn, 1, "2024-03-12", -100)
    add_tx(conn, 3, "2024-03-12", 100)
    assert pair_internal_transfers(conn) == 0


def test_same_account_not_paired(conn):
    add_tx(conn, 2, "2024-03-12", -100)
    add_tx(conn, 2, "2024-03-12", 100)
    assert pair_internal_transfers(conn) == 0


def test_non_ok_section_excluded(conn):
    conn.execute("INSERT INTO documents(id, sha256, stored_path, parser, imported_at) "
                 "VALUES (1, 'x', 'p', 'cm', 'now')")
    conn.execute("INSERT INTO sections(id, document_id, account_id, statement_date, gap_cents, status, "
                 "content_hash) VALUES (1, 1, 2, '2024-03-31', 0, 'superseded', 'h')")
    add_tx(conn, 1, "2024-03-12", -100)
    inn = add_tx(conn, 2, "2024-03-12", 100)
    conn.execute("UPDATE transactions SET section_id = 1 WHERE id = ?", (inn,))
    assert pair_internal_transfers(conn) == 0


def test_apply_rules_skips_mirror_and_resyncs(conn):
    out = add_tx(conn, 1, "2024-03-12", -100, "VIR LIVRET")
    inn = add_tx(conn, 2, "2024-03-12", 100, "VIR LIVRET")
    conn.execute("UPDATE transactions SET mirror_of = ? WHERE id = ?", (out, inn))
    create_rule(conn, "VIR", "contains", 1, direction="debit")      # ne vise que la source
    assert row(conn, out)[1] == 1 and row(conn, inn)[1] == 1
    create_rule(conn, "VIR LIVRET", "contains", 2, direction="credit")  # viserait le miroir
    apply_rules(conn)
    assert row(conn, inn)[1] == 1                                    # le miroir suit sa source
    conn.execute("UPDATE transactions SET category_id = NULL WHERE id = ?", (out,))
    apply_rules(conn, [out])
    assert row(conn, inn)[1] == 1


def test_apply_rules_resync_follows_source_change(conn):
    out = add_tx(conn, 1, "2024-03-12", -100, "VIR LIVRET")
    inn = add_tx(conn, 2, "2024-03-12", 100, "AUTRE")
    conn.execute("UPDATE transactions SET mirror_of = ?, category_id = 1 WHERE id = ?", (out, inn))
    create_rule(conn, "VIR", "contains", 2, apply_to="all_non_manual")
    assert row(conn, out)[1] == 2 and row(conn, inn)[1] == 2


def test_set_manual_category_resyncs_mirror(conn):
    out = add_tx(conn, 1, "2024-03-12", -100)
    inn = add_tx(conn, 2, "2024-03-12", 100)
    conn.execute("UPDATE transactions SET mirror_of = ?, category_id = 1 WHERE id = ?", (out, inn))
    set_manual_category(conn, out, 2)
    assert row(conn, inn)[1] == 2
    set_manual_category(conn, out, None)
    assert row(conn, inn)[1] is None


def test_pipeline(conn):
    create_rule(conn, "VIR", "contains", 1, direction="debit")
    out = add_tx(conn, 1, "2024-03-12", -100, "VIR LIVRET")
    inn = add_tx(conn, 2, "2024-03-13", 100, "VIR RECU")
    on_new_transactions(conn, [out, inn])
    assert row(conn, inn) == (out, 1) and row(conn, out) == (None, 1)


def test_apply_rules_never_touches_mirror_directly(conn):
    out = add_tx(conn, 1, "2024-03-12", -100, "AAA")
    inn = add_tx(conn, 2, "2024-03-12", 100, "ZZZ")
    conn.execute("UPDATE transactions SET mirror_of = ?, category_id = 1 WHERE id = ?", (out, inn))
    conn.execute("UPDATE transactions SET category_id = 1 WHERE id = ?", (out,))
    conn.execute("INSERT INTO rules(pattern, match, category_id, priority) VALUES ('ZZZ', 'contains', 2, 10)")
    apply_rules(conn, [inn])
    assert row(conn, inn)[1] == 1


def test_manual_category_on_mirror_redirects_to_source(conn):
    out = add_tx(conn, 1, "2024-03-12", -100)
    inn = add_tx(conn, 2, "2024-03-12", 100)
    conn.execute("UPDATE transactions SET mirror_of = ?, category_id = 1 WHERE id = ?", (out, inn))
    set_manual_category(conn, inn, 2)
    flags = lambda i: conn.execute("SELECT manual, category_id FROM transactions WHERE id = ?", (i,)).fetchone()
    assert flags(out) == (1, 2) and flags(inn) == (0, 2)
    assert conn.execute("SELECT COUNT(*) FROM manual_overrides").fetchone()[0] == 1
    assert conn.execute("SELECT tx_key FROM manual_overrides").fetchone()[0] ==         conn.execute("SELECT tx_key FROM transactions WHERE id = ?", (out,)).fetchone()[0]
    set_manual_category(conn, inn, None)
    assert flags(out) == (0, None) and flags(inn) == (0, None)
    assert conn.execute("SELECT COUNT(*) FROM manual_overrides").fetchone()[0] == 0


# --- classement des sources appariées en Épargne / livret ---

def _savings_setup(conn, label="LIVRET BLEU", children=("Livret Bleu", "CEL")):
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (10, '10', ?, 'savings')", (label,))
    for name in children:
        conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, 1, 'savings')", (name,))
    out = add_tx(conn, 1, "2024-03-12", -100)
    inn = add_tx(conn, 10, "2024-03-12", 100)
    assert pair_internal_transfers(conn) == 1
    return out, inn


def _cat_name(conn, tx_id):
    return conn.execute("SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id "
                        "WHERE t.id = ?", (tx_id,)).fetchone()[0]


def _by(conn, tx_id):
    return conn.execute("SELECT classified_by FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]


def test_paired_source_gets_livret_category(conn):
    out, inn = _savings_setup(conn)
    assert classify_paired_sources(conn) == 1
    assert _cat_name(conn, out) == "Livret Bleu" and _by(conn, out) == "pair"
    sync_mirrors(conn)
    assert _cat_name(conn, inn) == "Livret Bleu" and _by(conn, inn) == "pair"
    assert classify_paired_sources(conn) == 0


def test_cel_alias(conn):
    out, _ = _savings_setup(conn, label="Épargne Logement")
    classify_paired_sources(conn)
    assert _cat_name(conn, out) == "CEL"


def test_unknown_savings_account_creates_subcategory_once(conn):
    _savings_setup(conn, label="LIVRET A", children=())
    first = savings_category_for_account(conn, 10)
    assert savings_category_for_account(conn, 10) == first
    rows = conn.execute("SELECT name, parent_id, kind FROM categories WHERE name = 'Livret A'").fetchall()
    assert rows == [("Livret A", 1, "savings")]
    classify_paired_sources(conn)
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE name = 'Livret A'").fetchone()[0] == 1


def test_root_created_when_missing(tmp_path):
    c = connect(tmp_path / "r.db")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'LIVRET JEUNE', 'savings')")
    cat = savings_category_for_account(c, 1)
    assert c.execute("SELECT c.name, p.name, p.kind FROM categories c JOIN categories p ON p.id = c.parent_id "
                     "WHERE c.id = ?", (cat,)).fetchone() == ("Livret Jeune", "Épargne", "savings")
    c.close()


def test_rule_category_blocks_pair_classification(conn):
    out, inn = _savings_setup(conn)
    create_rule(conn, "VIREMENT", "contains", 2, direction="debit")
    assert classify_paired_sources(conn) == 0
    assert row(conn, out)[1] == 2 and _by(conn, out) == "rule"


def test_manual_source_untouched(conn):
    out, inn = _savings_setup(conn)
    set_manual_category(conn, out, 2)
    assert classify_paired_sources(conn) == 0
    assert row(conn, out)[1] == 2 and _by(conn, out) == "manual"


def test_pair_sets_classified_by_on_mirror(conn):
    out = add_tx(conn, 1, "2024-03-12", -100)
    inn = add_tx(conn, 2, "2024-03-12", 100)
    conn.execute("UPDATE transactions SET category_id = 1, classified_by = 'rule' WHERE id = ?", (out,))
    pair_internal_transfers(conn)
    assert _by(conn, inn) == "pair"
    out2 = add_tx(conn, 1, "2024-04-12", -200)
    inn2 = add_tx(conn, 2, "2024-04-12", 200)
    pair_internal_transfers(conn)
    assert row(conn, inn2) == (out2, None) and _by(conn, inn2) is None


def test_sync_mirrors_clears_model_fields(conn):
    out = add_tx(conn, 1, "2024-03-12", -100)
    inn = add_tx(conn, 2, "2024-03-12", 100)
    conn.execute("UPDATE transactions SET mirror_of = ?, confidence = 0.9, suggested_category_id = 2, "
                 "suggestion_reason = 'x' WHERE id = ?", (out, inn))
    sync_mirrors(conn)
    assert conn.execute("SELECT confidence, suggested_category_id, suggestion_reason FROM transactions "
                        "WHERE id = ?", (inn,)).fetchone() == (None, None, None)
