"""Sérialisation JSON commune aux routes : Decimal → chaîne à 2 décimales, date → ISO.

Aucun numéro de compte complet ne sort de l'API : `mask_numbers` réduit toute suite d'au moins
5 chiffres à ses 4 derniers (les dates et montants n'en contiennent jamais autant d'affilée).
"""
import re
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from fastapi.encoders import jsonable_encoder

from app.importer.service import ImportReport

_LONG_NUMBER = re.compile(r"\d{5,}")


def to_jsonable(obj: Any) -> Any:
    return jsonable_encoder(obj, custom_encoder={Decimal: lambda d: f"{d:.2f}"})


def mask_numbers(text: str | None) -> str | None:
    return None if text is None else _LONG_NUMBER.sub(lambda m: "••" + m.group()[-4:], text)


def report_to_json(report: ImportReport) -> dict:
    """Rapport d'import : `account_number` remplacé par `number_tail`, messages masqués."""
    sections = []
    for section in report.sections:
        data = asdict(section)
        data["number_tail"] = data.pop("account_number")[-4:]
        data["message"] = mask_numbers(data["message"])
        sections.append(data)
    return to_jsonable({"document_id": report.document_id, "duplicate_file": report.duplicate_file,
                        "sections": sections, "error": mask_numbers(report.error)})
