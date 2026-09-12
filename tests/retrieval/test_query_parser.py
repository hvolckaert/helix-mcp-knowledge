import pytest

from helix_mcp_knowledge.errors import SearchValidationError
from helix_mcp_knowledge.retrieval.query_parser import (
    build_fts_query,
    expand_lexical_tokens,
)


def test_spanish_helix_query_adds_bounded_english_terms() -> None:
    expanded = expand_lexical_tokens(
        "¿Cómo identificar duplicados y fusionar atributos en un conjunto de datos fiable?"
    )

    assert expanded[:5] == ["Cómo", "identificar", "duplicados", "fusionar", "atributos"]
    assert {
        "identify",
        "identification",
        "duplicate",
        "duplicates",
        "merge",
        "merging",
        "attribute",
        "attributes",
        "dataset",
        "data",
        "reliable",
        "golden",
    } <= set(expanded)


def test_unaccented_spanish_query_is_detected_from_multiple_hints() -> None:
    expanded = expand_lexical_tokens("usuario cambia idioma del navegador")

    assert {"user", "language", "locale", "browser"} <= set(expanded)


def test_english_query_is_not_expanded() -> None:
    query = "How does a user change browser language and ticket status?"

    assert expand_lexical_tokens(query) == [
        "How",
        "does",
        "user",
        "change",
        "browser",
        "language",
        "and",
        "ticket",
        "status",
    ]


def test_cmdb_duplicate_ci_intent_adds_canonical_reconciliation_terms() -> None:
    expanded = expand_lexical_tokens(
        "How does CMDB resolve conflicting duplicate CIs into one trusted production view?"
    )

    assert {"reconciliation", "merge", "merging", "reliable", "dataset"} <= set(expanded)


def test_cmdb_query_with_canonical_reconciliation_term_is_not_expanded() -> None:
    query = "How does reconciliation merge duplicate CIs into production?"

    assert expand_lexical_tokens(query) == [
        "How",
        "does",
        "reconciliation",
        "merge",
        "duplicate",
        "CIs",
        "into",
        "production",
    ]


def test_non_ci_duplicate_query_is_not_expanded() -> None:
    query = "How do I resolve conflicting duplicate records in production?"

    assert expand_lexical_tokens(query) == [
        "How",
        "do",
        "resolve",
        "conflicting",
        "duplicate",
        "records",
        "in",
        "production",
    ]


def test_fts_query_keeps_every_term_quoted() -> None:
    built = build_fts_query('¿Cómo buscar "ARERR" y configuración?')

    assert '"ARERR"' in built
    assert '"configuration"' in built
    assert " OR " in built


def test_query_without_searchable_terms_is_rejected() -> None:
    with pytest.raises(SearchValidationError, match="no searchable terms"):
        expand_lexical_tokens("¿?!")
