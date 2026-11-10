"""Parseur par coordonnées des relevés « RELEVE ET INFORMATIONS BANCAIRES ».

Le flux du PDF ne suit pas l'ordre visuel (les montants sont stockés après les libellés) : on
reconstruit les lignes à partir des coordonnées des mots (pdfplumber, origine en haut à gauche).
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber

from app.importer.amounts import is_amount, parse_amount
from app.model import ParsedDocument, ParsedSection, ParsedTransaction, ParseError

TITLE = "RELEVE ET INFORMATIONS BANCAIRES"
LINE_TOLERANCE = 2         # pt : mots d'une même ligne visuelle (écart de `top`)
AMOUNT_ZONE_MARGIN = 40    # pt : zone des montants = x0 >= Débit.x0 - 40
DATE_COLUMN_MARGIN = 5     # pt : mots ignorés à gauche de la colonne date - 5

_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")
_MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}
_BALANCE_KINDS = {"CREDITEUR": 1, "DEBITEUR": -1}

Word = dict  # mot pdfplumber : clés text, x0, x1, top, ...


@dataclass
class _Line:
    top: float
    words: list[Word]

    @property
    def texts(self) -> list[str]:
        return [w["text"] for w in self.words]


def _group_lines(words: list[Word]) -> list[_Line]:
    lines: list[_Line] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and w["top"] - lines[-1].top <= LINE_TOLERANCE:
            lines[-1].words.append(w)
        else:
            lines.append(_Line(w["top"], [w]))
    for line in lines:
        line.words.sort(key=lambda w: w["x0"])
    return lines


def _is_date(text: str) -> bool:
    return _DATE_RE.fullmatch(text) is not None


def _to_date(text: str) -> date:
    return datetime.strptime(text, "%d/%m/%Y").date()


@dataclass
class _Columns:
    """Géométrie du tableau, repérée à partir des mots d'en-tête de la page."""

    debit_x0: float
    credit_x0: float
    date_x0: float | None = None   # connu après la ligne « Date Date valeur Opération »

    def in_amount_zone(self, w: Word) -> bool:
        return w["x0"] >= self.debit_x0 - AMOUNT_ZONE_MARGIN

    def is_zone_amount(self, w: Word) -> bool:
        return self.in_amount_zone(w) and is_amount(w["text"])

    def signed(self, w: Word) -> Decimal:
        value = parse_amount(w["text"])
        return -value if w["x1"] < self.credit_x0 else value

    def in_bounds(self, w: Word) -> bool:
        return self.date_x0 is None or w["x0"] >= self.date_x0 - DATE_COLUMN_MARGIN


@dataclass
class _TxBuilder:
    op_date: date
    value_date: date
    label: list[str]
    amount: Decimal | None = None

    def build(self) -> ParsedTransaction:
        if self.amount is None:
            raise ParseError(f"transaction sans montant : {' '.join(self.label)!r}")
        return ParsedTransaction(self.op_date, self.value_date, " ".join(self.label), self.amount)


@dataclass
class _SectionBuilder:
    account_label: str
    account_number: str
    opening: Decimal | None = None
    closing: Decimal | None = None
    transactions: list[ParsedTransaction] = field(default_factory=list)
    current: _TxBuilder | None = None

    def end_tx(self) -> None:
        if self.current is not None:
            self.transactions.append(self.current.build())
            self.current = None

    def build(self, statement_date: date) -> ParsedSection:
        self.end_tx()
        if self.opening is None or self.closing is None:
            raise ParseError(f"soldes introuvables pour le compte {self.account_number}")
        return ParsedSection(self.account_label, self.account_number, statement_date,
                             self.opening, self.closing, self.transactions)


def _section_header(line: _Line) -> tuple[_Columns, str, str] | None:
    """Ligne d'en-tête de compte : intitulé, `N° <numéro>`, puis `Débit EUROS Crédit EUROS`."""
    by_text = {w["text"]: w for w in line.words}
    debit, credit = by_text.get("Débit"), by_text.get("Crédit")
    if debit is None or credit is None:
        return None
    cols = _Columns(debit["x0"], credit["x0"])
    texts = [w["text"] for w in line.words if not cols.in_amount_zone(w)]
    if "N°" not in texts:
        return None
    i = texts.index("N°")
    if i + 1 >= len(texts):
        return None
    return cols, " ".join(texts[:i]), texts[i + 1]


