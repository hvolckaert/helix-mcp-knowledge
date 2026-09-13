from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_release_assets_are_attested_before_publication() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    attest = workflow.index("uses: actions/attest@")
    publish = workflow.index("gh release create", attest)

    assert attest < publish
    assert "subject-path: dist/*" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
