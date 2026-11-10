from pathlib import Path
from typing import Protocol

from app.model import ParsedDocument


class StatementParser(Protocol):
    """Interface d'un format de relevé bancaire (interchangeable)."""

    name: str

    def can_parse(self, pdf_path: Path) -> bool:
        """Indique si le PDF ressemble à un relevé de ce format."""
        ...

    def parse(self, pdf_path: Path) -> ParsedDocument:
        """Analyse le relevé ; lève `ParseError` s'il ne peut pas être interprété."""
        ...
