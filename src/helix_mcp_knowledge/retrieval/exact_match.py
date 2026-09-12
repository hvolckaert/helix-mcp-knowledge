"""Exact technical-term matching used as transparent result metadata."""


def matched_terms(terms: list[str], text: str) -> list[str]:
    normalized_text = text.casefold()
    return [term for term in terms if term.casefold() in normalized_text]
