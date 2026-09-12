"""Deterministic Unicode and whitespace normalization."""

import re
import unicodedata

BLANK_LINES = re.compile(r"\n{3,}")
INLINE_SPACE = re.compile(r"[\t\r\f\v ]+")


def normalize_text(value: str, *, preserve_lines: bool = False) -> str:
    text = unicodedata.normalize("NFC", value).replace("\x00", "")
    lines = [INLINE_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    if preserve_lines:
        return "\n".join(lines).strip()
    return BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
