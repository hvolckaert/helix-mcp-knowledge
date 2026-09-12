"""Safe FTS query construction and technical-term extraction."""

import re

from ..errors import SearchValidationError

TOKEN_PATTERN = re.compile(r"[\w]+(?:[_:.-][\w]+)*", re.UNICODE)
TECHNICAL_PATTERNS = (
    re.compile(r"\bARERR\s+\d+\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*[:_][A-Za-z0-9:_-]+\b"),
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]+)+\b"),
)


def query_tokens(query: str) -> list[str]:
    tokens = [token for token in TOKEN_PATTERN.findall(query) if len(token) > 1]
    if not tokens:
        raise SearchValidationError("query has no searchable terms")
    return list(dict.fromkeys(tokens))


def build_fts_query(query: str) -> str:
    """Build a quoted OR query so user syntax cannot become FTS operators."""
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in query_tokens(query))


def extract_technical_terms(query: str) -> list[str]:
    terms: list[str] = []
    for pattern in TECHNICAL_PATTERNS:
        terms.extend(match.group(0) for match in pattern.finditer(query))
    return list(dict.fromkeys(terms))
