"""Sérialisation JSON commune aux routes : Decimal → chaîne à 2 décimales, date → ISO."""
from decimal import Decimal
from typing import Any

from fastapi.encoders import jsonable_encoder


def to_jsonable(obj: Any) -> Any:
    return jsonable_encoder(obj, custom_encoder={Decimal: lambda d: f"{d:.2f}"})
