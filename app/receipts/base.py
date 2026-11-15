from pathlib import Path
from typing import Protocol

from app.receipts.model import ParsedReceipt


class ReceiptParser(Protocol):
    """Interface d'un format de ticket de caisse (une enseigne), interchangeable."""

    store: str

    def kind(self, pdf: Path) -> str | None:
        """'receipt' (ticket), 'cb_slip' (ticket CB) ou None si le PDF n'est pas de cette enseigne."""
        ...

    def parse(self, pdf: Path) -> ParsedReceipt:
        """Lit un ticket ; lève `ReceiptParseError` s'il ne peut pas être interprété."""
        ...
