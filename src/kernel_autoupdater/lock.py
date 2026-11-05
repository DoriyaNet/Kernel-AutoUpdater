"""Best-effort cross-process update lock."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOGGER = logging.getLogger(__name__)

try:  # pragma: no cover - only exercised on POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows development
    fcntl = None


class LockError(RuntimeError):
    """Raised when another updater instance is already running."""


@contextmanager
def update_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    locked = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                raise LockError(
                    "another kernel-autoupdater instance is running "
                    "(lock: {})".format(path)
                ) from exc
        else:  # pragma: no cover - Windows development
            LOGGER.debug("fcntl unavailable; skipping process lock")
        yield
    finally:
        if locked and fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()

