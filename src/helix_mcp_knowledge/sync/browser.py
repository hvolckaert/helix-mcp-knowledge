"""Browser-backed downloader for BMC SSO and Cloudflare-protected pages."""

import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..config import AppConfig
from ..errors import SourceSyncError
from .http import DownloadResult


class OfficialBrowserDownloader:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.context = None
        self.playwright = None

    def __enter__(self) -> "OfficialBrowserDownloader":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SourceSyncError(
                "browser synchronization requires `uv sync --extra browser-sync` "
                "and `uv run playwright install chromium`"
            ) from exc
        browser_settings = self.config.ingestion.http.authentication.browser
        profile_path = self.config.resolve_path(browser_settings.profile_path)
        profile_path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            profile_path.chmod(0o700)
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(profile_path),
                headless=browser_settings.headless,
                accept_downloads=False,
            )
        except Exception as exc:
            self.playwright.stop()
            raise SourceSyncError(
                "unable to start the automated browser; install Chromium with "
                "`uv run playwright install chromium`"
            ) from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.context is not None:
            self.context.close()
        if self.playwright is not None:
            self.playwright.stop()

    def fetch(
        self,
        url: str,
        destination: Path,
        state: dict[str, object] | None = None,
    ) -> DownloadResult:
        del state
        self._validate_source_url(url)
        if self.context is None:
            raise SourceSyncError("automated browser is not running")
        page = self.context.new_page()
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.config.ingestion.http.timeout_seconds * 1000,
            )
            self._wait_for_challenge(page)
            if response is not None and response.status >= 400:
                raise SourceSyncError(f"official source returned HTTP {response.status}: {url}")
            if self._login_required(page):
                self._authenticate(force=True)
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.config.ingestion.http.timeout_seconds * 1000,
                )
                self._wait_for_challenge(page)
                if self._login_required(page):
                    raise SourceSyncError(
                        "BMC authentication did not grant access to the official source"
                    )
            final_url = page.url
            self._validate_source_url(final_url)
            html = page.content().encode("utf-8")
            max_bytes = self.config.ingestion.max_file_size_mb * 1024 * 1024
            if len(html) > max_bytes:
                raise SourceSyncError(f"official source exceeds configured size limit: {url}")
            self._atomic_write(destination, html)
            return DownloadResult(
                status="downloaded",
                source_url=final_url,
                source_path=destination,
                file_size=len(html),
                etag=None,
                last_modified=None,
            )
        except SourceSyncError:
            raise
        except Exception as exc:
            raise SourceSyncError(f"browser download failed for {url}: {exc}") from exc
        finally:
            page.close()

    def _authenticate(self, *, force: bool = False) -> None:
        if self.context is None:
            raise SourceSyncError("automated browser is not running")
        auth = self.config.ingestion.http.authentication
        username = os.environ.get(auth.username_env)
        password = os.environ.get(auth.password_env)
        if not username or not password:
            raise SourceSyncError(
                "missing BMC credential environment variables: "
                f"{auth.username_env}, {auth.password_env}"
            )
        page = self.context.new_page()
        settings = auth.browser
        try:
            page.goto(
                settings.login_url,
                wait_until="domcontentloaded",
                timeout=self.config.ingestion.http.timeout_seconds * 1000,
            )
            self._wait_for_challenge(page)
            if not force and not self._login_required(page):
                return
            username_input = page.locator(settings.username_selector).first
            if not username_input.is_visible(timeout=5000):
                if self._login_required(page):
                    raise SourceSyncError("BMC login page did not expose a username field")
                return
            username_input.fill(username)
            page.locator(settings.submit_selector).first.click()
            password_input = page.locator(settings.password_selector).first
            if not password_input.is_visible(timeout=10000):
                raise SourceSyncError(
                    "BMC login requires an unsupported additional authentication step"
                )
            password_input.fill(password)
            page.locator(settings.submit_selector).first.click()
            page.wait_for_load_state(
                "domcontentloaded", timeout=self.config.ingestion.http.timeout_seconds * 1000
            )
            self._wait_for_challenge(page)
            if page.locator(settings.password_selector).first.is_visible(timeout=2000):
                raise SourceSyncError("BMC authentication failed or requires MFA/SSO interaction")
        finally:
            page.close()

    def _wait_for_challenge(self, page) -> None:
        timeout = self.config.ingestion.http.authentication.browser.challenge_timeout_seconds
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            title = page.title().casefold()
            content = page.locator("body").inner_text(timeout=5000).casefold()
            if "just a moment" not in title and "performing security verification" not in content:
                return
            page.wait_for_timeout(1000)
        raise SourceSyncError("Cloudflare browser challenge did not complete automatically")

    @staticmethod
    def _login_required(page) -> bool:
        text = page.locator("body").inner_text(timeout=5000).casefold()
        return any(
            marker in text
            for marker in (
                "you must log in or register",
                "log in to your bmc account",
                "sign in to your account",
            )
        )

    def _validate_source_url(self, url: str) -> None:
        parsed = urlsplit(url)
        allowed = {
            domain.casefold().rstrip(".") for domain in self.config.ingestion.http.allowed_domains
        }
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme != "https"
            or hostname not in allowed
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise SourceSyncError(f"browser left the official source allowlist: {url}")

    @staticmethod
    def _atomic_write(destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".browser-", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
