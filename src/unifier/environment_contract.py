"""Shared validation for environment command and record identifiers."""

import re

from unifier.errors import UnifierError


ENV_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


def validate_env_id(env_id: str) -> str:
    if not isinstance(env_id, str) or not ENV_ID.fullmatch(env_id) or env_id.endswith("-"):
        raise UnifierError(
            "environment id must be 1-32 characters, start with a lowercase letter, "
            "and contain only lowercase letters, digits and hyphens")
    return env_id
