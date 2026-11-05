"""Configuration loading and validation."""

from __future__ import annotations

import configparser
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kernel_autoupdater.kernelorg import DEFAULT_FEED_URL
from kernel_autoupdater.versioning import Channel, VersionError

LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/kernel-autoupdater/config.ini")
DEFAULT_WORK_DIR = Path("/var/lib/kernel-autoupdater")
DEFAULT_LOG_FILE = Path("/var/log/kernel-autoupdater.log")
DEFAULT_PATCHES_DIR = Path("/etc/kernel-autoupdater/patches")
DEFAULT_KEYRING = Path("/etc/kernel-autoupdater/kernel.org-keyring.gpg")


class ConfigError(RuntimeError):
    """Raised for invalid or unusable configuration."""


def _as_bool(value: str, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ConfigError("{} must be a boolean, got {!r}".format(key, value))


def _as_int(value: str, key: str, minimum: int = 0) -> int:
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise ConfigError("{} must be an integer, got {!r}".format(key, value)) from exc
    if number < minimum:
        raise ConfigError("{} must be >= {}".format(key, minimum))
    return number


def _as_path(value: str, key: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    if not expanded:
        raise ConfigError("{} must not be empty".format(key))
    return Path(expanded)


@dataclass
class Config:
    source_path: Optional[Path] = None
    channel: str = "stable"
    update_method: str = "build"
    feed_url: str = DEFAULT_FEED_URL
    feed_timeout: int = 30
    download_timeout: int = 120
    work_dir: Path = field(default_factory=lambda: DEFAULT_WORK_DIR)
    log_file: Optional[Path] = field(default_factory=lambda: DEFAULT_LOG_FILE)
    auto_update: bool = False
    auto_reboot: bool = False
    reboot_delay_minutes: int = 5
    min_free_gb: int = 12
    verify_pgp: bool = False
    pgp_keyring: Path = field(default_factory=lambda: DEFAULT_KEYRING)
    allow_unverified: bool = False
    patches_dir: Path = field(default_factory=lambda: DEFAULT_PATCHES_DIR)
    config_source: str = "auto"
    localversion: str = "-kau"
    jobs: int = 0
    keep_build_dir: bool = False
    clear_distro_keys: bool = True
    package_manager: str = "auto"
    kernel_packages: str = "auto"
    refresh_package_index: bool = True
    package_min_free_gb: int = 2
    update_initramfs: bool = True
    update_bootloader: bool = True

    @classmethod
    def load(cls, path: Optional[Path] = None, explicit: bool = False) -> "Config":
        config = cls()
        if path is None:
            return config

        resolved = Path(os.path.expanduser(str(path)))
        config.source_path = resolved
        if not resolved.exists():
            if explicit:
                raise ConfigError("config file not found: {}".format(resolved))
            return config

        parser = configparser.ConfigParser(interpolation=None)
        try:
            with resolved.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error) as exc:
            raise ConfigError("cannot read {}: {}".format(resolved, exc)) from exc

        config._apply(parser)
        return config

    def _get(self, parser: configparser.ConfigParser, section: str, option: str, default: str) -> str:
        return parser.get(section, option, fallback=default)

    def _apply(self, parser: configparser.ConfigParser) -> None:
        try:
            self.channel = self._get(parser, "updater", "channel", self.channel).strip()
            Channel.parse(self.channel)
        except VersionError as exc:
            raise ConfigError(str(exc)) from exc

        self.update_method = self._get(
            parser, "updater", "update_method", self.update_method
        ).strip().lower()
        if self.update_method not in ("build", "package"):
            raise ConfigError(
                "updater.update_method must be build or package, got {!r}".format(
                    self.update_method
                )
            )

        self.feed_url = self._get(parser, "updater", "feed_url", self.feed_url).strip()
        if not self.feed_url:
            raise ConfigError("updater.feed_url must not be empty")
        self.feed_timeout = _as_int(
            self._get(parser, "updater", "feed_timeout", str(self.feed_timeout)),
            "updater.feed_timeout",
            minimum=1,
        )
        self.download_timeout = _as_int(
            self._get(parser, "updater", "download_timeout", str(self.download_timeout)),
            "updater.download_timeout",
            minimum=1,
        )
        self.work_dir = _as_path(
            self._get(parser, "updater", "work_dir", str(self.work_dir)),
            "updater.work_dir",
        )
        log_default = str(self.log_file) if self.log_file else "none"
        log_value = self._get(parser, "updater", "log_file", log_default)
        if log_value.strip().lower() in ("", "none", "off"):
            self.log_file = None
        else:
            self.log_file = _as_path(log_value, "updater.log_file")
        self.auto_update = _as_bool(
            self._get(parser, "updater", "auto_update", str(self.auto_update)),
            "updater.auto_update",
        )
        self.auto_reboot = _as_bool(
            self._get(parser, "updater", "auto_reboot", str(self.auto_reboot)),
            "updater.auto_reboot",
        )
        self.reboot_delay_minutes = _as_int(
            self._get(
                parser, "updater", "reboot_delay_minutes", str(self.reboot_delay_minutes)
            ),
            "updater.reboot_delay_minutes",
        )
        self.min_free_gb = _as_int(
            self._get(parser, "updater", "min_free_gb", str(self.min_free_gb)),
            "updater.min_free_gb",
        )

        self.verify_pgp = _as_bool(
            self._get(parser, "source", "verify_pgp", str(self.verify_pgp)),
            "source.verify_pgp",
        )
        self.pgp_keyring = _as_path(
            self._get(parser, "source", "pgp_keyring", str(self.pgp_keyring)),
            "source.pgp_keyring",
        )
        self.allow_unverified = _as_bool(
            self._get(parser, "source", "allow_unverified", str(self.allow_unverified)),
            "source.allow_unverified",
        )
        self.patches_dir = _as_path(
            self._get(parser, "source", "patches_dir", str(self.patches_dir)),
            "source.patches_dir",
        )

        self.config_source = self._get(
            parser, "build", "config_source", self.config_source
        ).strip()
        if not self.config_source:
            raise ConfigError("build.config_source must not be empty")
        self.localversion = self._get(
            parser, "build", "localversion", self.localversion
        ).strip()
        if any(char in self.localversion for char in "\r\n"):
            raise ConfigError("build.localversion must be a single line")
        self.jobs = _as_int(
            self._get(parser, "build", "jobs", str(self.jobs)), "build.jobs"
        )
        self.keep_build_dir = _as_bool(
            self._get(
                parser, "build", "keep_build_dir", str(self.keep_build_dir)
            ),
            "build.keep_build_dir",
        )
        self.clear_distro_keys = _as_bool(
            self._get(
                parser, "build", "clear_distro_keys", str(self.clear_distro_keys)
            ),
            "build.clear_distro_keys",
        )

        self.package_manager = self._get(
            parser, "packages", "manager", self.package_manager
        ).strip().lower()
        if self.package_manager not in ("auto", "apt", "dnf"):
            raise ConfigError(
                "packages.manager must be auto, apt or dnf, got {!r}".format(
                    self.package_manager
                )
            )
        self.kernel_packages = self._get(
            parser, "packages", "kernel_packages", self.kernel_packages
        ).strip()
        if not self.kernel_packages:
            raise ConfigError("packages.kernel_packages must not be empty")
        if any(char in self.kernel_packages for char in "\r\n"):
            raise ConfigError("packages.kernel_packages must be a single line")
        if self.kernel_packages.lower() != "auto":
            names = [
                item
                for item in re.split(r"[\s,]+", self.kernel_packages)
                if item
            ]
            if not names:
                raise ConfigError("packages.kernel_packages must not be empty")
            invalid = [
                item
                for item in names
                if not re.match(r"^[A-Za-z0-9][A-Za-z0-9+._:-]*$", item)
            ]
            if invalid:
                raise ConfigError(
                    "packages.kernel_packages contains invalid names: {}".format(
                        ", ".join(invalid)
                    )
                )
        self.refresh_package_index = _as_bool(
            self._get(
                parser,
                "packages",
                "refresh_package_index",
                str(self.refresh_package_index),
            ),
            "packages.refresh_package_index",
        )
        self.package_min_free_gb = _as_int(
            self._get(
                parser,
                "packages",
                "min_free_gb",
                str(self.package_min_free_gb),
            ),
            "packages.min_free_gb",
        )

        self.update_initramfs = _as_bool(
            self._get(
                parser, "install", "update_initramfs", str(self.update_initramfs)
            ),
            "install.update_initramfs",
        )
        self.update_bootloader = _as_bool(
            self._get(
                parser, "install", "update_bootloader", str(self.update_bootloader)
            ),
            "install.update_bootloader",
        )

        known_sections = {"updater", "source", "build", "packages", "install"}
        for section in parser.sections():
            if section not in known_sections:
                LOGGER.warning("ignoring unknown config section [%s]", section)

    @property
    def state_path(self) -> Path:
        return self.work_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.work_dir / ".update.lock"

    @property
    def feed_cache_path(self) -> Path:
        return self.work_dir / "releases.json"

    @property
    def downloads_dir(self) -> Path:
        return self.work_dir / "downloads"

    @property
    def build_dir(self) -> Path:
        return self.work_dir / "build"


def load_config(path: Optional[str] = None) -> Config:
    """Load the explicit path, KAU_CONFIG, or the system default."""
    env_path = (os.environ.get("KAU_CONFIG") or "").strip()
    explicit = path is not None or bool(env_path)
    selected = Path(path) if path else Path(env_path or str(DEFAULT_CONFIG_PATH))
    return Config.load(selected, explicit=explicit)

