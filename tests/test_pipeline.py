"""Chaîne de classement unique : règles → appariement → modèle → miroirs (données fictives)."""
import time
from pathlib import Path

import pytest

from app.analytics.totals import unclassified_count
from app.classify.pipeline import on_new_transactions, reclassify
from app.classify.rules import create_rule, delete_rule, set_manual_category, update_rule
from app.classify.seed import load_seed
from app.config import ConfigError, load_settings
from app.importer.creditmutuel import CreditMutuelParser
from app.importer.service import import_pdf
from app.store.db import connect

SEED = Path(__file__).resolve().parent.parent / "app" / "classify" / "rules.seed.toml"
DUPONT = "CB BOULANGERIE DUPONT"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv("EXTRANALYZER_AUTO_THRESHOLD", raising=False)
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'CC', 'checking')")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (2, '2', 'LIVRET BLEU', 'savings')")
    yield c
    c.close()


@pytest.fixture
def cats(conn):
    ids = {}
    for name, kind in [("Courses", "expense"), ("Restauration", "expense"), ("Divers", "expense"),
                       ("Santé", "expense"), ("Épargne", "savings")]:
        ids[name] = conn.execute("INSERT INTO categories(name, kind) VALUES (?, ?)", (name, kind)).lastrowid
    ids["Livret Bleu"] = conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES "
                                      "('Livret Bleu', ?, 'savings')", (ids["Épargne"],)).lastrowid
    return ids


def add_tx(conn, label, cents, account_id=1, op_date="2024-03-11"):
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return conn.execute(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)", (account_id, f"k{n}", op_date, label, label, cents)).lastrowid


def manual(conn, label, cents, category_id):
    tx = add_tx(conn, label, cents)
    set_manual_category(conn, tx, category_id, reclassify_after=False)
    return tx


def state(conn, tx_id):
    """(category_id, classified_by, suggested_category_id, confidence, suggestion_reason)."""
    return conn.execute("SELECT category_id, classified_by, suggested_category_id, confidence, suggestion_reason "
                        "FROM transactions WHERE id = ?", (tx_id,)).fetchone()


def pair(conn, label="VIR LIVRET", cents=-10000, op_date="2024-03-12"):
    """Virement courant → livret ; renvoie (source, miroir) après le passage de la chaîne."""
    src = add_tx(conn, label, cents, 1, op_date)
    mirror = add_tx(conn, "VERSEMENT", -cents, 2, op_date)
    return src, mirror


def test_reclassify_order_and_counts(conn, cats):
    rule_row = add_tx(conn, "PRLV EDF FACTURE", -5000)
    ruled_src, ruled_mirror = pair(conn, "VIR VERS LIVRET JEUNE", -20000, "2024-03-20")
    pair_src, pair_mirror = pair(conn)
    examples = [manual(conn, DUPONT, -350, cats["Courses"]) for _ in range(3)]
    manual(conn, "CB PHARMACIE CENTRALE", -1200, cats["Santé"])
    auto = add_tx(conn, DUPONT, -420)
    suggested = add_tx(conn, "CB PHARMACIE CENTRALE", -900)
    lonely = add_tx(conn, "XYZZY QWERTY", 1234)
    conn.execute("INSERT INTO rules(pattern, match, category_id, priority) VALUES ('PRLV EDF', 'contains', ?, 80)",
                 (cats["Divers"],))
    conn.execute("INSERT INTO rules(pattern, match, category_id, priority) VALUES ('LIVRET JEUNE', 'contains', ?, "
                 "120)", (cats["Divers"],))
    # état dérivé périmé : doit être effacé sur les lignes manual / rule / pair
    conn.execute("UPDATE transactions SET confidence = 0.5, suggested_category_id = ?, suggestion_reason = 'x'",
                 (cats["Restauration"],))

    counts = reclassify(conn)

    assert counts == {"rule": 2, "pair": 1, "model": 1, "suggested": 1, "unclassified": 1}
    assert state(conn, rule_row) == (cats["Divers"], "rule", None, None, None)
    assert state(conn, ruled_src) == (cats["Divers"], "rule", None, None, None)       # règle > appariement
    assert state(conn, ruled_mirror) == (cats["Divers"], "pair", None, None, None)
    assert state(conn, pair_src) == (cats["Livret Bleu"], "pair", None, None, None)
    assert state(conn, pair_mirror) == (cats["Livret Bleu"], "pair", None, None, None)
    for ex in examples:
        assert state(conn, ex) == (cats["Courses"], "manual", None, None, None)
    cat, by, sugg, conf, reason = state(conn, auto)
    assert (cat, by, sugg) == (cats["Courses"], "model", cats["Courses"]) and conf >= 0.9 and reason
    cat, by, sugg, conf, reason = state(conn, suggested)
    assert (cat, by, sugg) == (None, None, cats["Santé"]) and 0 < conf and reason       # 1 seul voisin
    assert state(conn, lonely) == (None, None, None, None, "aucune opération similaire")
    assert conn.execute("SELECT rule_id FROM transactions WHERE id = ?", (auto,)).fetchone()[0] is None


