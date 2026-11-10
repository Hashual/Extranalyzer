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
LINE_TOLERANCE = 2          # pt : mots d'une même ligne visuelle (écart de `top`)
AMOUNT_ZONE_MARGIN = 40     # pt : zone des montants = x0 >= Débit.x0 - 40
MARGIN_BOUND = 20           # pt : mots en marge = x1 < (mot « Date » des titres).x0 - 20
MAX_CONTINUATION_GAP = 24   # pt : écart vertical maximal entre deux lignes d'une même transaction

_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")
_MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}
_STATEMENT_DATE_RE = re.compile(r"\b(\d{1,2}) (" + "|".join(_MONTHS) + r") (\d{4})\b",
                                re.IGNORECASE)
_BALANCE_RE = re.compile(r"\bSOLDE (CREDITEUR|DEBITEUR) AU \d{2}/\d{2}/\d{4}\b")
_BALANCE_KINDS = {"CREDITEUR": 1, "DEBITEUR": -1}
OTHER_ACCOUNTS = "SITUATION DE VOS AUTRES COMPTES"
_PAGE_NUMBER_RE = re.compile(r"\bPage \d+\b")   # n'importe où sur la ligne
# Mentions de bas de page : garde secondaire (la garde principale est MAX_CONTINUATION_GAP).
_FOOTER_TEXT_RE = re.compile(r"^(Sous réserve|Information sur la protection|CAISSE DE CREDIT MUTUEL"
                             r"|Médiateur|\(GE\)|\(GD\)|Remarque|www\.)", re.IGNORECASE)

Word = dict  # mot pdfplumber : clés text, x0, x1, top, ...


@dataclass
class _Line:
    top: float
    words: list[Word]
    raw_text: str = ""   # texte complet, avant filtrage de la marge

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
        line.raw_text = line.text
    return lines


def _is_titles_line(texts: list[str]) -> bool:
    """Ligne de titres « Date  Date valeur  Opération » (une référence peut la précéder)."""
    return "Date" in texts and "Opération" in texts


@dataclass
class _Page:
    """Lignes d'une page, sans les références techniques en marge.

    Les dates des transactions commencent ~10 pt à GAUCHE du mot « Date » des titres : seuls les
    mots qui se terminent avant `Date.x0 - MARGIN_BOUND` sont écartés. `raw_lines` garde tout.
    """

    width: float
    lines: list[_Line]
    raw_lines: list[_Line]

    @classmethod
    def from_words(cls, width: float, words: list[Word]) -> "_Page":
        lines = _group_lines(words)
        titles = next((line for line in lines if _is_titles_line(line.texts)), None)
        if titles is None:
            return cls(width, lines, lines)
        bound = next(w for w in titles.words if w["text"] == "Date")["x0"] - MARGIN_BOUND
        visible = [_Line(line.top, [w for w in line.words if w["x1"] >= bound], line.raw_text)
                   for line in lines]
        return cls(width, [line for line in visible if line.words], lines)


def _is_date(text: str) -> bool:
    return _DATE_RE.fullmatch(text) is not None


def _to_date(text: str) -> date:
    return datetime.strptime(text, "%d/%m/%Y").date()


@dataclass
class _Columns:
    """Colonnes de montants de la page, repérées sur `Débit EUROS Crédit EUROS`."""

    debit_x0: float
    credit_x0: float

    def in_amount_zone(self, w: Word) -> bool:
        return w["x0"] >= self.debit_x0 - AMOUNT_ZONE_MARGIN

    def is_zone_amount(self, w: Word) -> bool:
        return self.in_amount_zone(w) and is_amount(w["text"])

    def signed(self, w: Word) -> Decimal:
        value = parse_amount(w["text"])
        return -value if w["x1"] < self.credit_x0 else value


@dataclass
class _TxBuilder:
    op_date: date
    value_date: date
    label: list[str]
    amount: Decimal | None
    last_top: float          # ordonnée de la dernière ligne rattachée à la transaction

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

    @property
    def closed(self) -> bool:
        return self.closing is not None

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


def _columns(line: _Line, page_width: float) -> _Columns | None:
    """`Débit EUROS` puis `Crédit EUROS`, dans cet ordre, dans la moitié droite de la page."""
    pairs = list(zip(line.words, line.words[1:]))
    debit = next((w for w, nxt in pairs if w["text"] == "Débit" and nxt["text"] == "EUROS"), None)
    credit = next((w for w, nxt in pairs if w["text"] == "Crédit" and nxt["text"] == "EUROS"), None)
    if debit is None or credit is None:
        return None
    if not page_width / 2 <= debit["x0"] < credit["x0"]:
        return None
    return _Columns(debit["x0"], credit["x0"])


