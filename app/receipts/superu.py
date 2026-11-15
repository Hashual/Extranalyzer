"""Lecteur des tickets de caisse Super U (PDF texte, une seule page longue, police à chasse fixe).

Règles relevées sur les vrais tickets (structure seulement) ; les mots pdfplumber sont regroupés en
lignes de texte, avec la graisse du premier mot. Deux mises en page coexistent :

- **caisse récente** : `*** VENTE ***` ouvre les articles et `Nombre de lignes d'article` les ferme.
  Un titre de rayon est une ligne en gras sans montant. Un article tient sur une ligne
  `LIBELLÉ … montant € TVA`, suivie de `n x prix EUR` (quantité), ou de `Pesée manuelle` puis
  `q kg x prix €/kg` (pesé). Le total est `TOTAL [n] Articles montant €`. La date et l'heure (avec
  secondes) sont sous l'en-tête `Date Heure Magasin Tpv Util Tick`, en pied de ticket.
- **caisse ancienne** : l'en-tête `Opérateur Date Heure TPV Ticket` est suivi de la date et de l'heure
  (sans secondes), puis des articles jusqu'à la ligne `===========`. Un titre de rayon commence par
  `>>>>` (en gras) ; certaines caisses (caisses automatiques) n'en impriment aucun : les articles vont
  alors dans le rayon `SANS RAYON`. Un article est `LIBELLÉ montant € TVA` ; une quantité met le
  libellé seul sur sa ligne puis `n x prix € montant € TVA` ; un pesé met `LIBELLÉ TVA` puis
  `q kg x prix €/kg montant €`. Le total est `TOTAL montant €` (double taille). Le séparateur décimal
  est la virgule ou le point.

Commun aux deux : le magasin est la première ligne du ticket. Le bloc `VOS REDUCTIONS IMMEDIATES`
liste des remises `n x prix € LIBELLÉ -montant €` (sans rayon visible : rayon `REMISES`), puis
`RESTE A PAYER`, qui devient le total du ticket. Suivent les paiements (`libellé [EUR] montant €`)
jusqu'au tableau de TVA (`11 / 5,50 …` ou `TVA HT TTC`) :
- carte bancaire : `CB`, `CB SANS CONTACT`, `CARTE BANCAIRE` ;
- titre-restaurant : `TRD …` (carte titre-restaurant), `CHEQUE BIMPLI`, titres papier ; la ligne
  `Payé en TITRES RESTAURANT` n'est qu'un récapitulatif ;
- bon : `BON DE REDUCTION`, `BONS U`, et cagnotte `PAIEMENT € CARTE U` ;
- espèces : `ESPECES`, net du `Rendu ESPECES`.
Le ticket nomme toujours la carte titre-restaurant (`TRD …`), le ticket CB n'est donc pas nécessaire
pour distinguer les cartes. Les `€ Carte U gagnés`, après le tableau de TVA, sont ignorés.

Ticket CB : première ligne `CARTE BANCAIRE` ou `CARTE TITRE-RESTAURANT`, puis `MONTANT` et
`TICKET CLIENT`.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pdfplumber

from app.receipts.model import ParsedReceipt, Payment, ReceiptLine, ReceiptParseError
from app.store.repo import fold

NO_AISLE = "SANS RAYON"
DISCOUNT_AISLE = "REMISES"

AMOUNT = r"-?\d+[.,]\d\d"
QTY = r"\d+(?:[.,]\d+)?"
ITEM_RE = re.compile(rf"(.+?) ({AMOUNT}) € (\d{{1,2}})")
QTY_EUR_RE = re.compile(rf"({QTY}) x ({AMOUNT}) EUR")                          # caisse récente
QTY_AMOUNT_RE = re.compile(rf"({QTY}) x ({AMOUNT}) € ({AMOUNT}) € \d{{1,2}}")  # caisse ancienne
KG_RE = re.compile(rf"({QTY}) kg x ({AMOUNT}) €/kg(?: ({AMOUNT}) €)?")
REDUCTION_RE = re.compile(rf"({QTY}) [xX] ({QTY}) ?€ (.+) (-\d+[.,]\d\d) €")
TOTAL_RE = re.compile(rf"TOTAL(?: \[\d+\] Articles?)? ({AMOUNT}) €")
PAID_RE = re.compile(rf"RESTE A PAYER ({AMOUNT}) €")
CHANGE_RE = re.compile(rf"RENDU ESPECES ({AMOUNT})")           # sur le texte replié (sans €)
PAYMENT_RE = re.compile(rf"(.+?)(?: EUR)? ({AMOUNT}) ?€")
DATE_RE = re.compile(r"(\d\d)/(\d\d)/(\d\d) (\d\d):(\d\d)(?::(\d\d))?")
TVA_TABLE_RE = re.compile(r"\d{1,2} / \d")

SALE_START = "*** VENTE ***"
SALE_END = "Nombre de lignes d'article"
CLASSIC_HEADER = "Date Heure TPV Ticket"
NOT_PAYMENTS = ("SOUS-TOTAL", "TOTAL TVA", "TOTAL REDUCTIONS", "DONT ARTICLES", "PAYE EN", "ARRONDI")
MEAL_VOUCHER_WORDS = ("TITRE RESTAURANT", "TITRES RESTAURANT", "TITRE-RESTAURANT", "TICKET RESTAURANT",
                      "CARTE TR", "BIMPLI", "SWILE", "EDENRED", "CONECS", "PLUXEE", "SODEXO", "UP DEJEUNER",
                      "CHEQUE DEJEUNER")


@dataclass
class _Row:
    text: str
    bold: bool


def _rows(pdf: Path, first_page_only: bool = False) -> list[_Row]:
    """Lignes de texte dans l'ordre de lecture (mots regroupés par ordonnée)."""
    rows: list[_Row] = []
    with pdfplumber.open(pdf) as doc:
        for page in doc.pages[:1] if first_page_only else doc.pages:
            words = sorted(page.extract_words(extra_attrs=["fontname"]), key=lambda w: (round(w["top"]), w["x0"]))
            line: list[dict] = []
            for word in words:
                if line and abs(word["top"] - line[0]["top"]) > 3:
                    rows.append(_Row(" ".join(w["text"] for w in line), "Bold" in line[0]["fontname"]))
                    line = []
                line.append(word)
            if line:
                rows.append(_Row(" ".join(w["text"] for w in line), "Bold" in line[0]["fontname"]))
    return rows


