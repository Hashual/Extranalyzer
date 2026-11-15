import math
import random
import time
from collections import Counter

import pytest

from app.classify import model
from app.classify.model import Example, build_index, predict

COURSES, LOISIRS, SALAIRE = 1, 2, 3


def ex(label, cat, amount=-4500, account=1):
    return Example(label, amount, account, cat)


def test_empty_index_predicts_nothing():
    p = predict(build_index([]), "MAGASIN ALPHA", -1000, 1)
    assert p.category_id is None
    assert p.confidence == 0.0
    assert p.support == 0
    assert p.best_similarity == 0.0
    assert p.reason == "aucune opération similaire"
    assert not p.is_auto(0.9)


def test_identical_labels_auto():
    idx = build_index([ex("MAGASIN ALPHA", COURSES) for _ in range(4)])
    p = predict(idx, "MAGASIN ALPHA", -4500, 1)
    assert p.category_id == COURSES
    assert p.confidence == 1.0
    assert p.support == 4
    assert p.is_auto(0.9)


def test_support_guard():
    idx = build_index([ex("MAGASIN ALPHA", COURSES) for _ in range(2)])
    p = predict(idx, "MAGASIN ALPHA", -4500, 1)
    assert p.category_id == COURSES
    assert p.confidence == 1.0
    assert p.support == 2
    assert not p.is_auto(0.9)


def test_tie_never_auto():
    exs = [ex("MAGASIN ALPHA", COURSES) for _ in range(3)]
    exs += [ex("MAGASIN ALPHA", LOISIRS) for _ in range(3)]
    p = predict(build_index(exs), "MAGASIN ALPHA", -4500, 1)
    assert p.confidence <= 0.5
    assert not p.is_auto(0.9)


def test_direction_hard_filter():
    idx = build_index([ex("MAGASIN ALPHA", COURSES, -4500) for _ in range(4)])
    p = predict(idx, "MAGASIN ALPHA", 4500, 1)
    assert p.category_id is None
    assert p.support == 0
    assert predict(idx, "MAGASIN ALPHA", 0, 1).category_id is None


def test_credit_uses_credit_examples():
    exs = [ex("MAGASIN ALPHA", COURSES, -4500) for _ in range(4)]
    exs += [ex("MAGASIN ALPHA", SALAIRE, 4500) for _ in range(3)]
    p = predict(build_index(exs), "MAGASIN ALPHA", 4500, 1)
    assert p.category_id == SALAIRE


def test_reason_text():
    idx = build_index([ex("MAGASIN ALPHA", COURSES) for _ in range(4)])
    assert predict(idx, "MAGASIN ALPHA", -4500, 1).reason == "4 opération(s) similaire(s) classée(s) ainsi"


def test_dissimilar_label_below_min_similarity():
    idx = build_index([ex("MAGASIN ALPHA", COURSES) for _ in range(4)])
    p = predict(idx, "ZZZZ QQQQ", -4500, 1)
    assert p.category_id is None


def test_bonuses_break_close_call():
    exs = [ex("MAGASIN ALPHA", COURSES, -4500, 1), ex("MAGASIN ALPHA", LOISIRS, -4500, 2)]
    p = predict(build_index(exs), "MAGASIN ALPHA", -4500, 1)
    assert p.category_id == COURSES
    assert p.confidence > 0.5


def test_k_limits_neighbours():
    idx = build_index([ex("MAGASIN ALPHA", COURSES) for _ in range(20)])
    assert predict(idx, "MAGASIN ALPHA", -4500, 1).support == 7


FILLER = ["MAGASIN ZETA", "PHARMACIE OMEGA", "TAXI SIGMA", "BOULANGERIE PI"]


def reference_neighbours(examples, label, amount, k=7, min_sim=0.35):
    """Implémentation brute-force indépendante (cosinus TF-IDF sur trigrammes)."""

    def grams(text):
        c = Counter()
        for w in text.split():
            p = f" {w} "
            for i in range(len(p) - 2):
                c[p[i : i + 3]] += 1
        return c

    n = len(examples)
    df = Counter()
    gl = [grams(e.label_norm) for e in examples]
    for g in gl:
        df.update(g.keys())

    def vec(g):
        return {t: f * (math.log((1 + n) / (1 + df.get(t, 0))) + 1) for t, f in g.items()}

    def cos(a, b):
        na = math.sqrt(sum(x * x for x in a.values()))
        nb = math.sqrt(sum(x * x for x in b.values()))
        return sum(w * b.get(t, 0.0) for t, w in a.items()) / (na * nb)

    if amount == 0:
        return []
    q = vec(grams(label))
    out = []
    for pos, (e, g) in enumerate(zip(examples, gl)):
        if (e.amount_cents > 0) != (amount > 0) or e.amount_cents == 0:
            continue
        sim = cos(q, vec(g))
        if sim >= min_sim:
            out.append((sim, pos))
    out.sort(key=lambda t: (-round(t[0], 12), t[1]))
    return [(sim, examples[pos]) for sim, pos in out[:k]]


