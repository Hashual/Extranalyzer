import re

_CARD_RE = re.compile(r"\bCARTE \d{4}\b")
_DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
_PAYMENT_CODE_RE = re.compile(r"\b(PAIEMENT (?:CB|PSC|MOB)) \d{4}\b")
_TOKEN_RE = re.compile(r"\b[A-Z0-9]{8,}\b")


def _strip_reference(match: re.Match) -> str:
    token = match.group(0)
    return "" if sum(ch.isdigit() for ch in token) >= 4 else token


def normalize_label(label: str) -> str:
    """Libellé stable pour l'identité et le classement : majuscules, sans numéros variables."""
    text = label.upper()
    text = _CARD_RE.sub("", text)
    text = _DATE_RE.sub("", text)
    text = _PAYMENT_CODE_RE.sub(r"\1", text)
    text = _TOKEN_RE.sub(_strip_reference, text)
    return " ".join(text.split())
