"""Canonical UUID validation for persisted records."""

import uuid

from unifier.errors import UnifierError


def validate_uuid(value, label: str, *, form: str = "the canonical lowercase UUID form") -> str:
    if not isinstance(value, str):
        raise UnifierError(f"{label} must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UnifierError(f"{label} is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise UnifierError(f"{label} must be {form}")
    return value
