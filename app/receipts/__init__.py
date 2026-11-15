from app.receipts.base import ReceiptParser
from app.receipts.superu import SuperUParser

PARSERS_RECEIPTS: list[ReceiptParser] = [SuperUParser()]
