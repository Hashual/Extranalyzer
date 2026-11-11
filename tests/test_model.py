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
