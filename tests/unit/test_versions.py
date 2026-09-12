import pytest

from helix_mcp_knowledge.catalog.versions import normalize_version, version_sort_key
from helix_mcp_knowledge.errors import SearchValidationError


def test_numeric_and_rolling_versions_are_normalized() -> None:
    assert normalize_version(" 26.3 ") == "26.3"
    assert normalize_version(" Current ") == "current"


def test_versions_sort_with_rolling_and_latest_first() -> None:
    versions = ["26.1", "current", "26.3", "26.2"]

    assert sorted(versions, key=version_sort_key, reverse=True) == [
        "current",
        "26.3",
        "26.2",
        "26.1",
    ]


def test_unknown_version_label_is_rejected() -> None:
    with pytest.raises(SearchValidationError, match="invalid BMC version"):
        normalize_version("latest")
