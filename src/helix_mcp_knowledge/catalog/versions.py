"""Version normalization helpers."""

import re

from ..errors import SearchValidationError

VERSION_PATTERN = re.compile(r"^\d{2}\.\d(?:\.\d+)?$")
ROLLING_VERSION = "current"


def normalize_version(value: str) -> str:
    normalized = value.strip()
    if normalized.casefold() == ROLLING_VERSION:
        return ROLLING_VERSION
    if not VERSION_PATTERN.fullmatch(normalized):
        raise SearchValidationError(f"invalid BMC version: {value}")
    return normalized


def version_sort_key(value: str) -> tuple[int, tuple[int, ...]]:
    """Sort numeric releases naturally and rolling documentation first."""

    normalized = normalize_version(value)
    if normalized == ROLLING_VERSION:
        return 1, ()
    return 0, tuple(int(part) for part in normalized.split("."))
