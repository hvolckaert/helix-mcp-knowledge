"""Safe FTS query construction and technical-term extraction."""

import re
import unicodedata

from ..errors import SearchValidationError

TOKEN_PATTERN = re.compile(r"[\w]+(?:[_:.-][\w]+)*", re.UNICODE)
TECHNICAL_PATTERNS = (
    re.compile(r"\bARERR\s+\d+\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*[:_][A-Za-z0-9:_-]+\b"),
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]+)+\b"),
)

# Small, deterministic Helix-domain glossary for cross-language lexical retrieval.
# This intentionally expands only Spanish queries into the English terms used by
# BMC documentation. It is not intended to be a general-purpose translator.
_SPANISH_HINTS = frozenset(
    {
        "al",
        "aparecen",
        "como",
        "conjunto",
        "cual",
        "cuando",
        "datos",
        "debe",
        "del",
        "donde",
        "el",
        "ella",
        "hacer",
        "idioma",
        "la",
        "las",
        "los",
        "navegador",
        "para",
        "por",
        "que",
        "si",
        "traducidos",
        "una",
        "usuario",
    }
)
_SPANISH_TO_ENGLISH = {
    "actividad": ("activity",),
    "actividades": ("activity", "activities"),
    "activo": ("asset",),
    "activos": ("asset", "assets"),
    "aparece": ("appears",),
    "aparecen": ("appear",),
    "atributo": ("attribute",),
    "atributos": ("attribute", "attributes"),
    "cache": ("cache",),
    "cambio": ("change",),
    "cambios": ("change", "changes"),
    "campo": ("field",),
    "campos": ("field", "fields"),
    "configuracion": ("configuration",),
    "conjunto": ("dataset",),
    "crear": ("create",),
    "datos": ("data", "dataset"),
    "duplicado": ("duplicate",),
    "duplicados": ("duplicate", "duplicates"),
    "elemento": ("item",),
    "elementos": ("item", "items"),
    "estado": ("status",),
    "estados": ("status",),
    "fiable": ("reliable",),
    "formulario": ("form",),
    "formularios": ("form", "forms"),
    "fusionar": ("merge", "merging"),
    "idioma": ("language", "locale"),
    "identificar": ("identify", "identification"),
    "incidencia": ("incident",),
    "incidencias": ("incident", "incidents"),
    "localizado": ("localized",),
    "localizados": ("localized",),
    "metadatos": ("metadata",),
    "navegador": ("browser",),
    "normalizacion": ("normalization",),
    "prioridad": ("priority",),
    "prioridades": ("priority",),
    "reconciliacion": ("reconciliation",),
    "registro": ("record",),
    "registros": ("record", "records"),
    "servidor": ("server",),
    "solicitud": ("request",),
    "solicitudes": ("request", "requests"),
    "trabajo": ("job",),
    "trabajos": ("job", "jobs"),
    "traducido": ("translated", "localized"),
    "traducidos": ("translated", "localized"),
    "urgencia": ("urgency",),
    "urgencias": ("urgency",),
    "usa": ("uses",),
    "usuario": ("user",),
}
_SPANISH_PHRASE_TO_ENGLISH = {
    # BMC's canonical CMDB term for a reliable reconciled dataset.
    "conjunto de datos fiable": ("golden", "dataset"),
}


def query_tokens(query: str) -> list[str]:
    tokens = [token for token in TOKEN_PATTERN.findall(query) if len(token) > 1]
    if not tokens:
        raise SearchValidationError("query has no searchable terms")
    return list(dict.fromkeys(tokens))


def expand_lexical_tokens(query: str) -> list[str]:
    """Add bounded English Helix terms when the input is recognizably Spanish."""

    tokens = query_tokens(query)
    folded = [_fold_token(token) for token in tokens]
    hint_count = len(set(folded) & _SPANISH_HINTS)
    has_spanish_punctuation = "¿" in query or "¡" in query
    has_spanish_character = any(character in query.casefold() for character in "áéíóúñü")
    if not (has_spanish_punctuation or has_spanish_character or hint_count >= 2):
        return tokens

    expanded = list(tokens)
    seen = {token.casefold() for token in tokens}
    folded_query = _fold_token(query)
    for phrase, translations in _SPANISH_PHRASE_TO_ENGLISH.items():
        if phrase in folded_query:
            _append_unseen(expanded, seen, translations)
    for token in folded:
        _append_unseen(expanded, seen, _SPANISH_TO_ENGLISH.get(token, ()))
    return expanded


def build_fts_query(query: str) -> str:
    """Build a quoted OR query so user syntax cannot become FTS operators."""
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in expand_lexical_tokens(query))


def extract_technical_terms(query: str) -> list[str]:
    terms: list[str] = []
    for pattern in TECHNICAL_PATTERNS:
        terms.extend(match.group(0) for match in pattern.finditer(query))
    return list(dict.fromkeys(terms))


def _fold_token(token: str) -> str:
    decomposed = unicodedata.normalize("NFKD", token.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _append_unseen(expanded: list[str], seen: set[str], translations: tuple[str, ...]) -> None:
    for translation in translations:
        if translation.casefold() not in seen:
            seen.add(translation.casefold())
            expanded.append(translation)
