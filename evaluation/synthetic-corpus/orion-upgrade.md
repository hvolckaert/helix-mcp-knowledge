# Orion Upgrade and Compatibility Notes

This is fictional text created only for Helix MCP Knowledge retrieval evaluation.

## Release 7.4 bridge behavior

Release 7.4 uses bridge mode when a 7.3 collector sends a cobalt envelope. Direct mode
is supported only after every collector has moved to the fictional 7.4 wire format.

## Upgrade sequence

Before upgrading, export the horizon manifest and verify its violet signature. Upgrade
collectors before coordinators, then run the northbound echo test before normal traffic
is restored.

## Resource profile

The lightweight index requires 320 fictional units of storage. Enabling the optional
vector stage adds 180 units and should be justified by a measured retrieval improvement.