def test_model_never_learns_from_model_rows(conn, cats):
    examples = [manual(conn, DUPONT, -350, cats["Courses"]) for _ in range(3)]
    targets = [add_tx(conn, DUPONT, -400) for _ in range(3)]
    assert reclassify(conn)["model"] == 3
    for ex in examples:
        set_manual_category(conn, ex, None, reclassify_after=False)
    counts = reclassify(conn)
    assert counts["model"] == 0 and counts["suggested"] == 0 and counts["unclassified"] == 6
    for tx in examples + targets:
        assert state(conn, tx)[:4] == (None, None, None, None)


def test_model_row_downgraded_when_contradicting_example_added(conn, cats):
    for _ in range(3):
        manual(conn, DUPONT, -350, cats["Courses"])
    target = add_tx(conn, DUPONT, -400)
    reclassify(conn)
    assert state(conn, target)[:2] == (cats["Courses"], "model")
    manual(conn, DUPONT, -350, cats["Restauration"])
    reclassify(conn)
    cat, by, sugg, conf, _ = state(conn, target)
    assert (cat, by, sugg) == (None, None, cats["Courses"])
    assert conf == pytest.approx(0.75)


def test_delete_rule_falls_back_to_pair_then_model(conn, cats):
    src, mirror = pair(conn)
    for _ in range(3):
        manual(conn, DUPONT, -350, cats["Courses"])
    target = add_tx(conn, DUPONT, -400)
    on_rule = create_rule(conn, "VIR LIVRET", "contains", cats["Divers"])
    on_target = create_rule(conn, "BOULANGERIE", "contains", cats["Restauration"])
    assert state(conn, src)[:2] == (cats["Divers"], "rule")
    assert state(conn, mirror)[:2] == (cats["Divers"], "pair")
    assert state(conn, target)[:2] == (cats["Restauration"], "rule")          # règle > modèle

    delete_rule(conn, on_rule)
    assert state(conn, src)[:2] == (cats["Livret Bleu"], "pair")
    assert state(conn, mirror)[:2] == (cats["Livret Bleu"], "pair")
    assert state(conn, target)[:2] == (cats["Restauration"], "rule")

    delete_rule(conn, on_target)
    assert state(conn, target)[:3] == (cats["Courses"], "model", cats["Courses"])
    assert conn.execute("SELECT rule_id FROM transactions WHERE id = ?", (target,)).fetchone()[0] is None


def test_update_rule_runs_full_chain(conn, cats):
    for _ in range(3):
        manual(conn, DUPONT, -350, cats["Courses"])
    target = add_tx(conn, DUPONT, -400)
    rid = create_rule(conn, "BOULANGERIE", "contains", cats["Restauration"])
    assert state(conn, target)[:2] == (cats["Restauration"], "rule")
    update_rule(conn, rid, pattern="AUTRE CHOSE")
    assert state(conn, target)[:2] == (cats["Courses"], "model")


