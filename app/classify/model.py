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
    if amount_cents == 0:
        return -1
    return math.floor(math.log10(abs(amount_cents)))


class Index:
    """Index opaque : exemples vectorisés (TF-IDF, normalisés L2).

    Un index inversé par signe (trigramme -> [(position, poids)]) permet de
    calculer les cosinus sans parcourir tous les exemples.
    Les égalités sont départagées par ordre d'insertion : le résultat dépend
    donc de l'ordre des exemples, que l'appelant doit fournir trié par id.
    """

    def __init__(self, examples: list[Example]) -> None:
        self._n = len(examples)
        self._examples = list(examples)
        grams = [_trigrams(e.label_norm) for e in self._examples]
        df: Counter = Counter()
        for g in grams:
            df.update(g.keys())
        self._df = df
        self._inverted: dict[int, dict[str, list[tuple[int, float]]]] = {1: {}, -1: {}}
        for pos, (example, g) in enumerate(zip(self._examples, grams)):
            sign = _sign(example.amount_cents)
            if sign == 0:
                continue
            vec = self._vectorize(g)
            postings = self._inverted[sign]
            for gram, weight in vec.items():
                postings.setdefault(gram, []).append((pos, weight))

    def _idf(self, gram: str) -> float:
        return math.log((1 + self._n) / (1 + self._df.get(gram, 0))) + 1

    def _vectorize(self, grams: Counter) -> dict[str, float]:
        """Vecteur TF-IDF normalisé (vide si le libellé n'a aucun trigramme)."""
        vec = {g: tf * self._idf(g) for g, tf in grams.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm == 0.0:
            return {}
        return {g: v / norm for g, v in vec.items()}

    def neighbours(self, label_norm: str, amount_cents: int) -> list[tuple[float, Example]]:
        sign = _sign(amount_cents)
        if sign == 0 or not self._examples:
            return []
        qvec = self._vectorize(_trigrams(label_norm))
        postings = self._inverted[sign]
        dots: dict[int, float] = {}
        for gram, qw in qvec.items():
            for pos, w in postings.get(gram, ()):
                dots[pos] = dots.get(pos, 0.0) + qw * w
        scored = [(sim, pos) for pos, sim in dots.items() if sim >= MIN_SIMILARITY]
        # arrondi pour que le bruit flottant ne casse pas les égalités (ordre d'insertion)
        scored.sort(key=lambda t: (-round(t[0], 12), t[1]))
        return [(sim, self._examples[pos]) for sim, pos in scored[:K]]


def build_index(examples: list[Example]) -> Index:
    return Index(examples)


def predict(index: Index, label_norm: str, amount_cents: int, account_id: int) -> Prediction:
    neighbours = index.neighbours(label_norm, amount_cents)
    if not neighbours:
        return _NO_NEIGHBOUR
    magnitude = _magnitude(amount_cents)
    weights: dict[int, float] = {}
    counts: Counter = Counter()
    best_sim: dict[int, float] = {}
    total = 0.0
    for sim, ex in neighbours:
        w = sim
        if ex.account_id == account_id:
            w *= BONUS
        if _magnitude(ex.amount_cents) == magnitude:
            w *= BONUS
        weights[ex.category_id] = weights.get(ex.category_id, 0.0) + w
        counts[ex.category_id] += 1
        best_sim.setdefault(ex.category_id, sim)  # voisins triés : le premier est le meilleur
        total += w
    winner = max(weights, key=weights.__getitem__)
    support = counts[winner]
    return Prediction(
        category_id=winner,
        confidence=weights[winner] / total,
        support=support,
        best_similarity=best_sim[winner],
        reason=f"{support} opération(s) similaire(s) classée(s) ainsi",
    )
