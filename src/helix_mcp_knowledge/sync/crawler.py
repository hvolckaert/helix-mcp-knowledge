"""Bounded link discovery for one official product/version tree."""

import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from ..errors import SourceSyncError
from .manifest import OfficialCollectionDefinition

NON_CONTENT_SEGMENTS = {
    "admin",
    "attach",
    "delete",
    "download",
    "edit",
    "export",
    "get",
    "login",
    "logout",
    "pdf",
    "preview",
    "register",
    "rest",
    "save",
    "skin",
    "ssx",
    "jsx",
    "webjars",
}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class RobotsPolicy:
    def __init__(self, parser: RobotFileParser, user_agent: str) -> None:
        self.parser = parser
        self.user_agent = user_agent

    @classmethod
    def load(
        cls,
        client: httpx.Client,
        root_url: str,
        user_agent: str,
        *,
        max_bytes: int = 1024 * 1024,
    ) -> "RobotsPolicy":
        parsed = urlsplit(root_url)
        robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        current_url = robots_url
        for redirect_count in range(4):
            for attempt in range(3):
                try:
                    with client.stream("GET", current_url, follow_redirects=False) as response:
                        if response.status_code in REDIRECT_STATUS_CODES:
                            location = response.headers.get("Location")
                        else:
                            response.raise_for_status()
                            declared_size = response.headers.get("content-length")
                            if (
                                declared_size
                                and declared_size.isdecimal()
                                and int(declared_size) > max_bytes
                            ):
                                raise SourceSyncError(
                                    f"robots.txt exceeds the size limit: {robots_url}"
                                )
                            content = bytearray()
                            for chunk in response.iter_bytes():
                                content.extend(chunk)
                                if len(content) > max_bytes:
                                    raise SourceSyncError(
                                        f"robots.txt exceeds the size limit: {robots_url}"
                                    )
                            parser = RobotFileParser(current_url)
                            parser.parse(content.decode("utf-8", errors="replace").splitlines())
                            return cls(parser, user_agent)
                    break
                except httpx.HTTPError as exc:
                    retryable = isinstance(exc, httpx.TransportError) or (
                        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500
                    )
                    if attempt < 2 and retryable:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise SourceSyncError(f"unable to verify robots.txt: {robots_url}") from exc
            redirected_url = urljoin(current_url, location or "")
            if not location or not _same_origin(robots_url, redirected_url):
                raise SourceSyncError(
                    "robots.txt redirected outside the official documentation host"
                )
            if redirect_count == 3:
                raise SourceSyncError("robots.txt exceeded the redirect limit")
            current_url = redirected_url
        raise SourceSyncError(f"unable to verify robots.txt: {robots_url}")

    def require_allowed(self, url: str) -> None:
        if not self.is_allowed(url):
            raise SourceSyncError(f"robots.txt disallows official source: {url}")

    def is_allowed(self, url: str) -> bool:
        return self.parser.can_fetch(self.user_agent, url)


def _same_origin(expected_url: str, candidate_url: str) -> bool:
    expected = urlsplit(expected_url)
    candidate = urlsplit(candidate_url)
    try:
        expected_port = expected.port or (443 if expected.scheme == "https" else 80)
        candidate_port = candidate.port or (443 if candidate.scheme == "https" else 80)
    except ValueError:
        return False
    return bool(
        candidate.scheme == expected.scheme
        and candidate.hostname == expected.hostname
        and candidate_port == expected_port
        and candidate.username is None
        and candidate.password is None
    )


class OfficialLinkCrawler:
    def __init__(
        self,
        collection: OfficialCollectionDefinition,
        *,
        allowed_domains: list[str],
        robots: RobotsPolicy,
    ) -> None:
        self.collection = collection
        self.root = urlsplit(collection.root_url)
        self.allowed_domains = {domain.casefold().rstrip(".") for domain in allowed_domains}
        self.robots = robots

    def links(self, page_url: str, source_path: Path) -> Iterable[str]:
        soup = BeautifulSoup(source_path.read_text(encoding="utf-8-sig"), "html.parser")
        discovered: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            if "nofollow" in {value.casefold() for value in (anchor.get("rel") or [])}:
                continue
            candidate = self._canonicalize(page_url, str(anchor["href"]))
            if candidate is None or candidate in discovered:
                continue
            if not self.robots.is_allowed(candidate):
                continue
            discovered.add(candidate)
            yield candidate

    def _canonicalize(self, page_url: str, href: str) -> str | None:
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            return None
        parsed = urlsplit(urljoin(page_url, href))
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        path_segments = {segment.casefold() for segment in parsed.path.split("/") if segment}
        if (
            parsed.scheme != "https"
            or hostname not in self.allowed_domains
            or parsed.query
            or not parsed.path.startswith(self.root.path)
            or not parsed.path.endswith("/")
            or path_segments & NON_CONTENT_SEGMENTS
        ):
            return None
        return urlunsplit(("https", parsed.netloc.casefold(), parsed.path, "", ""))