def test_clearing_manual_on_paired_source_returns_to_pair(conn, cats):
    src, mirror = pair(conn)
    reclassify(conn)
    assert state(conn, src)[:2] == (cats["Livret Bleu"], "pair")
    set_manual_category(conn, src, cats["Courses"])
    assert state(conn, src)[:2] == (cats["Courses"], "manual")
    assert state(conn, mirror)[:2] == (cats["Courses"], "pair")
    set_manual_category(conn, src, None)
    assert state(conn, src)[:2] == (cats["Livret Bleu"], "pair")
    assert state(conn, mirror)[:2] == (cats["Livret Bleu"], "pair")
    set_manual_category(conn, mirror, cats["Divers"])         # via la jambe miroir : redirigé vers la source
    assert state(conn, src)[:2] == (cats["Divers"], "manual")
    set_manual_category(conn, mirror, None)
    assert state(conn, src)[:2] == (cats["Livret Bleu"], "pair")
    assert conn.execute("SELECT COUNT(*) FROM manual_overrides").fetchone()[0] == 0


def test_set_manual_category_reclassify_after(conn, cats):
    target = add_tx(conn, DUPONT, -400)
    examples = [add_tx(conn, DUPONT, -350) for _ in range(3)]
    for ex in examples:
        set_manual_category(conn, ex, cats["Courses"], reclassify_after=False)
    assert state(conn, target)[:2] == (None, None)                   # pas de passage
    set_manual_category(conn, examples[0], cats["Courses"])          # défaut : passage complet
    assert state(conn, target)[:2] == (cats["Courses"], "model")


def test_on_new_transactions_runs_model(conn, cats):
    for _ in range(3):
        manual(conn, DUPONT, -350, cats["Courses"])
    target = add_tx(conn, DUPONT, -400)
    on_new_transactions(conn, [target])
    assert state(conn, target)[:2] == (cats["Courses"], "model")


def _downgrade_setup(conn, cats):
    for _ in range(3):
        manual(conn, DUPONT, -350, cats["Courses"])
    manual(conn, DUPONT, -350, cats["Restauration"])
    return add_tx(conn, DUPONT, -400)                                # confiance 0,75


def test_threshold_from_env(conn, cats, monkeypatch):
    target = _downgrade_setup(conn, cats)
    reclassify(conn)
    assert state(conn, target)[:2] == (None, None)                  # défaut 0,90
    monkeypatch.setenv("EXTRANALYZER_AUTO_THRESHOLD", "0.7")
    reclassify(conn)
    assert state(conn, target)[:2] == (cats["Courses"], "model")
    reclassify(conn, threshold=0.8)                                  # l'argument l'emporte
    assert state(conn, target)[:2] == (None, None)
    monkeypatch.setenv("EXTRANALYZER_AUTO_THRESHOLD", "abc")
    with pytest.raises(ConfigError):
        reclassify(conn)


@pytest.mark.parametrize("raw", ["0", "-0.5", "1.5", "abc", "nan", ""])
def test_settings_rejects_invalid_threshold(monkeypatch, raw):
    monkeypatch.setenv("EXTRANALYZER_PASSWORD", "secret-de-test")
    monkeypatch.setenv("EXTRANALYZER_AUTO_THRESHOLD", raw)
    with pytest.raises(ConfigError):
        load_settings()


def test_settings_threshold(monkeypatch):
    monkeypatch.setenv("EXTRANALYZER_PASSWORD", "secret-de-test")
    monkeypatch.delenv("EXTRANALYZER_AUTO_THRESHOLD", raising=False)
    assert load_settings().auto_threshold == 0.90
    monkeypatch.setenv("EXTRANALYZER_AUTO_THRESHOLD", "1")
    assert load_settings().auto_threshold == 1.0


