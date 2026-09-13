from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_catalog_release_is_verified_before_immutable_publication() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/publish-catalog.yml").read_text(
        encoding="utf-8"
    )

    create = workflow.index('gh release create "${tag}"')
    draft = workflow.index("--draft", create)
    upload = workflow.index('gh release upload "${tag}"', draft)
    verify_assets = workflow.index("cmp --silent", upload)
    verify_digests = workflow.index("digest mismatch", verify_assets)
    attest = workflow.index("uses: actions/attest@", verify_digests)
    publish = workflow.index("gh release edit", attest)

    assert create < draft < upload < verify_assets < verify_digests < attest < publish
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
