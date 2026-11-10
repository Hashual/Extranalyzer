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
_STATEMENT_DATE_RE = re.compile(r"\b(\d{1,2}) (" + "|".join(_MONTHS) + r") (\d{4})\b")
_HEADER_RE = re.compile(r"^(?P<label>.+?) N° (?P<number>\d+) en euros")
_BALANCE_RE = re.compile(r"\bSOLDE (CREDITEUR|DEBITEUR) AU\b")
_BALANCE_KINDS = {"CREDITEUR": 1, "DEBITEUR": -1}
OTHER_ACCOUNTS = "SITUATION DE VOS AUTRES COMPTES"
_FOOTER_RE = re.compile(r"Page \d+")

Word = dict  # mot pdfplumber : clés text, x0, x1, top, ...


@dataclass
class _Line:
    top: float
    words: list[Word]

    @property
    def texts(self) -> list[str]:
        return [w["text"] for w in self.words]

    @property
    def text(self) -> str:
        return " ".join(self.texts)


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

    @property
    def closed(self) -> bool:
        return self.closing is not None


def _columns(line: _Line) -> _Columns | None:
    """Colonnes de montants, repérées par les seuls mots `Débit` et `Crédit` de la ligne."""
    by_text = {w["text"]: w for w in line.words}
    debit, credit = by_text.get("Débit"), by_text.get("Crédit")
    if debit is None or credit is None:
        return None
    return _Columns(debit["x0"], credit["x0"])


def _account_header(line: _Line) -> tuple[str, str] | None:
    """En-tête de compte `<intitulé> N° <numéro> en euros` : (intitulé, numéro)."""
    m = _HEADER_RE.match(line.text)
    return (m["label"], m["number"]) if m else None


def _is_tx_start(texts: list[str]) -> bool:
    """Première ligne d'une transaction : date d'opération puis date de valeur."""
    return len(texts) >= 2 and _is_date(texts[0]) and _is_date(texts[1])


def _is_titles_line(texts: list[str]) -> bool:
    return bool(texts) and texts[0] == "Date" and "Opération" in texts


def _statement_date(pages: list[list[_Line]]) -> date:
    for lines in pages:
        for line in lines:
            m = _STATEMENT_DATE_RE.search(line.text)
            if m:
                try:
                    return date(int(m[3]), _MONTHS[m[2]], int(m[1]))
                except ValueError as exc:
                    raise ParseError(f"date du relevé invalide : {m[0]!r}") from exc
    raise ParseError("date du relevé introuvable")


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

        statement_date = _statement_date(pages)
        sections = self._read_sections(pages)
        if not sections:
            raise ParseError("aucune section de compte trouvée")
        return ParsedDocument(statement_date, [s.build(statement_date) for s in sections])

    def _read_sections(self, pages: list[list[_Line]]) -> list[_SectionBuilder]:
        """Découpe le document en sections de compte.

        Les lignes ne sont rattachées à la section courante que pendant la lecture (`reading`) :
        elle reprend à chaque en-tête de compte (nouveau ou répété) et s'interrompt au changement
        de page, au pied de page `Page N` et au bloc `SITUATION DE VOS AUTRES COMPTES`. Après le
        solde de clôture, plus rien n'est rattaché à la section.
        """
        sections: list[_SectionBuilder] = []
        section: _SectionBuilder | None = None
        for lines in pages:
            cols: _Columns | None = None   # colonnes repérées page par page
            reading = False                # reprend après l'en-tête de compte répété
            for line in lines:
                text = line.text
                if OTHER_ACCOUNTS in text:
                    if section is not None:
                        section.end_tx()
                    reading = False
                    continue
                if _FOOTER_RE.fullmatch(text):
                    reading = False
                    continue
                line_cols, header = _columns(line), _account_header(line)
                if line_cols is not None:
                    cols = line_cols
                if header is not None:
                    label, number = header
                    if section is None or section.account_number != number:
                        if section is not None:
                            section.end_tx()
                        section = _SectionBuilder(label, number)
                        sections.append(section)
                if header is not None or line_cols is not None:
                    reading = True     # en-tête sans intitulé reconnu : section en cours
                    continue
                if not reading or section is None or section.closed:
                    continue
                if cols is None:
                    if _is_tx_start(line.texts):
                        raise ParseError("colonnes Débit/Crédit introuvables sur une page du "
                                         f"compte {section.account_number}")
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

        balance = _BALANCE_RE.search(" ".join(label))
        if balance:
            section.end_tx()
            self._read_balance(balance[1], label, amounts, section)
        elif _is_tx_start(texts):
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
    def _read_balance(kind: str, label: list[str], amounts: list[Word],
                      section: _SectionBuilder) -> None:
        if not amounts:
            raise ParseError(f"ligne de solde sans montant : {' '.join(label)!r}")
        value = parse_amount(amounts[0]["text"]) * _BALANCE_KINDS[kind]
        if section.opening is None:
            section.opening = value
        else:
            section.closing = value