def test_reclassify_does_not_commit(conn, cats):
    conn.commit()
    for _ in range(3):
        manual(conn, DUPONT, -350, cats["Courses"])
    add_tx(conn, DUPONT, -400)
    reclassify(conn)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_savings_withdrawal_imported_is_classified_as_pair(conn, tmp_path):
    """Retrait livret → courant : le crédit « VIR DE M TEST » du compte courant est Épargne / Livret Bleu."""
    from test_import_service import _cc_section, _pdf, LIVRET
    from pdfgen import FSection, FTx
    load_seed(conn, SEED)
    conn.commit()
    livret = FSection(LIVRET, ("CREDITEUR", "11/03/2024", "500,00"), ("CREDITEUR", "10/04/2024", "400,00"),
                      [FTx("12/03/2024", "12/03/2024", ["VIR VERS COMPTE TEST"], debit="100,00")])
    data = _pdf(tmp_path, "w.pdf", [_cc_section(closing="187,50", vir="100,00"), livret])
    report = import_pdf(conn, data, "w.pdf", tmp_path / "pdfs", [CreditMutuelParser()], on_new_transactions)
    assert report.error is None
    src = conn.execute("SELECT t.id, t.amount_cents, t.classified_by, c.name, p.name FROM transactions t "
                       "JOIN categories c ON c.id = t.category_id JOIN categories p ON p.id = c.parent_id "
                       "WHERE t.label_norm = 'VIR DE M TEST'").fetchone()
    assert src[1:] == (10000, "pair", "Livret Bleu", "Épargne")
    mirror = conn.execute("SELECT amount_cents, classified_by, mirror_of FROM transactions "
                          "WHERE mirror_of IS NOT NULL").fetchone()
    assert mirror == (-10000, "pair", src[0])
    remaining = [r[0] for r in conn.execute(
        "SELECT label_norm FROM transactions WHERE category_id IS NULL AND mirror_of IS NULL")]
    assert "VIR DE M TEST" not in remaining
    assert unclassified_count(conn) == len(remaining)


