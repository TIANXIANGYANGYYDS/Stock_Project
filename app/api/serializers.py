from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from bson import ObjectId
from pydantic import BaseModel


def serialize_mongo_value(value: Any) -> Any:
    """Recursively convert BSON/Python values to JSON-safe values.

    MongoDB's internal ``_id`` is intentionally omitted at every mapping level.
    Business fields keep their original names and nesting.
    """

    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return serialize_mongo_value(value.value)
    if isinstance(value, BaseModel):
        return serialize_mongo_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): serialize_mongo_value(item)
            for key, item in value.items()
            if key != "_id"
        }
    if isinstance(value, (list, tuple, set)):
        return [serialize_mongo_value(item) for item in value]
    return value


def serialize_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return serialize_mongo_value(document)
