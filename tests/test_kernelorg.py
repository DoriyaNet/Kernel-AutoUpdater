import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kernel_autoupdater import kernelorg
from kernel_autoupdater.kernelorg import (
    DownloadError,
    FeedError,
    _parse_feed,
    checksums_url_for,
    download,
    fetch_feed,
    find_release,
    parse_sha256sums,
    release_for_version,
    select_release,
    signature_url_for,
)
from kernel_autoupdater.versioning import Channel


def sample_payload():
    def entry(moniker, version, source=None, eol=False):
        return {
            "moniker": moniker,
            "version": version,
            "iseol": eol,
            "released": {"isodate": "2026-10-30"},
            "source": source
            or "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-{}.tar.xz".format(
                version
            ),
            "pgp": "https://example.invalid/linux-{}.tar.sign".format(version),
        }

    return {
        "latest_stable": {"version": "7.3.1"},
        "releases": [
            entry(
                "mainline",
                "7.4-rc1",
                source="https://git.kernel.org/torvalds/t/linux-7.4-rc1.tar.gz",
            ),
            entry("stable", "7.3.1"),
            entry("stable", "7.2.15", eol=True),
            entry("longterm", "6.18.60"),
            entry("longterm", "6.12.115"),
            entry("longterm", "6.6.161"),
            entry("longterm", "5.15.224"),
        ],
    }


def sample_feed():
    return _parse_feed(json.dumps(sample_payload()))


class FakeResponse:
    def __init__(self, data):
        self._buffer = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size=-1):
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FeedTests(unittest.TestCase):
    def test_select_stable(self):
        release = select_release(sample_feed(), Channel.parse("stable"))
        self.assertEqual(release.version, "7.3.1")

    def test_select_longterm_and_series(self):
        self.assertEqual(
            select_release(sample_feed(), Channel.parse("longterm")).version,
            "6.18.60",
        )
        self.assertEqual(
            select_release(
                sample_feed(), Channel.parse("longterm:6.12")
            ).version,
            "6.12.115",
        )

    def test_select_mainline(self):
        release = select_release(sample_feed(), Channel.parse("mainline"))
        self.assertEqual(release.version, "7.4-rc1")
        self.assertIsNone(release.checksums_url)

    def test_find_release_and_missing(self):
        self.assertEqual(find_release(sample_feed(), "7.3.1").version, "7.3.1")
        self.assertIsNone(find_release(sample_feed(), "7.3.0"))

    def test_feed_requires_releases(self):
        with self.assertRaises(FeedError):
            _parse_feed("{}")
        with self.assertRaises(FeedError):
            _parse_feed("not json")

    def test_feed_cache_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "releases.json"
            cache.write_text(json.dumps(sample_payload()), encoding="utf-8")
            with patch("kernel_autoupdater.kernelorg.urlopen", side_effect=OSError("offline")):
                feed = fetch_feed(cache_path=cache)
            self.assertTrue(feed.stale)
            self.assertEqual(select_release(feed, Channel.parse("stable")).version, "7.3.1")


class ChecksumTests(unittest.TestCase):
    def test_checksums_url(self):
        self.assertEqual(
            checksums_url_for(
                "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.1.tar.xz"
            ),
            "https://cdn.kernel.org/pub/linux/kernel/v7.x/sha256sums.asc",
        )
        self.assertIsNone(
            checksums_url_for(
                "https://git.kernel.org/torvalds/t/linux-7.4-rc1.tar.gz"
            )
        )
        self.assertIsNone(checksums_url_for("https://example.com/linux.tar.xz"))

    def test_parse_signed_checksums(self):
        text = """-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA256

fa595aad93734f829c963c2a90ef7c3673224fd7fdae7b49406e397cde2de231  linux-7.3.1.tar.xz
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa *linux-7.3.1.tar.gz
-----BEGIN PGP SIGNATURE-----

ignored
-----END PGP SIGNATURE-----
"""
        checksums = parse_sha256sums(text)
        self.assertEqual(
            checksums["linux-7.3.1.tar.xz"],
            "fa595aad93734f829c963c2a90ef7c3673224fd7fdae7b49406e397cde2de231",
        )
        self.assertIn("linux-7.3.1.tar.gz", checksums)

    def test_release_for_version(self):
        release = release_for_version("7.2.9")
        self.assertEqual(
            release.source,
            "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.9.tar.xz",
        )
        self.assertIsNotNone(release.checksums_url)
        self.assertEqual(
            release.pgp,
            "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.9.tar.sign",
        )
        zero = release_for_version("6.8.0")
        self.assertEqual(
            zero.source,
            "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.tar.xz",
        )
        rc = release_for_version("7.4-rc1", allow_rc=True)
        self.assertEqual(rc.source, "https://git.kernel.org/torvalds/t/linux-7.4-rc1.tar.gz")

    def test_signature_url(self):
        self.assertEqual(
            signature_url_for("https://example/linux-6.8.tar.xz"),
            "https://example/linux-6.8.tar.sign",
        )
        self.assertIsNone(signature_url_for("https://example/linux.tar"))


class DownloadTests(unittest.TestCase):
    def test_download_and_verify(self):
        data = b"kernel tarball"
        digest = __import__("hashlib").sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "linux.tar.xz"
            with patch(
                "kernel_autoupdater.kernelorg.urlopen",
                return_value=FakeResponse(data),
            ):
                result = download(
                    "https://example.invalid/linux.tar.xz",
                    destination,
                    expected_sha256=digest,
                )
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), data)

    def test_download_rejects_bad_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "linux.tar.xz"
            with patch(
                "kernel_autoupdater.kernelorg.urlopen",
                return_value=FakeResponse(b"data"),
            ):
                with self.assertRaises(DownloadError):
                    download(
                        "https://example.invalid/linux.tar.xz",
                        destination,
                        expected_sha256="0" * 64,
                    )
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(destination.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()

