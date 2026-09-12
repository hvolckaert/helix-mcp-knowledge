from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from helix_mcp_knowledge.catalog.watch import (
    CatalogCandidateValidator,
    CatalogWatchConfig,
    add_validated_candidates,
    candidate_versions,
    next_release_version,
)
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest


def _manifest() -> OfficialSourceManifest:
    return OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": 4,
            "products": [{"product_id": "cmdb", "name": "BMC Helix CMDB", "aliases": ["CMDB"]}],
            "collections": [
                {
                    "collection_id": "bmc-cmdb-26-3",
                    "root_url": "https://docs.helixops.ai/cmdb/263/",
                    "local_path_prefix": "cmdb/26.3/discovered",
                    "product": "cmdb",
                    "version": "26.3",
                }
            ],
        }
    )


def _settings() -> CatalogWatchConfig:
    return CatalogWatchConfig.model_validate(
        {
            "sample_pages": 3,
            "request_delay_seconds": 0,
            "products": {
                "cmdb": {
                    "root_url_template": "https://docs.helixops.ai/cmdb/{compact_version}/",
                    "local_path_prefix_template": "cmdb/{version}/discovered",
                    "max_pages": 1000,
                    "required_markers": ["CMDB", "{version}"],
                    "smoke_queries": ["CMDB"],
                }
            },
        }
    )


def test_next_release_version_crosses_the_year_boundary() -> None:
    assert next_release_version(["26.3", "26.4"]) == "27.1"
    assert next_release_version(["current"]) is None
    assert candidate_versions(_manifest()) == {"cmdb": "26.4"}


def test_candidate_validation_crawls_and_indexes_a_bounded_sample() -> None:
    pages = {
        "https://docs.helixops.ai/robots.txt": (
            "text/plain",
            "User-agent: *\nAllow: /\n",
        ),
        "https://docs.helixops.ai/cmdb/264/": (
            "text/html",
            "<html><main><h1>BMC Helix CMDB 26.4</h1><p>CMDB overview.</p>"
            '<a href="Getting-started/">Start</a><a href="Using/">Use</a></main></html>',
        ),
        "https://docs.helixops.ai/cmdb/264/Getting-started/": (
            "text/html",
            "<html><main><h1>Getting started</h1><p>CMDB concepts and setup.</p></main></html>",
        ),
        "https://docs.helixops.ai/cmdb/264/Using/": (
            "text/html",
            "<html><main><h1>Using CMDB</h1><p>CMDB administration.</p></main></html>",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        content_type, body = pages[str(request.url)]
        return httpx.Response(200, headers={"content-type": content_type}, text=body)

    validator = CatalogCandidateValidator(
        settings=_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    result = validator.validate("cmdb", "26.4")

    assert result.status == "validated"
    assert result.sampled_pages == 3
    assert result.indexed_chunks >= 3
    assert result.smoke_hits and result.smoke_hits["CMDB"] >= 3

    updated = add_validated_candidates(_manifest(), _settings(), [result])
    assert updated.catalog_revision == 5
    assert updated.collections[-1].version == "26.4"
    assert updated.sources[-1].source_id == "bmc-cmdb-26-4-home"


def test_catalog_watch_rejects_nonstandard_https_port() -> None:
    payload = _settings().model_dump()
    payload["products"]["cmdb"]["root_url_template"] = (
        "https://docs.helixops.ai:8443/cmdb/{compact_version}/"
    )

    with pytest.raises(ValidationError, match="credential-free BMC HTTPS URLs"):
        CatalogWatchConfig.model_validate(payload)


def test_candidate_validation_does_not_follow_redirect_outside_version_tree() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            302,
            headers={"location": "https://docs.helixops.ai:8443/internal/"},
        )

    validator = CatalogCandidateValidator(
        settings=_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    result = validator.validate("cmdb", "26.4")

    assert result.status == "error"
    assert "outside its version tree" in (result.error or "")
    assert requested == [
        "https://docs.helixops.ai/robots.txt",
        "https://docs.helixops.ai/cmdb/264/",
    ]


def test_candidate_validation_rejects_oversized_page() -> None:
    settings = _settings().model_copy(update={"max_page_bytes": 1024})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "2048"},
            content=b"x" * 2048,
        )

    validator = CatalogCandidateValidator(
        settings=settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    result = validator.validate("cmdb", "26.4")

    assert result.status == "error"
    assert result.error == "candidate page exceeds the configured size limit"
