"""Modèle local de classement : plus proches voisins sur trigrammes de caractères.

Module pur (bibliothèque standard uniquement, aucun accès base).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

K = 7
MIN_SIMILARITY = 0.35
BONUS = 1.10
MIN_SUPPORT = 3
MIN_BEST_SIMILARITY = 0.6


@dataclass(frozen=True)
class Example:
    label_norm: str
    amount_cents: int
    account_id: int
    category_id: int


@dataclass(frozen=True)
class Prediction:
    category_id: int | None
    confidence: float
    support: int
    best_similarity: float
    reason: str

    def is_auto(self, threshold: float) -> bool:
        return (
            self.category_id is not None
            and self.confidence >= threshold
            and self.support >= MIN_SUPPORT
            and self.best_similarity >= MIN_BEST_SIMILARITY
        )


_NO_NEIGHBOUR = Prediction(None, 0.0, 0, 0.0, "aucune opération similaire")


def _trigrams(label: str) -> Counter:
    grams: Counter = Counter()
    for word in label.split():
        padded = f" {word} "
        for i in range(len(padded) - 2):
            grams[padded[i : i + 3]] += 1
    return grams


def _sign(amount_cents: int) -> int:
    return (amount_cents > 0) - (amount_cents < 0)


def _magnitude(amount_cents: int) -> int:
    return math.floor(math.log10(abs(amount_cents)))


class Index:
    """Index opaque : exemples vectorisés (TF-IDF) dans l'ordre d'insertion."""

    def __init__(self, examples: list[Example]) -> None:
        self._n = len(examples)
        grams = [_trigrams(e.label_norm) for e in examples]
        df: Counter = Counter()
        for g in grams:
            df.update(g.keys())
        self._df = df
        self._examples = list(examples)
        self._vectors = [self._vectorize(g) for g in grams]

    def _idf(self, gram: str) -> float:
        return math.log((1 + self._n) / (1 + self._df.get(gram, 0))) + 1

    def _vectorize(self, grams: Counter) -> tuple[dict[str, float], float]:
        vec = {g: tf * self._idf(g) for g, tf in grams.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return vec, norm

    def vectorize_label(self, label_norm: str) -> tuple[dict[str, float], float]:
        return self._vectorize(_trigrams(label_norm))

    def neighbours(self, label_norm: str, amount_cents: int) -> list[tuple[float, Example]]:
        sign = _sign(amount_cents)
        if sign == 0 or not self._examples:
            return []
        qvec, qnorm = self.vectorize_label(label_norm)
        if qnorm == 0.0:
            return []
        scored: list[tuple[float, int, Example]] = []
        for pos, (example, (vec, norm)) in enumerate(zip(self._examples, self._vectors)):
            if _sign(example.amount_cents) != sign or norm == 0.0:
                continue
            small, large = (qvec, vec) if len(qvec) <= len(vec) else (vec, qvec)
            dot = sum(w * large[g] for g, w in small.items() if g in large)
            sim = dot / (qnorm * norm)
            if sim >= MIN_SIMILARITY:
                scored.append((sim, pos, example))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [(sim, ex) for sim, _, ex in scored[:K]]


def build_index(examples: list[Example]) -> Index:
    return Index(examples)


def predict(index: Index, label_norm: str, amount_cents: int, account_id: int) -> Prediction:
    neighbours = index.neighbours(label_norm, amount_cents)
    if not neighbours:
        return _NO_NEIGHBOUR
    magnitude = _magnitude(amount_cents)
    weights: dict[int, float] = {}
    counts: Counter = Counter()
    total = 0.0
    for sim, ex in neighbours:
        w = sim
        if ex.account_id == account_id:
            w *= BONUS
        if _magnitude(ex.amount_cents) == magnitude:
            w *= BONUS
        weights[ex.category_id] = weights.get(ex.category_id, 0.0) + w
        counts[ex.category_id] += 1
        total += w
    winner = max(weights, key=weights.__getitem__)
    support = counts[winner]
    return Prediction(
        category_id=winner,
        confidence=weights[winner] / total,
        support=support,
        best_similarity=neighbours[0][0],
        reason=f"{support} opération(s) similaire(s) classée(s) ainsi",
    )
