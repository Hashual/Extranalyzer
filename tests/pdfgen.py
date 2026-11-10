"""Générateur de relevés PDF synthétiques (fixtures de test uniquement, libellés fictifs).

La géométrie par défaut reproduit celle mesurée sur les relevés « RELEVE ET INFORMATIONS
BANCAIRES » (structure seulement, aucun contenu réel). Les abscisses/ordonnées sont exprimées comme
dans pdfplumber (origine en haut à gauche) ; la conversion vers reportlab (origine en bas à gauche)
se fait dans `_Writer`. Toutes les abscisses sont décalées de `x_shift`, et de `next_page_shift` en
plus à partir de la page 2 (comme sur les relevés réels).
"""
from dataclasses import dataclass
from pathlib import Path

from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = 595, 842
FONT, SIZE = "Helvetica", 8        # dates : 10 caractères ≈ 40 pt, comme mesuré
TITLE_SIZE = 11
SMALL_SIZE = 6
LINE_H = 10.8              # pas vertical des lignes du tableau

# --- Page 1 : bloc d'adresse, titre, date du relevé ---------------------------------------------
ADDRESS_X, ADDRESS_TOPS = 118, (60, 72, 84, 96, 200)
ADDRESS_LINES = ("BANQUE TEST", "1 RUE FICTIVE", "00000 VILLE TEST", "TEL 00 00 00 00 00",
                 "Conseiller : M CONSEIL TEST")
OWNER_X, OWNER_TOPS = 303, (150, 162, 174)
OWNER_LINES = ("M TEST TITULAIRE", "2 AVENUE FICTIVE", "00000 VILLE TEST")
SEPARATOR_X, SEPARATOR_TOP, SEPARATOR = 7, 273, "_" * 125     # un seul mot, x 7–563
TITLE_X, TITLE_TOP = 48, 290.5
TITLE_CODE_X, TITLE_CODE = 481, "Code 12345"                    # même ligne que le titre
DATE_X, DATE_TOP = 48, 306.5
PAGE1_TABLE_TOP = 329      # premier en-tête de compte
NEXT_PAGE_TABLE_TOP = 60

# --- Tableau ----------------------------------------------------------------------------------
HEADER_X = 82              # intitulé du compte, `N°` séparé
HOLDER_GAP = 12            # en-tête → ligne TITULAIRE(S)
TITLES_GAP = 17.5          # TITULAIRE(S) → ligne de titres
TITLES_DATE_X, TITLES_VALUE_X, TITLES_OPERATION_X = 63, 98, 161
DEBIT_HDR_X, CREDIT_HDR_X = 409, 480        # sur la ligne de titres
OP_DATE_X = 52             # 11 pt à GAUCHE du mot « Date » des titres
VALUE_DATE_X = 100
LABEL_X = 148
SOLDE_X = 152
REF_X, CLOSING_REF = 53, "Réf : 040"        # devant le solde de clôture
DEBIT_RIGHT, CREDIT_RIGHT = 460, 534        # x1 des montants
IBAN_X, IBAN_GAP = 371, 16
IBAN = "IBAN : 0000 0000 0000 0000 0000 0000 000"
INFO_X, INFO_GAPS = 58, (38, 47, 56)        # paragraphe d'information entre deux sections
INFO_LINES = ("Information fictive sur votre compte", "sans rapport avec les opérations",
              "du relevé de test")
SITUATION_X, OTHER_ROW_X = 77, 60
MAX_TOP = 680              # dernière ligne du tableau (le pied de page est ≥ 50 pt plus bas)

# --- Références techniques en marge -------------------------------------------------------------
MARGIN_REF_X, MARGIN_REF = 6, "R1"                     # x 6–14
LONG_REF_X, LONG_REF, LONG_REF_SIZE = 0, "KV.20240411.000001", 3   # x 0–29

# --- Pied de page -------------------------------------------------------------------------------
FOOTER_X, FOOTER_TOP = 48, 730
DEFAULT_FOOTER = ("Banque TEST societe cooperative a capital variable",
                  "Siege social 1 rue fictive 00000 VILLE TEST")
PAGE_LINE_TOP, PAGE_LINE_X, PAGE_LINE = 781, 224, "Document fictif de test reserve"
PAGE_NUMBER_X = 505
BELOW_X, BELOW_TOPS = 97, (791, 798, 805, 812)
BELOW_LINES = ("Banque TEST : Crédit et services fictifs", "Mentions fictives numero un",
               "Mentions fictives numero deux", "Mentions fictives numero trois")


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
    next_page_shift: float = 10
    amounts_drawn_last: bool = True
    repeat_columns: bool = True
    columns_offset: float = 0
    margin_refs: bool = True
    balance_prefix: str = ""
    footer_lines: tuple[str, ...] = DEFAULT_FOOTER
    footer_gap: float | None = None


