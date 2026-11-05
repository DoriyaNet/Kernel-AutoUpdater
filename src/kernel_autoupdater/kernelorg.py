"""kernel.org release feed, checksums and downloads."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from kernel_autoupdater.versioning import Channel, KernelVersion, VersionError

LOGGER = logging.getLogger(__name__)

DEFAULT_FEED_URL = "https://www.kernel.org/releases.json"
USER_AGENT = "kernel-autoupdater/0.1 (+https://www.kernel.org/)"
_SHA256_LINE_RE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64})\s+\*?(?P<name>\S+)\s*$")


class FeedError(RuntimeError):
    """Raised when the release feed cannot be read."""


class ReleaseNotFoundError(RuntimeError):
    """Raised when no release matches the requested channel."""


class DownloadError(RuntimeError):
    """Raised when a download or checksum verification fails."""


@dataclass(frozen=True)
class KernelRelease:
    version: str
    moniker: str
    source: str
    pgp: Optional[str] = None
    is_eol: bool = False
    released: Optional[str] = None
    changelog: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "KernelRelease":
        version = str(data.get("version", "")).strip()
        KernelVersion.parse(version)
        source = data.get("source")
        if not source:
            raise FeedError("release {} has no source URL".format(version))
        released = data.get("released") or {}
        if isinstance(released, dict):
            released = released.get("isodate")
        return cls(
            version=version,
            moniker=str(data.get("moniker", "unknown")),
            source=str(source),
            pgp=str(data["pgp"]) if data.get("pgp") else None,
            is_eol=bool(data.get("iseol", False)),
            released=str(released) if released else None,
            changelog=str(data["changelog"]) if data.get("changelog") else None,
        )

    @property
    def parsed_version(self) -> KernelVersion:
        return KernelVersion.parse(self.version)

    @property
    def checksums_url(self) -> Optional[str]:
        return checksums_url_for(self.source)


@dataclass(frozen=True)
class Feed:
    releases: tuple
    latest_stable: Optional[str] = None
    stale: bool = False
    fetched_at: Optional[float] = None


def _parse_feed(text: str) -> Feed:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeedError("release feed is not valid JSON: {}".format(exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("releases"), list):
        raise FeedError("release feed does not contain a releases list")

    releases = []
    for item in payload["releases"]:
        if not isinstance(item, dict):
            continue
        try:
            releases.append(KernelRelease.from_dict(item))
        except (FeedError, VersionError) as exc:
            LOGGER.debug("ignoring malformed feed entry: %s", exc)
    if not releases:
        raise FeedError("release feed contains no usable releases")

    latest = payload.get("latest_stable")
    if isinstance(latest, dict):
        latest = latest.get("version")
    return Feed(
        releases=tuple(releases),
        latest_stable=str(latest) if latest else None,
        fetched_at=time.time(),
    )


def _read_cache(cache_path: Optional[Path]) -> Optional[Feed]:
    if cache_path is None or not cache_path.exists():
        return None
    try:
        text = cache_path.read_text(encoding="utf-8")
        feed = _parse_feed(text)
    except (OSError, FeedError) as exc:
        LOGGER.warning("cannot read release feed cache %s: %s", cache_path, exc)
        return None
    return Feed(
        releases=feed.releases,
        latest_stable=feed.latest_stable,
        stale=True,
        fetched_at=cache_path.stat().st_mtime,
    )


def _write_cache(cache_path: Optional[Path], text: str) -> None:
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_name(cache_path.name + ".tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(str(tmp_path), str(cache_path))
    except OSError as exc:
        LOGGER.warning("cannot write release feed cache %s: %s", cache_path, exc)


def fetch_feed(
    feed_url: str = DEFAULT_FEED_URL,
    cache_path: Optional[Path] = None,
    timeout: int = 30,
    offline: bool = False,
) -> Feed:
    """Fetch the kernel.org release feed, falling back to a local cache."""
    if offline:
        cached = _read_cache(cache_path)
        if cached is None:
            raise FeedError("offline mode requested but no feed cache is available")
        LOGGER.info("using cached release feed (offline mode)")
        return cached

    request = Request(feed_url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset)
        feed = _parse_feed(text)
    except Exception as exc:  # urllib raises several exception types
        cached = _read_cache(cache_path)
        if cached is not None:
            LOGGER.warning("cannot fetch release feed (%s); using cached copy", exc)
            return cached
        raise FeedError("cannot fetch release feed {}: {}".format(feed_url, exc)) from exc

    _write_cache(cache_path, text)
    return feed


def select_release(
    feed: Feed,
    channel: Channel,
    include_eol: bool = False,
) -> KernelRelease:
    candidates = []
    for release in feed.releases:
        if release.moniker != channel.moniker:
            continue
        if release.is_eol and not include_eol:
            continue
        try:
            parsed = release.parsed_version
        except VersionError:
            continue
        if channel.matches(parsed):
            candidates.append(release)
    if not candidates:
        raise ReleaseNotFoundError(
            "no release found for channel {}".format(channel)
        )
    return max(candidates, key=lambda item: item.parsed_version.sort_key)


def find_release(feed: Feed, version: str) -> Optional[KernelRelease]:
    wanted = KernelVersion.parse(version)
    matches = [r for r in feed.releases if r.parsed_version == wanted]
    if not matches:
        return None
    for release in matches:
        if not release.is_eol:
            return release
    return matches[0]


def release_for_version(version: str, allow_rc: bool = False) -> KernelRelease:
    """Build a release record for a version absent from the feed."""
    parsed = KernelVersion.parse(version.lstrip("v"))
    canonical = str(parsed)
    if parsed.rc is not None:
        if not allow_rc:
            raise ReleaseNotFoundError(
                "release candidate {} is not in the release feed".format(version)
            )
        source = "https://git.kernel.org/torvalds/t/linux-{}.tar.gz".format(parsed.tag)
        return KernelRelease(
            version=canonical,
            moniker="mainline",
            source=source,
            pgp=None,
        )

    # kernel.org names x.y.0 tarballs linux-x.y.tar.xz, not linux-x.y.0.tar.xz.
    if parsed.patch == 0:
        archive_version = "{}.{}".format(parsed.major, parsed.minor)
    else:
        archive_version = canonical
    source = "https://cdn.kernel.org/pub/linux/kernel/v{}.x/linux-{}.tar.xz".format(
        parsed.major, archive_version
    )
    return KernelRelease(
        version=canonical,
        moniker="custom",
        source=source,
        pgp=signature_url_for(source),
    )


def signature_url_for(source: str) -> Optional[str]:
    """Return the detached .tar.sign URL for a kernel.org tarball."""
    match = re.match(r"^(.*\.tar)\.(xz|gz|bz2)$", source)
    if not match:
        return None
    return match.group(1) + ".sign"


def checksums_url_for(source: str) -> Optional[str]:
    """Return the sha256sums.asc URL for a kernel.org source tarball."""
    parsed = urlparse(source)
    if not parsed.netloc.endswith("cdn.kernel.org"):
        return None
    filename = posixpath.basename(parsed.path)
    if not re.match(r"^linux-[\w.\-]+\.tar\.(xz|gz|bz2)$", filename):
        return None
    directory = posixpath.dirname(parsed.path)
    return "{}://{}{}/sha256sums.asc".format(parsed.scheme, parsed.netloc, directory)


def parse_sha256sums(text: str) -> Dict[str, str]:
    """Parse a kernel.org sha256sums file, with or without PGP armor."""
    checksums = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("Hash:"):
            continue
        match = _SHA256_LINE_RE.match(line)
        if match:
            checksums[match.group("name")] = match.group("digest").lower()
    return checksums


def fetch_text(url: str, timeout: int = 60) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset)
    except Exception as exc:
        raise DownloadError("cannot download {}: {}".format(url, exc)) from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    url: str,
    destination: Path,
    expected_sha256: Optional[str] = None,
    timeout: int = 120,
) -> Path:
    """Download a file atomically, optionally verifying its SHA-256."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        if expected_sha256:
            actual = sha256_file(destination)
            if actual.lower() == expected_sha256.lower():
                LOGGER.info("using cached archive %s", destination)
                return destination
            LOGGER.warning("cached archive checksum mismatch; downloading again")
        else:
            return destination

    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        partial.unlink()

    request = Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    downloaded = 0
    total = 0
    last_percent = -1
    try:
        with urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else 0
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = int(downloaded * 100 / total)
                        if percent >= last_percent + 10:
                            LOGGER.info(
                                "downloading %s: %d%%", destination.name, percent
                            )
                            last_percent = percent
        actual = digest.hexdigest()
        if expected_sha256 and actual.lower() != expected_sha256.lower():
            raise DownloadError(
                "SHA-256 mismatch for {}: expected {}, got {}".format(
                    destination.name, expected_sha256, actual
                )
            )
        os.replace(str(partial), str(destination))
        LOGGER.info("downloaded %s (%d bytes)", destination.name, downloaded)
        return destination
    except DownloadError:
        if partial.exists():
            partial.unlink()
        raise
    except Exception as exc:
        if partial.exists():
            partial.unlink()
        raise DownloadError("cannot download {}: {}".format(url, exc)) from exc


def max_release(releases: Iterable[KernelRelease]) -> KernelRelease:
    return max(releases, key=lambda item: item.parsed_version.sort_key)

