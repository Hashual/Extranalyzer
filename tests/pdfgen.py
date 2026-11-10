"""Générateur de relevés PDF synthétiques (fixtures de test uniquement, libellés fictifs).

Reproduit la mise en page mesurée sur les relevés « RELEVE ET INFORMATIONS BANCAIRES ».
Les abscisses/ordonnées ci-dessous sont exprimées comme dans pdfplumber (origine en haut à
gauche) ; la conversion vers reportlab (origine en bas à gauche) se fait dans `_Writer`.
"""
from dataclasses import dataclass
from pathlib import Path

from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = 595, 842
FONT, SIZE = "Helvetica", 9
TITLE_SIZE = 11
LINE_H = 12
MARGIN_REF_X = 10      # référence technique en marge, à gauche de la colonne date
MARGIN_REF_SIZE = 4
MARGIN_REF = "KV.0000.000"

DATE_X = 40            # colonne « Date » (date d'opération)
VALUE_DATE_X = 95      # colonne « Date valeur »
LABEL_X = 150          # colonne « Opération » (libellé)
DEBIT_HDR_X = 409      # x0 du mot « Débit »
CREDIT_HDR_X = 480     # x0 du mot « Crédit »
DEBIT_RIGHT_OFFSET = 30    # montants débit : x1 ≈ Débit.x1 + 30
CREDIT_RIGHT_OFFSET = 105  # montants crédit : x1 ≈ Débit.x1 + 105

TITLE_TOP = 40
DATE_TOP = 60
TABLE_TOP = 90
FOOTER_TOP = 815
MAX_TOP = 780          # garde-fou physique : jamais d'écriture sous cette ordonnée
SECTION_GAP = 18


@dataclass
class FTx:
    op: str
    val: str
    label: list[str]              # 1re ligne + continuations
    debit: str | None = None
    credit: str | None = None
    amount_line: int = 0          # index de la ligne de libellé portant le montant


@dataclass
class FSection:
    header: str                   # ex. "C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)"
    opening: tuple[str, str, str]  # ("CREDITEUR"|"DEBITEUR", "11/03/2024", "271,50")
    closing: tuple[str, str, str]
    txs: list[FTx]
    repeat_header: str | None = None   # intitulé répété sur les pages suivantes (défaut : header)



@dataclass
class _Layout:
    """Options de mise en page de `make_statement` (voir sa docstring)."""

    x_shift: float = 0
    amounts_drawn_last: bool = True
    repeat_columns: bool = True
    columns_offset: float = 5
    margin_refs: bool = False
    balance_prefix: str = ""
    footer_lines: tuple[str, ...] = ()
    footer_gap: float = 36


class _Writer:
    def __init__(self, path: Path, layout: _Layout):
        self.c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
        self.layout = layout
        self.page = 1
        self.top = TABLE_TOP
        self.rows = 0               # lignes de tableau sur la page courante
        self.pending_amounts: list[tuple[float, float, str]] = []
        debit_x1 = DEBIT_HDR_X + layout.x_shift + stringWidth("Débit", FONT, SIZE)
        self.debit_right = debit_x1 + DEBIT_RIGHT_OFFSET
        self.credit_right = debit_x1 + CREDIT_RIGHT_OFFSET
        # Le pied de page (lignes + `Page N`) doit tenir sous la dernière ligne du tableau.
        n_footer = len(layout.footer_lines)
        self.max_top = MAX_TOP if not n_footer else min(
            MAX_TOP, FOOTER_TOP - layout.footer_gap - LINE_H * (n_footer - 1))

    @staticmethod
    def _y(top: float, size: float = SIZE) -> float:
        return PAGE_H - top - size

    def text(self, x: float, top: float, s: str, size: float = SIZE) -> None:
        self.c.setFont(FONT, size)
        self.c.drawString(x, self._y(top, size), s)

    def margin_ref(self, top: float) -> None:
        if self.layout.margin_refs:
            self.text(MARGIN_REF_X, top, MARGIN_REF, size=MARGIN_REF_SIZE)

    def amount(self, column: str, s: str) -> None:
        right = self.debit_right if column == "debit" else self.credit_right
        if self.layout.amounts_drawn_last:
            self.pending_amounts.append((right, self.top, s))
        else:
            self._draw_amount(right, self.top, s)

    def _draw_amount(self, right: float, top: float, s: str) -> None:
        self.c.setFont(FONT, SIZE)
        self.c.drawRightString(right, self._y(top), s)

    def next_line(self, table_row: bool = True) -> None:
        self.margin_ref(self.top)
        self.top += LINE_H
        if table_row:
            self.rows += 1

    def fits(self, n_rows: int, rows_per_page: int) -> bool:
        if self.rows == 0:
            return True
        return self.rows + n_rows <= rows_per_page and self.top + n_rows * LINE_H <= self.max_top

    def end_page(self) -> None:
        for right, top, s in self.pending_amounts:
            self._draw_amount(right, top, s)
        self.pending_amounts.clear()
        top = self.top - LINE_H + self.layout.footer_gap    # sous la dernière ligne écrite
        for line in self.layout.footer_lines:
            self.text(DATE_X, top, line)
            self.margin_ref(top)
            top += LINE_H
        self.text(280, FOOTER_TOP, f"Page {self.page}")
        self.margin_ref(FOOTER_TOP)
        self.c.showPage()
        self.page += 1
        self.top = TABLE_TOP
        self.rows = 0

    def section_header(self, header: str, columns: bool = True) -> None:
        self.text(DATE_X, self.top, header)
        if columns:
            cols_top = self.top + self.layout.columns_offset
            self.text(DEBIT_HDR_X + self.layout.x_shift, cols_top, "Débit EUROS")
            self.text(CREDIT_HDR_X + self.layout.x_shift, cols_top, "Crédit EUROS")
            if self.layout.columns_offset:
                self.margin_ref(cols_top)
        self.next_line(table_row=False)
        self.text(DATE_X, self.top, "Date")
        self.text(VALUE_DATE_X, self.top, "Date valeur")
        self.text(LABEL_X, self.top, "Opération")
        self.next_line(table_row=False)

    def save(self) -> None:
        self.end_page()
        self.c.save()


