"""Hardened HTTP downloader for explicitly allowlisted official pages."""

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from ..errors import SourceNotFoundError, SourceSyncError


@dataclass(frozen=True, slots=True)
class DownloadResult:
    status: str
    source_url: str
    source_path: Path
    file_size: int
    etag: str | None
    last_modified: str | None


class OfficialHttpDownloader:
    def __init__(
        self,
        *,
        client: httpx.Client,
        allowed_domains: list[str],
        max_bytes: int,
        max_redirects: int = 3,
        max_attempts: int = 3,
    ) -> None:
        self.client = client
        self.allowed_domains = {domain.casefold().rstrip(".") for domain in allowed_domains}
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_attempts = max_attempts

    def fetch(
        self,
        url: str,
        destination: Path,
        state: dict[str, object] | None = None,
    ) -> DownloadResult:
        self._validate_url(url)
        state = state or {}
        conditional_headers = {
            name: value
            for name, value in {
                "If-None-Match": state.get("etag"),
                "If-Modified-Since": state.get("last_modified"),
            }.items()
            if isinstance(value, str) and value
        }
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._request(url, destination, conditional_headers)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise SourceSyncError(
                        f"network failure after {self.max_attempts} attempts: {url}"
                    ) from exc
                time.sleep(0.5 * attempt)
        raise SourceSyncError(f"unable to download official source: {url}")

    def _request(self, url: str, destination: Path, headers: dict[str, str]) -> DownloadResult:
        current_url = url
        for redirect_count in range(self.max_redirects + 1):
            with self.client.stream("GET", current_url, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count == self.max_redirects:
                        raise SourceSyncError(f"too many redirects while downloading {url}")
                    location = response.headers.get("location")
                    if not location:
                        raise SourceSyncError(f"redirect without Location while downloading {url}")
                    current_url = urljoin(current_url, location)
                    if self._is_interactive_authentication_redirect(current_url):
                        hostname = urlsplit(current_url).hostname or "an external identity provider"
                        raise SourceNotFoundError(
                            "official source requires interactive authentication at " + hostname
                        )
                    self._validate_url(current_url)
                    continue
                if response.status_code == 304:
                    if destination.is_file():
                        return DownloadResult(
                            status="unchanged",
                            source_url=current_url,
                            source_path=destination,
                            file_size=destination.stat().st_size,
                            etag=response.headers.get("etag") or headers.get("If-None-Match"),
                            last_modified=response.headers.get("last-modified")
                            or headers.get("If-Modified-Since"),
                        )
                    return self._request(url, destination, {})
                if response.status_code == 403 and response.headers.get("cf-mitigated"):
                    raise SourceSyncError(
                        "the official documentation host rejected non-interactive access "
                        "with a Cloudflare challenge; use browser authentication instead"
                    )
                if response.status_code == 404:
                    raise SourceNotFoundError(f"official source returned HTTP 404: {current_url}")
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SourceSyncError(
                        f"official source returned HTTP {response.status_code}: {current_url}"
                    ) from exc
                content_type = response.headers.get("content-type", "").casefold()
                if not any(
                    accepted in content_type for accepted in ("text/html", "application/xhtml+xml")
                ):
                    raise SourceSyncError(
                        f"official source is not HTML ({content_type or 'unknown type'}): "
                        f"{current_url}"
                    )
                file_size = self._write_response(response, destination)
                return DownloadResult(
                    status="downloaded",
                    source_url=current_url,
                    source_path=destination,
                    file_size=file_size,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
        raise SourceSyncError(f"unable to download official source: {url}")

    def _write_response(self, response: httpx.Response, destination: Path) -> int:
        declared_size = response.headers.get("content-length")
        if declared_size and declared_size.isdecimal() and int(declared_size) > self.max_bytes:
            raise SourceSyncError(f"official source exceeds configured size limit: {response.url}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        size = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".download-", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SourceSyncError(
                            f"official source exceeds configured size limit: {response.url}"
                        )
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(destination)
            return size
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme != "https"
            or hostname not in self.allowed_domains
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise SourceSyncError(f"official source URL is not allowlisted: {url}")

    @staticmethod
    def _is_interactive_authentication_redirect(url: str) -> bool:
        parsed = urlsplit(url)
        path = parsed.path.casefold().rstrip("/")
        query = parse_qs(parsed.query)
        authorization_endpoint = path.endswith("/authorize") or (
            "/oauth2/" in path and "authorize" in path
        )
        return (
            parsed.scheme == "https"
            and authorization_endpoint
            and any(key in query for key in ("client_id", "redirect_uri", "response_type"))
        )