class _Writer:
    def __init__(self, path: Path, layout: _Layout):
        self.c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
        self.layout = layout
        self.page = 1
        self.top = PAGE1_TABLE_TOP
        self.rows = 0               # lignes de tableau sur la page courante
        self.last_row_top = PAGE1_TABLE_TOP
        self.pending_amounts: list[tuple[float, float, str]] = []

    @property
    def dx(self) -> float:
        return self.layout.x_shift + (self.layout.next_page_shift if self.page > 1 else 0)

    def text(self, x: float, top: float, s: str, size: float = SIZE) -> None:
        self.c.setFont(FONT, size)
        self.c.drawString(x + self.dx, PAGE_H - top - size, s)

    def margin_ref(self, top: float, long: bool = False) -> None:
        if not self.layout.margin_refs:
            return
        if long:
            self.text(LONG_REF_X, top, LONG_REF, size=LONG_REF_SIZE)
        else:
            self.text(MARGIN_REF_X, top, MARGIN_REF, size=SMALL_SIZE)

    def amount(self, column: str, s: str) -> None:
        right = (DEBIT_RIGHT if column == "debit" else CREDIT_RIGHT) + self.dx
        if self.layout.amounts_drawn_last:
            self.pending_amounts.append((right, self.top, s))
        else:
            self._draw_amount(right, self.top, s)

    def _draw_amount(self, right: float, top: float, s: str) -> None:
        self.c.setFont(FONT, SIZE)
        self.c.drawRightString(right, PAGE_H - top - SIZE, s)

    def row_done(self, long_ref: bool = False) -> None:
        """Fin d'une ligne de tableau (solde, transaction, continuation)."""
        self.margin_ref(self.top, long=long_ref)
        self.last_row_top = self.top
        self.top += LINE_H
        self.rows += 1

    def fits(self, n_rows: int, rows_per_page: int, extra: float = 0) -> bool:
        if self.rows == 0:
            return True
        return (self.rows + n_rows <= rows_per_page
                and self.top + extra + (n_rows - 1) * LINE_H <= MAX_TOP)

    def page1_header(self, statement_date: str) -> None:
        for top, line in zip(ADDRESS_TOPS, ADDRESS_LINES):
            self.text(ADDRESS_X, top, line)
        for top, line in zip(OWNER_TOPS, OWNER_LINES):
            self.text(OWNER_X, top, line)
        self.text(SEPARATOR_X, SEPARATOR_TOP, SEPARATOR)
        self.text(TITLE_X, TITLE_TOP, "RELEVE ET INFORMATIONS BANCAIRES", size=TITLE_SIZE)
        self.text(TITLE_CODE_X, TITLE_TOP, TITLE_CODE)
        self.text(DATE_X, DATE_TOP, statement_date)

    def end_page(self) -> None:
        for right, top, s in self.pending_amounts:
            self._draw_amount(right, top, s)
        self.pending_amounts.clear()
        gap = self.layout.footer_gap
        top = FOOTER_TOP if gap is None else self.last_row_top + gap
        for line in self.layout.footer_lines:
            self.text(FOOTER_X, top, line)
            self.margin_ref(top)
            top += 10
        self.text(PAGE_LINE_X, PAGE_LINE_TOP, PAGE_LINE)
        self.text(PAGE_NUMBER_X, PAGE_LINE_TOP, f"Page {self.page}")
        for top, line in zip(BELOW_TOPS, BELOW_LINES):
            self.text(BELOW_X, top, line, size=SMALL_SIZE)
        self.c.showPage()
        self.page += 1
        self.top = NEXT_PAGE_TABLE_TOP
        self.last_row_top = NEXT_PAGE_TABLE_TOP
        self.rows = 0

    def section_header(self, header: str, columns: bool = True) -> None:
        """En-tête de compte, ligne TITULAIRE(S), ligne de titres (avec Débit/Crédit EUROS)."""
        self.text(HEADER_X, self.top, header)
        self.margin_ref(self.top)
        self.top += HOLDER_GAP
        self.text(HEADER_X, self.top, "TITULAIRE(S) : M TEST TITULAIRE")
        self.top += TITLES_GAP
        self.text(TITLES_DATE_X, self.top, "Date")
        self.text(TITLES_VALUE_X, self.top, "Date valeur")
        self.text(TITLES_OPERATION_X, self.top, "Opération")
        if columns:
            cols_top = self.top + self.layout.columns_offset
            self.text(DEBIT_HDR_X, cols_top, "Débit EUROS")
            self.text(CREDIT_HDR_X, cols_top, "Crédit EUROS")
        self.top += LINE_H + 1

    def save(self) -> None:
        self.end_page()
        self.c.save()


HEADER_BLOCK_H = HOLDER_GAP + TITLES_GAP + LINE_H + 1


def _balance_row(w: _Writer, balance: tuple[str, str, str], closing: bool) -> None:
    kind, day, amount = balance
    if closing:
        w.text(REF_X, w.top, CLOSING_REF)
    w.text(SOLDE_X, w.top, f"{w.layout.balance_prefix} SOLDE {kind} AU {day}".strip())
    w.amount("debit" if kind == "DEBITEUR" else "credit", amount)
    w.row_done(long_ref=not closing)