def _balance_row(w: _Writer, balance: tuple[str, str, str]) -> None:
    kind, day, amount = balance
    w.text(LABEL_X, w.top, f"{w.layout.balance_prefix} SOLDE {kind} AU {day}".strip())
    w.amount("debit" if kind == "DEBITEUR" else "credit", amount)
    w.next_line()


def _tx_rows(w: _Writer, tx: FTx) -> None:
    for i, label in enumerate(tx.label):
        if i == 0:
            w.text(DATE_X, w.top, tx.op)
            w.text(VALUE_DATE_X, w.top, tx.val)
        w.text(LABEL_X, w.top, label)
        if i == tx.amount_line:
            if tx.debit is not None:
                w.amount("debit", tx.debit)
            if tx.credit is not None:
                w.amount("credit", tx.credit)
        w.next_line()


def make_statement(path: Path, statement_date: str, sections: list[FSection], *,
                   other_accounts: list[tuple[str, str]] = (), rows_per_page: int = 25,
                   x_shift: float = 0, amounts_drawn_last: bool = True, repeat_columns: bool = True,
                   columns_offset: float = 5, margin_refs: bool = False, balance_prefix: str = "",
                   footer_lines: list[str] = (), footer_gap: float = 36) -> Path:
    """Dessine un relevé synthétique.

    - Page 1 : titre « RELEVE ET INFORMATIONS BANCAIRES » et `statement_date`.
    - Par section : en-tête de compte, `Débit EUROS` / `Crédit EUROS`, ligne de titres, solde
      d'ouverture, transactions, solde de clôture.
    - Saut de page dès que `rows_per_page` lignes de tableau seraient dépassées (une transaction
      multi-ligne n'est jamais coupée) ; l'en-tête de compte et les titres sont répétés.
    - Puis le bloc `SITUATION DE VOS AUTRES COMPTES` (`other_accounts` : (libellé, montant crédit)).
    - Pied de page sur chaque page : `footer_lines` (mentions, à `footer_gap` pt sous la dernière
      ligne écrite, colonne date) puis `Page N`.
    - `x_shift` décale les colonnes Débit/Crédit (en-têtes et montants).
    - `columns_offset` : écart vertical (pt) entre l'intitulé du compte et `Débit EUROS Crédit
      EUROS` ; comme sur les relevés réels, ils sont par défaut sur deux lignes pdfplumber
      distinctes (0 = même ligne).
    - `amounts_drawn_last` : les montants sont écrits dans le flux après tous les libellés de la page.
    - `repeat_columns=False` : l'en-tête répété sur une nouvelle page n'a pas `Débit`/`Crédit`.
    - `margin_refs` : référence technique (`KV.0000.000`) en marge gauche de chaque ligne (en-tête,
      colonnes, titres, tableau, pied de page).
    - `balance_prefix` : mot(s) écrit(s) avant `SOLDE …` sur les lignes de solde.
    """
    for section in sections:
        for tx in section.txs:
            assert tx.label, f"FTx sans libellé : {tx!r}"
            assert 0 <= tx.amount_line < len(tx.label), f"amount_line hors libellé : {tx!r}"
    path = Path(path)
    w = _Writer(path, _Layout(x_shift, amounts_drawn_last, repeat_columns, columns_offset,
                              margin_refs, balance_prefix, tuple(footer_lines), footer_gap))
    w.text(DATE_X, TITLE_TOP, "RELEVE ET INFORMATIONS BANCAIRES", size=TITLE_SIZE)
    w.text(400, DATE_TOP, statement_date)

    for n, section in enumerate(sections):
        if n > 0:
            w.top += SECTION_GAP
        if not w.fits(3, rows_per_page):
            w.end_page()
        w.section_header(section.header)
        for item in [section.opening, *section.txs, section.closing]:
            size = len(item.label) if isinstance(item, FTx) else 1
            if not w.fits(size, rows_per_page):
                w.end_page()
                w.section_header(section.repeat_header or section.header, columns=repeat_columns)
            if isinstance(item, FTx):
                _tx_rows(w, item)
            else:
                _balance_row(w, item)

    if other_accounts:
        w.top += SECTION_GAP
        if not w.fits(1 + len(other_accounts), rows_per_page):
            w.end_page()
        w.text(DATE_X, w.top, "SITUATION DE VOS AUTRES COMPTES")
        w.next_line()
        for label, amount in other_accounts:
            if not w.fits(1, rows_per_page):
                w.end_page()
            w.text(DATE_X, w.top, label)
            w.amount("credit", amount)
            w.next_line()

    w.save()
    return path