def _account_header(line: _Line) -> tuple[str, str] | None:
    """En-tête de compte `<intitulé> N° <numéro> en euros …` : (intitulé, numéro)."""
    t = line.texts
    for i in range(1, len(t) - 3):
        if t[i] == "N°" and t[i + 1].isdigit() and t[i + 2:i + 4] == ["en", "euros"]:
            return " ".join(t[:i]), t[i + 1]
    return None


def _is_tx_start(texts: list[str]) -> bool:
    """Première ligne d'une transaction : date d'opération puis date de valeur."""
    return len(texts) >= 2 and _is_date(texts[0]) and _is_date(texts[1])


def _is_footer_text(line: _Line) -> bool:
    return bool(_FOOTER_TEXT_RE.match(line.text) or _FOOTER_TEXT_RE.match(line.raw_text))


def _statement_date(first_page: _Page) -> date:
    """Première date `<j> <mois> <aaaa>` de la page 1, marge comprise."""
    for line in first_page.raw_lines:
        m = _STATEMENT_DATE_RE.search(line.text)
        if m:
            try:
                return date(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
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
                pages = [_Page.from_words(p.width, p.extract_words()) for p in pdf.pages]
        except Exception as exc:
            raise ParseError(f"PDF illisible : {exc}") from exc
        if not pages:
            raise ParseError("PDF sans page")

        statement_date = _statement_date(pages[0])
        sections = self._read_sections(pages)
        if not sections:
            raise ParseError("aucune section de compte trouvée")
        return ParsedDocument(statement_date, [s.build(statement_date) for s in sections])

    def _read_sections(self, pages: list[_Page]) -> list[_SectionBuilder]:
        """Découpe le document en sections de compte.

        Les lignes ne sont rattachées à la section courante que pendant la lecture (`reading`) :
        elle reprend à chaque en-tête de compte (nouveau ou répété) ou ligne `Débit EUROS Crédit
        EUROS`, et s'interrompt au changement de page, au pied de page `Page N` et au bloc
        `SITUATION DE VOS AUTRES COMPTES`. Après le solde de clôture, plus rien n'est rattaché à
        la section. Une transaction ne se poursuit pas sur la page suivante.
        """
        sections: list[_SectionBuilder] = []
        section: _SectionBuilder | None = None
        for page in pages:
            if section is not None:
                section.end_tx()
            cols: _Columns | None = None   # colonnes repérées page par page
            reading = False                # reprend après l'en-tête de compte répété
            for line in page.lines:
                text = line.text
                if OTHER_ACCOUNTS in text:
                    if section is not None:
                        section.end_tx()
                    reading = False
                    continue
                if _PAGE_NUMBER_RE.search(text):
                    reading = False
                    continue
                line_cols, header = _columns(line, page.width), _account_header(line)
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
                    reading = True     # colonnes sans intitulé reconnu : section en cours
                    continue
                if section is None or section.closed:
                    continue
                if not reading or cols is None:
                    if _is_tx_start(line.texts):
                        raise ParseError("en-tête de compte/colonnes introuvables sur une page du "
                                         f"compte {section.account_number}")
                    continue
                self._read_line(line, cols, section)
        return sections

    def _read_line(self, line: _Line, cols: _Columns, section: _SectionBuilder) -> None:
        words, texts = line.words, line.texts
        if _is_titles_line(texts):
            return
        amounts = [w for w in words if cols.is_zone_amount(w)]
        label = [w["text"] for w in words if not cols.is_zone_amount(w)]
        current = section.current

        balance = _BALANCE_RE.search(" ".join(label))
        if balance:
            section.end_tx()
            self._read_balance(balance[1], label, amounts, section)
        elif _is_tx_start(texts):
            section.end_tx()
            section.current = _TxBuilder(_to_date(texts[0]), _to_date(texts[1]), label[2:],
                                         cols.signed(amounts[0]) if amounts else None, line.top)
        elif (current is not None and any(not cols.in_amount_zone(w) for w in words)
              and line.top - current.last_top <= MAX_CONTINUATION_GAP
              and not _is_footer_text(line)):
            current.label.extend(label)
            current.last_top = line.top
            if current.amount is None and amounts:
                current.amount = cols.signed(amounts[0])
        else:   # fin du libellé : ligne éloignée, mention de bas de page ou montant isolé
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