def _tx_rows(w: _Writer, tx: FTx) -> None:
    for i, label in enumerate(tx.label):
        if i == 0:
            w.text(OP_DATE_X, w.top, tx.op)
            w.text(VALUE_DATE_X, w.top, tx.val)
        w.text(LABEL_X, w.top, label)
        if i == tx.amount_line:
            if tx.debit is not None:
                w.amount("debit", tx.debit)
            if tx.credit is not None:
                w.amount("credit", tx.credit)
        w.row_done()


def _between_sections(w: _Writer) -> None:
    """IBAN sous le solde de clôture puis paragraphe d'information."""
    closing_top = w.last_row_top
    w.text(IBAN_X, closing_top + IBAN_GAP, IBAN)
    for gap, line in zip(INFO_GAPS, INFO_LINES):
        w.text(INFO_X, closing_top + IBAN_GAP + gap, line)
    w.top = closing_top + IBAN_GAP + INFO_GAPS[-1] + 18


def make_statement(path: Path, statement_date: str, sections: list[FSection], *,
                   other_accounts: list[tuple[str, str]] = (), rows_per_page: int = 25,
                   x_shift: float = 0, next_page_shift: float = 10, amounts_drawn_last: bool = True,
                   repeat_columns: bool = True, columns_offset: float = 0, margin_refs: bool = True,
                   balance_prefix: str = "", footer_lines: list[str] = DEFAULT_FOOTER,
                   footer_gap: float | None = None) -> Path:
    """Dessine un relevé synthétique à la géométrie des relevés réels.

    - Page 1 : bloc d'adresse, séparateur, titre « RELEVE ET INFORMATIONS BANCAIRES » (et un code
      sur la même ligne), puis `statement_date` en x=48.
    - Par section : en-tête de compte (x=82), ligne TITULAIRE(S), ligne de titres portant aussi
      `Débit EUROS` / `Crédit EUROS` (`columns_offset` la décale verticalement), solde d'ouverture,
      transactions (dates en x=52, à gauche du mot « Date » des titres en x=63), solde de clôture
      précédé de `Réf : 040`, ligne IBAN, puis paragraphe d'information avant la section suivante
      (qui commence en milieu de page).
    - Saut de page dès que `rows_per_page` lignes de tableau seraient dépassées (une transaction
      multi-ligne n'est jamais coupée) ; l'en-tête de compte et les titres sont répétés
      (`repeat_header`, `repeat_columns=False` : titres sans `Débit`/`Crédit`).
    - Puis le bloc `SITUATION DE VOS AUTRES COMPTES en euros au …` (`other_accounts` :
      (libellé, montant en un seul mot)).
    - Pied de page : `footer_lines` en x=48 (à partir de top 730, ou `footer_gap` pt sous la
      dernière ligne du tableau), puis une ligne « … Page N », puis des mentions dont une contient
      `Crédit`.
    - `x_shift` décale toute la page ; les pages 2+ sont décalées de `next_page_shift` en plus.
    - `amounts_drawn_last` : les montants sont écrits dans le flux après tous les libellés de la page.
    - `margin_refs` : références techniques en marge (x 6–14, et une longue en x 0–29 sur la ligne
      du solde d'ouverture) sur les lignes du tableau et du pied de page.
    - `balance_prefix` : mot(s) écrit(s) avant `SOLDE …` sur les lignes de solde.
    """
    for section in sections:
        for tx in section.txs:
            assert tx.label, f"FTx sans libellé : {tx!r}"
            assert 0 <= tx.amount_line < len(tx.label), f"amount_line hors libellé : {tx!r}"
    path = Path(path)
    w = _Writer(path, _Layout(x_shift, next_page_shift, amounts_drawn_last, repeat_columns,
                              columns_offset, margin_refs, balance_prefix, tuple(footer_lines),
                              footer_gap))
    w.page1_header(statement_date)

    for n, section in enumerate(sections):
        if n > 0:
            _between_sections(w)
        if not w.fits(1, rows_per_page, extra=HEADER_BLOCK_H):
            w.end_page()
        w.section_header(section.header)
        items = [section.opening, *section.txs, section.closing]
        for i, item in enumerate(items):
            size = len(item.label) if isinstance(item, FTx) else 1
            if not w.fits(size, rows_per_page):
                w.end_page()
                w.section_header(section.repeat_header or section.header, columns=repeat_columns)
            if isinstance(item, FTx):
                _tx_rows(w, item)
            else:
                _balance_row(w, item, closing=i == len(items) - 1)

    if other_accounts:
        if sections:
            _between_sections(w)
        if not w.fits(1 + len(other_accounts), rows_per_page):
            w.end_page()
        w.text(SITUATION_X, w.top, f"SITUATION DE VOS AUTRES COMPTES en euros au {statement_date}")
        w.row_done()
        for label, amount in other_accounts:
            if not w.fits(1, rows_per_page):
                w.end_page()
            w.text(OTHER_ROW_X, w.top, label)
            w.amount("credit", amount)
            w.row_done()

    w.save()
    return path