def _is_titles_line(texts: list[str]) -> bool:
    return bool(texts) and texts[0] == "Date" and "Opération" in texts


def _statement_date(lines: list[_Line]) -> date | None:
    for line in lines:
        t = line.texts
        for i in range(len(t) - 2):
            month = _MONTHS.get(t[i + 1].lower())
            if month and t[i].isdigit() and re.fullmatch(r"\d{4}", t[i + 2]):
                return date(int(t[i + 2]), month, int(t[i]))
    return None


class CreditMutuelParser:
    name = "creditmutuel"

    def can_parse(self, pdf_path: Path) -> bool:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                if not pdf.pages:
                    return False
                page = pdf.pages[0]
                texts = {w["text"] for w in page.extract_words()}
                return TITLE in (page.extract_text() or "") and {"Débit", "Crédit"} <= texts
        except Exception:  # fichier illisible ou non PDF : ce n'est pas notre format
            return False

    def parse(self, pdf_path: Path) -> ParsedDocument:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                pages = [_group_lines(page.extract_words()) for page in pdf.pages]
        except Exception as exc:
            raise ParseError(f"PDF illisible : {exc}") from exc
        if not pages:
            raise ParseError("PDF sans page")

        statement_date = _statement_date(pages[0])
        if statement_date is None:
            raise ParseError("date du relevé introuvable")

        sections = self._read_sections(pages)
        if not sections:
            raise ParseError("aucune section de compte trouvée")
        return ParsedDocument(statement_date, [s.build(statement_date) for s in sections])

    def _read_sections(self, pages: list[list[_Line]]) -> list[_SectionBuilder]:
        sections: list[_SectionBuilder] = []
        section: _SectionBuilder | None = None
        for lines in pages:
            cols: _Columns | None = None   # colonnes repérées page par page
            for line in lines:
                header = _section_header(line)
                if header is not None:
                    cols, label, number = header
                    if section is not None:
                        section.end_tx()
                    section = _SectionBuilder(label, number)
                    sections.append(section)
                    continue
                if section is None or cols is None:
                    continue
                self._read_line(line, cols, section)
        return sections

    def _read_line(self, line: _Line, cols: _Columns, section: _SectionBuilder) -> None:
        words = [w for w in line.words if cols.in_bounds(w)]
        texts = [w["text"] for w in words]
        if not words:
            return
        if _is_titles_line(texts):
            cols.date_x0 = words[0]["x0"]
            return
        amounts = [w for w in words if cols.is_zone_amount(w)]
        label = [w["text"] for w in words if not cols.is_zone_amount(w)]

        if texts[0] == "SOLDE":
            section.end_tx()
            self._read_balance(texts, amounts, cols, section)
        elif len(texts) >= 2 and _is_date(texts[0]) and _is_date(texts[1]):
            section.end_tx()
            section.current = _TxBuilder(_to_date(texts[0]), _to_date(texts[1]), label[2:],
                                         cols.signed(amounts[0]) if amounts else None)
        elif section.current is not None and any(not cols.in_amount_zone(w) for w in words):
            section.current.label.extend(label)
            if section.current.amount is None and amounts:
                section.current.amount = cols.signed(amounts[0])
        else:
            section.end_tx()

    @staticmethod
    def _read_balance(texts: list[str], amounts: list[Word], cols: _Columns,
                      section: _SectionBuilder) -> None:
        if len(texts) < 2 or texts[1] not in _BALANCE_KINDS or not amounts:
            raise ParseError(f"ligne de solde illisible : {' '.join(texts)!r}")
        value = parse_amount(amounts[0]["text"]) * _BALANCE_KINDS[texts[1]]
        if section.opening is None:
            section.opening = value
        else:
            section.closing = value