def test_exact_similarity_below_threshold_is_excluded():
    # N=3 : sim("CARTE ALPHA", "CARTE BETA") = 0.33798 < 0.35 -> exclu
    exs = [ex("CARTE ALPHA", COURSES), ex("CARTE ALPHA", COURSES), ex("CARTE BETA", LOISIRS)]
    old = model.MIN_SIMILARITY
    model.MIN_SIMILARITY = 0.0
    try:
        sims = {e.label_norm: s for s, e in build_index(exs).neighbours("CARTE ALPHA", -4500)}
    finally:
        model.MIN_SIMILARITY = old
    assert sims["CARTE BETA"] == pytest.approx(0.33798, abs=1e-5)
    p = predict(build_index(exs), "CARTE ALPHA", -4500, 1)
    assert p.category_id == COURSES
    assert p.support == 2
    assert p.confidence == 1.0  # CARTE BETA n'est pas un voisin


def test_similarity_just_above_threshold_is_kept():
    exs = [ex("CARTE ALPHA", COURSES), ex("CARTE ALPHA", COURSES), ex("ZZ", LOISIRS)]
    neigh = build_index(exs).neighbours("CARTE BETA", -4500)
    assert [s for s, _ in neigh] == [pytest.approx(0.36528, abs=1e-5)] * 2
    p = predict(build_index(exs), "CARTE BETA", -4500, 1)
    assert p.support == 2
    assert p.best_similarity == pytest.approx(0.36528, abs=1e-5)


def test_is_auto_refused_only_by_best_similarity_gate():
    exs = [ex("CARTE ALPHA", COURSES) for _ in range(4)] + [ex(f, LOISIRS) for f in FILLER]
    p = predict(build_index(exs), "CARTE BETA", -4500, 1)
    assert p.category_id == COURSES
    assert p.confidence == 1.0
    assert p.support == 4
    assert p.best_similarity == pytest.approx(0.3759, abs=1e-4)
    assert not p.is_auto(0.9)  # confiance et support suffisants, similarité < 0,6
    close = predict(build_index(exs), "CARTE ALPHA", -4500, 1)
    assert close.is_auto(0.9)


def test_magnitude_bonus_alone_decides_winner():
    # même compte pour les deux : seul l'ordre de grandeur du montant diffère
    exs = [ex("MAGASIN ALPHA", LOISIRS, -450000, 1), ex("MAGASIN ALPHA", COURSES, -4500, 1)]
    p = predict(build_index(exs), "MAGASIN ALPHA", -4500, 1)
    assert p.category_id == COURSES
    assert p.confidence == pytest.approx(1.1 / 2.1)
    assert p.support == 1


def test_exact_confidence_and_support_on_mixed_neighbourhood():
    exs = [ex("CARTE ALPHA", COURSES, -4500, 1) for _ in range(3)]
    exs += [ex("CARTE ALPHA", LOISIRS, -450000, 2), ex("CARTE ALPHA", LOISIRS, -4500, 1)]
    p = predict(build_index(exs), "CARTE ALPHA", -4500, 1)
    # COURSES : 3 x 1,21 ; LOISIRS : 1,0 + 1,21
    assert p.category_id == COURSES
    assert p.support == 3
    assert p.confidence == pytest.approx(3.63 / (3.63 + 2.21))
    assert p.best_similarity == pytest.approx(1.0)


def test_best_similarity_is_the_winners_not_the_overall_best():
    exs = [ex("CARTE ALPHA", LOISIRS)] + [ex("CARTE ALPHAB", COURSES) for _ in range(3)]
    exs += [ex(f, LOISIRS) for f in FILLER]
    p = predict(build_index(exs), "CARTE ALPHA", -4500, 1)
    assert p.category_id == COURSES
    assert p.best_similarity == pytest.approx(0.7754, abs=1e-4)


def test_magnitude_guard_on_zero():
    assert model._magnitude(0) == -1
    assert model._magnitude(4500) == 3