def _cents(text: str) -> int:
    sign = -1 if text.startswith("-") else 1
    units, _, decimals = text.lstrip("-").replace(",", ".").partition(".")
    return sign * (int(units) * 100 + int((decimals + "00")[:2]))


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def payment_method(label: str) -> str:
    """card | meal_voucher | voucher | cash | other, d'après le libellé du paiement sur le ticket."""
    f = " ".join(fold(label).split())
    if f.startswith("TRD ") or any(w in f for w in MEAL_VOUCHER_WORDS):
        return "meal_voucher"
    if f == "CB" or f.startswith(("CB ", "CARTE BANCAIRE")):
        return "card"
    if f.startswith(("BON", "COUPON")) or "CARTE U" in f:
        return "voucher"
    if f.startswith("ESPECES"):
        return "cash"
    return "other"


def _is_receipt(texts: list[str]) -> bool:
    recent = SALE_START in texts and any(t.startswith(SALE_END) for t in texts)
    classic = any(CLASSIC_HEADER in t for t in texts) and any(re.fullmatch(r"\d+ Article\(s\)", t)
                                                              for t in texts)
    return recent or classic


def _purchased_at(texts: list[str]) -> datetime:
    for head, values in zip(texts, texts[1:]):
        if "Date Heure" in head and (m := DATE_RE.search(values)):
            day, month, year, hour, minute, second = m.groups()
            return datetime(2000 + int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    raise ReceiptParseError("date introuvable")


def _items(rows: list[_Row]) -> list[ReceiptLine]:
    lines: list[ReceiptLine] = []
    aisle = NO_AISLE
    i = 0
    while i < len(rows):
        text, bold = rows[i].text, rows[i].bold
        j = i + 1                                       # ligne de quantité ou de poids éventuelle
        if j < len(rows) and fold(rows[j].text).startswith("PESEE"):      # « Pesée manuelle »
            j += 1
        detail = rows[j].text if j < len(rows) else ""
        consumed = False
        if text.startswith(">>>>"):
            aisle = text[4:].strip()
        elif bold and not re.search(AMOUNT, text) and not text.startswith("*"):
            aisle = text
        elif m := ITEM_RE.fullmatch(text):
            amount = _cents(m[2])
            line = ReceiptLine(aisle, m[1], 1, amount, amount, "discount" if amount < 0 else "item")
            if q := QTY_EUR_RE.fullmatch(detail):
                line.quantity, line.unit_cents, consumed = _number(q[1]), _cents(q[2]), True
            elif (k := KG_RE.fullmatch(detail)) and k[3] is None:
                line.quantity, line.unit_cents, consumed = _number(k[1]), _cents(k[2]), True
            lines.append(line)
        elif q := QTY_AMOUNT_RE.fullmatch(detail):
            amount = _cents(q[3])
            lines.append(ReceiptLine(aisle, text, _number(q[1]), _cents(q[2]), amount,
                                     "discount" if amount < 0 else "item"))
            consumed = True
        elif (k := KG_RE.fullmatch(detail)) and k[3] is not None:
            label = re.sub(r" \d{1,2}$", "", text)          # le code TVA reste sur la ligne du libellé
            lines.append(ReceiptLine(aisle, label, _number(k[1]), _cents(k[2]), _cents(k[3]), "item"))
            consumed = True
        # sinon : ligne d'information (garantie, coupon…), ignorée
        i = j + 1 if consumed else i + 1
    return lines


class SuperUParser:
    store = "superu"

    def kind(self, pdf: Path) -> str | None:
        try:
            texts = [r.text for r in _rows(pdf, first_page_only=True)]
        except Exception:       # noqa: BLE001 - un PDF illisible n'est simplement pas de cette enseigne
            return None
        if _is_receipt(texts):
            return "receipt"
        if texts and texts[0].startswith("CARTE ") and "TICKET CLIENT" in texts \
                and any(t.startswith("MONTANT") for t in texts):
            return "cb_slip"
        return None

    def parse(self, pdf: Path) -> ParsedReceipt:
        rows = _rows(pdf)
        texts = [r.text for r in rows]
        if not _is_receipt(texts):
            raise ReceiptParseError("format de ticket non reconnu")
        if SALE_START in texts:
            start = texts.index(SALE_START) + 1
            end = next((i for i in range(start, len(texts)) if texts[i].startswith(SALE_END)), len(texts))
        else:
            start = next(i for i, t in enumerate(texts) if CLASSIC_HEADER in t) + 2
            end = next((i for i in range(start, len(texts)) if re.fullmatch(r"=+", texts[i])), len(texts))
        lines = _items(rows[start:end])
        if not lines:
            raise ReceiptParseError("aucun article")

        total = paid = None
        labels: list[tuple[str, int]] = []
        change = 0
        reductions = False
        for row in rows[end:]:
            text = row.text
            if TVA_TABLE_RE.match(text) or text == "TVA HT TTC" or text.startswith("VOS AVANTAGES"):
                break
            if text.startswith("*"):
                reductions = "REDUCTIONS IMMEDIATES" in text
                continue
            if reductions and (m := REDUCTION_RE.fullmatch(text)):
                quantity, amount = _number(m[1]), _cents(m[4])
                lines.append(ReceiptLine(DISCOUNT_AISLE, m[3], quantity, -_cents(m[2]), amount, "discount"))
                continue
            folded = " ".join(fold(text).split())
            if folded.startswith(NOT_PAYMENTS):
                continue
            if m := TOTAL_RE.fullmatch(text):
                total = _cents(m[1])
            elif m := PAID_RE.fullmatch(text):
                paid = _cents(m[1])
            elif m := CHANGE_RE.fullmatch(folded):
                change += _cents(m[1])
            elif m := PAYMENT_RE.fullmatch(text):
                labels.append((m[1], _cents(m[2])))
        if total is None:
            raise ReceiptParseError("total introuvable")

        payments = [Payment(payment_method(label), cents) for label, cents in labels]
        if change:
            cash = next((p for p in reversed(payments) if p.method == "cash"), None)
            if cash is None:
                raise ReceiptParseError("rendu sans paiement en espèces")
            cash.amount_cents -= change
        return ParsedReceipt(" ".join(texts[0].upper().split()), _purchased_at(texts),
                             paid if paid is not None else total, lines, payments)
