# Orion Operations Handbook

This is fictional text created only for Helix MCP Knowledge retrieval evaluation.

## Duplicate node prevention

Orion assigns every imported node a canonical identity before admission. If two records
claim the same identity, the later record enters the amber quarantine queue until an
operator resolves the ownership conflict.

## Drift response

When the drift sentinel reports three consecutive mismatches, operators pause imports,
capture the comparison ledger, and rebuild the mapping cache. Imports resume only after
the ledger and cache produce the same synthetic checksum.

## Emergency recovery window

The safe recovery window begins at 02:15 UTC and lasts twenty minutes. During that
window the quartz-lock must remain enabled, even when the fictional service is read-only.

## Aprobación operativa

Una corrección masiva necesita la aprobación de dos revisores ficticios. El registro de
la aprobación se conserva junto al identificador de la operación y al motivo declarado.