def test_reclassify_is_fast_enough(conn, cats):
    words = ["ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON", "ZETA", "ETA", "THETA", "IOTA", "KAPPA", "LAMBDA"]
    rows = []
    for i in range(1100):
        label = f"CB {words[i % 11]} {words[(i * 7) % 11]} {i % 37}"
        rows.append((1, f"p{i}", "2024-03-11", label, label, -(100 + i), 1 if i % 3 == 0 else 0,
                     cats["Courses"] if i % 3 == 0 else None, "manual" if i % 3 == 0 else None))
    conn.executemany("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, "
                     "manual, category_id, classified_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    start = time.perf_counter()
    counts = reclassify(conn)
    elapsed = time.perf_counter() - start
    assert sum(counts.values()) == 1100 - 367
    assert elapsed < 5


def test_model_step_ignores_model_rows_as_examples(conn, cats):
    """Étape modèle seule (sans le passage de `apply_rules`) : des lignes `model` ne sont jamais des exemples."""
    from app.classify.pipeline import _apply_model
    for _ in range(3):
        tx = add_tx(conn, DUPONT, -350)
        conn.execute("UPDATE transactions SET category_id = ?, classified_by = 'model' WHERE id = ?",
                     (cats["Courses"], tx))
    target = add_tx(conn, DUPONT, -400)
    assert _apply_model(conn, 0.9) == (0, 0, 4)
    assert state(conn, target)[:4] == (None, None, None, None)


@pytest.mark.parametrize("bad", [0, -0.1, 1.01, float("nan")])
def test_explicit_threshold_validated(conn, bad):
    with pytest.raises(ValueError):
        reclassify(conn, threshold=bad)


def test_pair_rows_are_not_model_examples(conn, cats):
    """Virement à son nom depuis une banque inconnue : des sources appariées (Épargne) ne servent pas d'exemples,
    l'opération ressemblante non appariée reste sans suggestion."""
    sources = [pair(conn, "VIR DE M TEST", 10000, f"2024-0{m}-12")[0] for m in (3, 5, 7)]
    target = add_tx(conn, "VIR DE M TEST", 10000, 1, "2024-09-12")
    counts = reclassify(conn)
    for src in sources:
        assert state(conn, src)[:2] == (cats["Livret Bleu"], "pair")
    assert state(conn, target) == (None, None, None, None, "aucune opération similaire")
    assert support(conn, target) is None
    assert counts["model"] == 0 and counts["suggested"] == 0 and counts["unclassified"] == 1
    for _ in range(3):
        manual(conn, "VIR DE M TEST", 10000, cats["Livret Bleu"])     # seules les décisions apprennent
    reclassify(conn)
    assert state(conn, target)[:2] == (cats["Livret Bleu"], "model")


def support(conn, tx_id):
    return conn.execute("SELECT suggestion_support FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]


def test_suggestion_support_written_by_model_and_cleared(conn, cats):
    manual(conn, "CB PHARMACIE CENTRALE", -1200, cats["Santé"])
    target = add_tx(conn, "CB PHARMACIE CENTRALE", -900)
    lonely = add_tx(conn, "XYZZY QWERTY", 1234)
    rule_row = add_tx(conn, "PRLV EDF FACTURE", -5000)
    src, mirror = pair(conn)
    conn.execute("UPDATE transactions SET suggestion_support = 9")
    conn.execute("INSERT INTO rules(pattern, match, category_id, priority) VALUES ('PRLV EDF', 'contains', ?, 80)",
                 (cats["Divers"],))
    reclassify(conn)
    assert support(conn, target) == 1
    assert support(conn, lonely) is None
    for tx in (rule_row, src, mirror):
        assert support(conn, tx) is None
    set_manual_category(conn, target, cats["Santé"])
    assert support(conn, target) is None


def test_model_step_memoises_identical_inputs(conn, cats, monkeypatch):
    import app.classify.pipeline as pipeline
    calls = []
    real = pipeline.predict
    monkeypatch.setattr(pipeline, "predict", lambda *a: calls.append(a[1:]) or real(*a))
    manual(conn, DUPONT, -350, cats["Courses"])
    for cents in (-410, -420, -430, -4200):             # 3 du même ordre de grandeur, 1 d'un autre
        add_tx(conn, DUPONT, cents)
    add_tx(conn, DUPONT, -415, account_id=2)            # autre compte
    reclassify(conn)
    assert len(calls) == 3


def test_steady_state_pass_writes_nothing(conn, cats):
    rule_row = add_tx(conn, "PRLV EDF FACTURE", -5000)
    pair(conn)
    for _ in range(3):
        manual(conn, DUPONT, -350, cats["Courses"])
    add_tx(conn, DUPONT, -420)                          # auto
    manual(conn, "CB PHARMACIE CENTRALE", -1200, cats["Santé"])
    add_tx(conn, "CB PHARMACIE CENTRALE", -900)         # suggestion
    add_tx(conn, "XYZZY QWERTY", 1234)                  # sans suggestion
    conn.execute("INSERT INTO rules(pattern, match, category_id, priority) VALUES ('PRLV EDF', 'contains', ?, 80)",
                 (cats["Divers"],))
    first = reclassify(conn)
    snapshot = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
    conn.execute("CREATE TEMP TABLE written(id INTEGER)")
    conn.execute("CREATE TEMP TRIGGER log_update AFTER UPDATE ON transactions "
                 "BEGIN INSERT INTO written VALUES (new.id); END")
    assert reclassify(conn) == first
    assert conn.execute("SELECT id FROM written").fetchall() == []
    assert conn.execute("SELECT * FROM transactions ORDER BY id").fetchall() == snapshot
    assert state(conn, rule_row)[:2] == (cats["Divers"], "rule")


def test_model_row_becomes_pair_when_its_savings_leg_arrives(conn, cats):
    for _ in range(3):
        manual(conn, "VIR VERS EPARGNE", -10000, cats["Divers"])
    src = add_tx(conn, "VIR VERS EPARGNE", -10000, 1, "2024-04-02")
    reclassify(conn)
    assert state(conn, src)[:2] == (cats["Divers"], "model")
    mirror = add_tx(conn, "VERSEMENT", 10000, 2, "2024-04-03")
    reclassify(conn)
    assert state(conn, src) == (cats["Livret Bleu"], "pair", None, None, None)
    assert state(conn, mirror) == (cats["Livret Bleu"], "pair", None, None, None)
    assert support(conn, src) is None


def test_stale_pair_source_falls_back_when_its_mirror_is_unlinked(conn, cats):
    src, mirror = pair(conn)
    reclassify(conn)
    assert state(conn, src)[:2] == (cats["Livret Bleu"], "pair")
    conn.execute("UPDATE transactions SET mirror_of = NULL WHERE id = ?", (mirror,))
    conn.execute("DELETE FROM transactions WHERE id = ?", (mirror,))
    reclassify(conn)
    assert state(conn, src)[:2] == (None, None)
