"""Retrieval package with a lazy public ``SearchEngine`` export.

Keeping package initialization dependency-free lets isolated optional workers import
their small retrieval contracts without pulling the full server model stack.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .search_engine import SearchEngine

__all__ = ["SearchEngine"]


def __getattr__(name: str) -> Any:
    if name == "SearchEngine":
        from .search_engine import SearchEngine

        return SearchEngine
    raise AttributeError(name)
