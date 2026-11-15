"""Générateur de tickets Super U synthétiques (fixtures de test uniquement, libellés fictifs).

Reproduit la structure relevée sur les vrais tickets (aucun contenu réel) : une seule page longue de
1092 pt de large, police à chasse fixe de 28,6 pt (≈ 17,2 pt par caractère), une ligne de texte tous
les 33,2 pt. Deux mises en page existent :

- `omnipos` (caisses récentes) : `*** VENTE ***`, titres de rayon en gras sans montant, article
  `LIBELLÉ … montant € TVA` suivi de `n x prix EUR` ou de `Pesée manuelle` + `q kg x prix €/kg`,
  puis `Nombre de lignes d'article`, `TOTAL [n] Articles`, paiements, `Payé en TITRES RESTAURANT`
  (récapitulatif), tableau de TVA ; date et heure (avec secondes) en pied de ticket ;
- `classic` (caisses plus anciennes) : en-tête `Opérateur Date Heure TPV Ticket` (heure sans
  secondes), titres `>>>> RAYON` en gras (parfois absents), article `LIBELLÉ montant € TVA`, ou
  libellé seul suivi de `n x prix € montant € TVA` (quantité) ou de `q kg x prix €/kg montant €`
  (pesé, le code TVA reste sur la ligne du libellé), `TOTAL` en double taille, `n Article(s)`.

Les coordonnées sont celles de pdfplumber (origine en haut à gauche) ; la conversion vers reportlab
se fait dans `_Ticket.flush`.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reportlab.pdfgen import canvas

PAGE_W = 1092
SIZE, BIG = 28.6, 57.2
CHAR_W = SIZE * 0.6                 # Courier : 0,6 em, comme la police à chasse fixe des vrais tickets
LINE_H = 33.2
FONT, BOLD = "Courier", "Courier-Bold"
LEFT = {"omnipos": 25, "classic": 42}
WIDTH = {"omnipos": 56, "classic": 54}      # caractères par ligne
TOP_MARGIN, BOTTOM_MARGIN = 494, 300


@dataclass
class Item:
    label: str
    amount_cents: int
    quantity: float = 1
    unit_cents: int | None = None   # par défaut : montant / quantité
    weighed: bool = False           # quantité en kg, prix au kg
    tva: int = 11


@dataclass
class Discount:
    """Réduction immédiate (bloc `VOS REDUCTIONS IMMEDIATES`), montant positif."""

    label: str
    amount_cents: int
    quantity: int = 1


def _money(cents: int, sep: str = ",") -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}{sep}{abs(cents) % 100:02d}"


def _qty(q: float) -> str:
    return f"{q:.3f}".replace(".", ",")


class _Ticket:
    def __init__(self, layout: str):
        self.layout = layout
        self.left = LEFT[layout]
        self.width = WIDTH[layout]
        self.rows: list[tuple[float, float, str, str, float]] = []      # (x, top, texte, police, taille)
        self.top = TOP_MARGIN

    def line(self, text: str, *, col: int = 0, bold: bool = False, size: float = SIZE) -> None:
        self.rows.append((self.left + col * CHAR_W, self.top, text, BOLD if bold else FONT, size))
        self.top += LINE_H if size == SIZE else LINE_H * 2

    def center(self, text: str, **kw) -> None:
        self.line(text, col=max(0, (self.width - len(text)) // 2), **kw)

    def spread(self, head: str, tail: str, **kw) -> None:
        """`head` à gauche, `tail` calé à droite de la ligne (deux fois moins de caractères en double taille)."""
        width = self.width // 2 if kw.get("size") == BIG else self.width
        self.line(head + " " * max(1, width - len(head) - len(tail)) + tail, **kw)

    def gap(self, n: float = 1) -> None:
        self.top += LINE_H * n

    def flush(self, path: Path) -> Path:
        height = self.top + BOTTOM_MARGIN
        c = canvas.Canvas(str(path), pagesize=(PAGE_W, height))
        for x, top, text, font, size in self.rows:
            c.setFont(font, size)
            c.drawString(x, height - top - size, text)
        c.showPage()
        c.save()
        return path


def _unit(item: Item) -> int:
    if item.unit_cents is not None:
        return item.unit_cents
    return round(item.amount_cents / item.quantity)


def _omnipos_items(t: _Ticket, aisles, sep: str) -> int:
    count = 0
    for aisle, items in aisles:
        if aisle:
            t.line(aisle, bold=True)
        for item in items:
            t.spread(item.label, f"{_money(item.amount_cents, sep)} € {item.tva:>2}")
            if item.weighed:
                t.line("Pesée manuelle", col=4)
                t.line(f"{_qty(item.quantity)} kg x {_money(_unit(item), sep)} €/kg", col=5)
                count += 1
            else:
                t.line(f"{item.quantity:g} x {_money(_unit(item), sep)} EUR", col=12)
                count += int(item.quantity)
    return count


def _classic_items(t: _Ticket, aisles, sep: str) -> int:
    count = 0
    for aisle, items in aisles:
        if aisle:
            t.line(f">>>> {aisle}", bold=True)
        for item in items:
            amount = _money(item.amount_cents, sep)
            if item.weighed:
                t.spread(item.label, f"{item.tva:>2}")
                t.line(f"{_qty(item.quantity)} kg x {_money(_unit(item), sep)} €/kg {amount} €", col=11)
                count += 1
            elif item.quantity != 1:
                t.line(item.label)
                q = f"{item.quantity:g}" if float(item.quantity).is_integer() else _qty(item.quantity)
                t.spread(f"    {q} x {_money(_unit(item), sep)} €", f"{amount} € {item.tva:>2}")
                count += int(item.quantity)
            else:
                t.spread(item.label, f"{amount} € {item.tva:>2}")
                count += 1
    return count


def _reductions(t: _Ticket, discounts: list[Discount], sep: str, paid: int) -> None:
    t.center("** VOS REDUCTIONS IMMEDIATES **", bold=True)
    for d in discounts:
        unit = d.amount_cents // d.quantity
        if t.layout == "omnipos":
            head = f"{d.quantity} x {unit // 100}.{unit % 100:02d}€ {d.label}"
        else:
            head = f"   {d.quantity} x {_money(unit, sep)} € {d.label}"
        t.spread(head, f"{_money(-d.amount_cents, sep)} €")
    t.spread("TOTAL REDUCTIONS IMMEDIATES", f"{_money(-sum(d.amount_cents for d in discounts), sep)} €",
             bold=True)
    t.line("*" * t.width, bold=True)
    t.spread("RESTE A PAYER", f"{_money(paid, sep)} €", bold=t.layout == "omnipos",
             size=SIZE if t.layout == "omnipos" else BIG)


def _payments(t: _Ticket, payments, change_cents: int, sep: str) -> None:
    for label, cents in payments:
        t.spread(label if t.layout == "omnipos" else f" {label}", f"{_money(cents, sep)} €")
        if label.startswith("BON"):
            t.line(f"1 x {_money(cents, sep)} EUR", col=8)
    if change_cents:
        t.spread(" Rendu ESPECES", f"{_money(change_cents, sep)} €", bold=True)


def _loyalty(t: _Ticket, sep: str) -> None:
    """Bloc des € Carte U gagnés : montants positifs après les paiements, à ignorer."""
    t.line("*" * t.width)
    t.center("VOS AVANTAGES CARTE U")
    t.center("No CARTE : 0000XXXXX00000")
    t.line(f"1 x 0.50€ PRODUIT FIDELITE TEST {_money(50, sep)} €", col=3)
    t.center(f"VOS € CARTE U GAGNES : {_money(50, sep)} €")
    t.center("TICKET A CONSERVER")
    t.line("*" * t.width)


def make_superu_receipt(path: Path, *, layout: str = "omnipos", shop: str = "MAGASIN TEST",
                        when: datetime = datetime(2025, 1, 8, 15, 10, 1),
                        aisles: list[tuple[str | None, list[Item]]],
                        discounts: list[Discount] = (), payments: list[tuple[str, int]] | None = None,
                        change_cents: int = 0, total_cents: int | None = None, sep: str = ",") -> Path:
    """Ticket fictif. `total_cents` force un total imprimé (incohérent) ; `sep` : séparateur décimal.

    Par défaut, un seul paiement `CB` du montant à payer.
    """
    t = _Ticket(layout)
    gross = sum(i.amount_cents for _, items in aisles for i in items)
    paid = gross - sum(d.amount_cents for d in discounts)
    printed = gross if total_cents is None else total_cents
    payments = payments if payments is not None else [("CB", paid)]

    t.center(shop)
    if layout == "omnipos":
        t.center("Magasin 00000")
        t.center("1 Rue Fictive")
        t.center("00000 VILLE TEST")
        t.gap(2)
        t.center("*** VENTE ***")
        t.gap()
        count = _omnipos_items(t, aisles, sep)
        t.gap()
        t.line(f"Nombre de lignes d'article {sum(len(i) for _, i in aisles)}")
        t.line("-----------", col=45)
        t.spread(f"TOTAL [{count}] Articles", f"{_money(printed, sep)} €", bold=True)
        t.gap()
        t.spread("SOUS-TOTAL", f"{_money(printed * 95 // 100, sep)} €")
        t.spread("TOTAL TVA", f"{_money(printed - printed * 95 // 100, sep)} €")
        t.spread("            Dont articles éligibles TR", f"{_money(printed, sep)} €")
        if discounts:
            _reductions(t, list(discounts), sep, paid)
        _payments(t, payments, change_cents, sep)
        meal = sum(c for label, c in payments if "TITRE" in label or label.startswith(("TRD", "CHEQUE")))
        if meal:
            t.gap()
            t.spread("Payé en TITRES RESTAURANT", f"{_money(meal, sep)} €")
        t.gap()
        t.line("TVA HT TTC", col=20)
        t.line(f"11 5,50 {_money(printed, sep)} €", col=0)
        t.line("-" * 47)
        _loyalty(t, sep)
        t.center("Date Heure Magasin Tpv Util Tick")
        t.center(f"{when:%d/%m/%y %H:%M:%S} 00000 001 001 00001")
    else:
        t.center("1 RUE FICTIVE")
        t.center("00000 VILLE TEST")
        t.gap(4)
        t.line(" Opérateur Date Heure TPV Ticket")
        t.line(f"101 AB {when:%d/%m/%y %H:%M} 1 000001")
        t.gap()
        count = _classic_items(t, aisles, sep)
        t.line("===========", col=31, size=BIG)
        t.spread(" TOTAL", f"{_money(printed, sep)} €", size=BIG)
        t.line(f"{count} Article(s)", col=15)
        t.gap(2)
        if discounts:
            _reductions(t, list(discounts), sep, paid)
        _payments(t, payments, change_cents, sep)
        t.line(f"11 / 5{sep}50 {_money(printed, sep)} €")
        t.line("-" * t.width, bold=True)
        t.spread("TOTAL TVA", f"{_money(printed, sep)} €", bold=True)
        _loyalty(t, sep)
        t.line(" Opérateur Date Heure TPV Ticket")
        t.line(f"101 AB {when:%d/%m/%y %H:%M} 1 000001")
    return t.flush(path)


def make_superu_slip(path: Path, kind: str = "card", amount_cents: int = 1234) -> Path:
    """Ticket CB fictif : `card` (carte bancaire) ou `meal_voucher` (carte titre-restaurant)."""
    t = _Ticket("omnipos")
    t.top = 30
    t.line("CARTE BANCAIRE" if kind == "card" else "CARTE TITRE-RESTAURANT", col=1)
    t.line("SANS CONTACT", col=4)
    t.line("CB" if kind == "card" else "TITRE RESTAURANT")
    t.line("le 08/01/25 a 15:10:30")
    t.line("MAGASIN TEST")
    t.line("************0000")
    t.line("MONTANT")
    t.line(f"{_money(amount_cents)} EUR", col=2)
    t.line("DEBIT")
    t.line("TICKET CLIENT", col=6)
    t.line("A CONSERVER", col=7)
    return t.flush(path)