def _random_dataset(rng, n):
    words = ["ALPHA", "BETA", "GAMMA", "DELTA", "OMEGA", "SIGMA", "ZETA", "CARTE", "VIR", "PRLV",
             "MAGASIN", "TAXI", "PHARMACIE", "CAFE", "LOYER", "ASSURANCE"]
    out = []
    for _ in range(n):
        label = " ".join(rng.sample(words, rng.randint(1, 3)))
        amount = rng.choice([-1, 1]) * rng.randint(100, 900000)
        out.append(Example(label, amount, rng.randint(1, 3), rng.randint(1, 6)))
    return out


def test_inverted_index_matches_bruteforce_reference():
    rng = random.Random(1234)
    examples = _random_dataset(rng, 300)
    index = build_index(examples)
    for q in _random_dataset(rng, 120):
        got = index.neighbours(q.label_norm, q.amount_cents)
        want = reference_neighbours(examples, q.label_norm, q.amount_cents)
        assert [s for s, _ in got] == pytest.approx([s for s, _ in want], abs=1e-9)
        assert [e for _, e in got] == [e for _, e in want]
    assert index.neighbours("CARTE", 0) == []


def test_performance_1100_examples_1100_queries():
    rng = random.Random(7)
    examples = _random_dataset(rng, 1100)
    start = time.perf_counter()
    index = build_index(examples)
    for q in examples:
        predict(index, q.label_norm, q.amount_cents, q.account_id)
    elapsed = time.perf_counter() - start
    print(f"1100x1100: {elapsed:.3f}s")
    assert elapsed < 2.0


def test_trusted_support_counts_manual_or_rule_neighbours_of_the_winner():
    exs = [Example("VIR M TEST", 10000, 1, SALAIRE, trusted=False) for _ in range(3)]
    exs += [Example("VIR M TEST", 10000, 1, SALAIRE, trusted=True)]
    exs += [Example("VIR M TEST", 10000, 1, LOISIRS, trusted=True)]
    p = predict(build_index(exs), "VIR M TEST", 10000, 1)
    assert p.category_id == SALAIRE
    assert p.support == 4
    assert p.trusted_support == 1


def test_pair_only_evidence_never_auto():
    exs = [Example("VIR M TEST", 10000, 1, SALAIRE, trusted=False) for _ in range(5)]
    p = predict(build_index(exs), "VIR M TEST", 10000, 1)
    assert p.category_id == SALAIRE
    assert p.confidence == 1.0 and p.support == 5 and p.best_similarity == pytest.approx(1.0)
    assert p.trusted_support == 0
    assert not p.is_auto(0.9)          # seul le garde « au moins un exemple de confiance » refuse
    exs[0] = Example("VIR M TEST", 10000, 1, SALAIRE, trusted=True)
    assert predict(build_index(exs), "VIR M TEST", 10000, 1).is_auto(0.9)


def test_examples_are_trusted_by_default():
    assert ex("MAGASIN ALPHA", COURSES).trusted is True
    assert predict(build_index([]), "X", -1, 1).trusted_support == 0


def test_neighbours_cached_per_label_and_direction():
    idx = build_index([ex("MAGASIN ALPHA", COURSES, -4500), ex("MAGASIN ALPHA", SALAIRE, 4500)])
    debit = idx.neighbours("MAGASIN ALPHA", -12)
    assert idx.neighbours("MAGASIN ALPHA", -999999) is debit         # même libellé, même sens : calcul réutilisé
    credit = idx.neighbours("MAGASIN ALPHA", 12)
    assert [e.category_id for _, e in debit] == [COURSES] and [e.category_id for _, e in credit] == [SALAIRE]


def test_store_numbers_do_not_separate_short_labels():
    """Le numéro de magasin change d'une opération à l'autre : il ne doit pas masquer le commerce."""
    lidl = [ex(f"PAIEMENT CB LYON LIDL {n}", COURSES) for n in (4521, 1187, 30422)]
    p = predict(build_index(lidl), "PAIEMENT CB LYON LIDL 87412", -4500, 1)
    assert p.best_similarity == pytest.approx(1.0) and p.support == 3
    assert p.is_auto(0.9)
    p = predict(build_index(lidl + [ex("PAIEMENT CB LYON CINEMA PATHE", LOISIRS)]),
                "PAIEMENT CB LYON LIDL 87412", -4500, 1)
    assert p.category_id == COURSES and p.best_similarity == pytest.approx(1.0)


def test_numeric_only_label_has_no_neighbour():
    idx = build_index([ex("12345", COURSES) for _ in range(3)])
    assert predict(idx, "12345", -4500, 1).category_id is None
