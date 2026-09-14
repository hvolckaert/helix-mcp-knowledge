from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

from helix_mcp_knowledge.catalog.updater import CatalogUpdateChecker
from helix_mcp_knowledge.config import CatalogUpdateSettings
from helix_mcp_knowledge.github_cli import ManagedGitHubCli
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest


def _catalog(revision: int) -> OfficialSourceManifest:
    return OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": revision,
            "products": [{"product_id": "cmdb", "name": "BMC Helix CMDB", "aliases": ["CMDB"]}],
            "collections": [
                {
                    "collection_id": f"bmc-cmdb-26-{revision}",
                    "root_url": f"https://docs.bmc.com/cmdb/26.{revision}/",
                    "local_path_prefix": f"cmdb/26.{revision}/discovered",
                    "product": "cmdb",
                    "version": f"26.{revision}",
                }
            ],
        }
    )


class CatalogRunner:
    def __init__(self, manifest: OfficialSourceManifest, *, corrupt_checksum: bool = False):
        self.manifest = manifest
        self.corrupt_checksum = corrupt_checksum
        self.calls: list[list[str]] = []
        self.urls: list[str] = []

    def _assets(self) -> dict[str, bytes]:
        manifest_name = "bmc-official-catalog.yaml"
        content = yaml.safe_dump(
            self.manifest.model_dump(mode="json", exclude_defaults=False), sort_keys=False
        ).encode()
        digest = "0" * 64 if self.corrupt_checksum else hashlib.sha256(content).hexdigest()
        return {
            manifest_name: content,
            "bmc-official-catalog.sha256": f"{digest}  {manifest_name}\n".encode(),
        }

    def get_json(self, url: str, *, timeout: int, action: str) -> object:
        del timeout, action
        self.urls.append(url)
        if "/attestations/" in url:
            return {"attestations": [{"bundle": {"mediaType": "test-bundle"}}]}
        return [
            {
                "tag_name": f"catalog-v{self.manifest.catalog_revision}",
                "draft": False,
                "prerelease": True,
                "html_url": "https://github.test/catalog-release",
                "assets": [
                    {
                        "name": name,
                        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                    }
                    for name, content in self._assets().items()
                ],
            }
        ]

    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout: int,
        action: str,
    ) -> None:
        del timeout, action
        self.urls.append(url)
        destination.write_bytes(self._assets()[destination.name])

    def __call__(self, command, **kwargs):
        rendered = [str(item) for item in command]
        self.calls.append(rendered)
        if rendered[1:2] == ["release"]:
            raise AssertionError("public catalog access must not use gh")
        assert rendered[1:3] == ["attestation", "verify"]
        environment = kwargs["env"]
        assert environment["GH_PROMPT_DISABLED"] == "1"
        assert "GH_TOKEN" not in environment
        assert "GITHUB_REPOSITORY" not in environment
        return subprocess.CompletedProcess(rendered, 0, stdout="", stderr="")


def _checker(app, tmp_path: Path, runner: CatalogRunner, callback=lambda: None):
    manifest_path = app.config.official_manifest_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        yaml.safe_dump(_catalog(1).model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    gh = tmp_path / "gh"
    gh.write_text("command", encoding="utf-8")
    gh.chmod(0o700)
    app.config.catalog_updates = CatalogUpdateSettings(
        enabled=True,
        repository="example/public",
        gh_command=str(gh),
        cache_path=Path("data/cache/catalog.yaml"),
    )
    return CatalogUpdateChecker(
        config=app.config,
        store=AutomationStore(app.database),
        runner=runner,
        transport=runner,
        clock=lambda: 1_800_000_000.0,
        owner_id="catalog-checker",
        on_updated=callback,
        gh_command=gh,
    )


def test_catalog_checker_downloads_verifies_and_activates_new_revision(app, tmp_path: Path) -> None:
    activated: list[bool] = []
    runner = CatalogRunner(_catalog(2))
    checker = _checker(app, tmp_path, runner, lambda: activated.append(True))

    result = checker.check(force=True)

    assert result.status == "updated"
    assert result.current_revision == 2
    assert result.latest_revision == 2
    assert activated == [True]
    assert OfficialSourceManifest.load(app.config.official_catalog_cache_path).catalog_revision == 2
    assert len(runner.calls) == 2
    assert len(runner.urls) == 5


def test_catalog_checker_migrates_legacy_configuration_to_managed_command(
    app, tmp_path: Path, monkeypatch
) -> None:
    runner = CatalogRunner(_catalog(2))
    checker = _checker(app, tmp_path, runner)
    app.config.catalog_updates.gh_command = "gh"
    app.config.updates.gh_command = "/usr/local/bin/gh"
    managed = tmp_path / "tools/github-cli/2.100.0/bin/gh"
    managed.parent.mkdir(parents=True)
    managed.write_text("managed", encoding="utf-8")
    managed.chmod(0o700)
    checker.gh_command = None
    monkeypatch.setattr(
        "helix_mcp_knowledge.catalog.updater.ensure_managed_github_cli",
        lambda *_args, **_kwargs: ManagedGitHubCli(
            version="2.100.0",
            command=managed,
            platform="linux",
            architecture="amd64",
            archive_sha256="a" * 64,
            binary_sha256="b" * 64,
            source_url="https://github.com/cli/cli/releases/download/v2.100.0/test",
            installed=True,
            downloaded=False,
        ),
    )

    result = checker.check(force=True)

    assert result.status == "updated"
    assert runner.calls[0][0] == str(managed)


def test_catalog_checker_rejects_a_checksum_mismatch(app, tmp_path: Path) -> None:
    runner = CatalogRunner(_catalog(2), corrupt_checksum=True)
    checker = _checker(app, tmp_path, runner)

    result = checker.check(force=True)

    assert result.status == "error"
    assert result.current_revision == 1
    assert result.error is not None and "SHA-256" in result.error
    assert not app.config.official_catalog_cache_path.exists()


def test_catalog_checker_does_not_download_an_existing_revision(app, tmp_path: Path) -> None:
    runner = CatalogRunner(_catalog(1))
    checker = _checker(app, tmp_path, runner)

    result = checker.check(force=True)

    assert result.status == "current"
    assert result.current_revision == 1
    assert runner.calls == []
    assert runner.urls == ["https://api.github.com/repos/example/public/releases?per_page=100"]
