"""Logging configuration that keeps stdout clean for MCP stdio."""

import logging
import sys


def configure_logging(level: str) -> None:
    """Configure process logging on stderr."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
