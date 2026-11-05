"""Persistent updater state."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class InstalledKernel:
    version: str
    release: str
    installed_at: str
    source: str


@dataclass
class State:
    last_check: Optional[str] = None
    last_check_version: Optional[str] = None
    last_update: Optional[str] = None
    running_at_update: Optional[str] = None
    last_error: Optional[str] = None
    installed: List[InstalledKernel] = field(default_factory=list)

    def record_check(self, version: str) -> None:
        self.last_check = utc_now()
        self.last_check_version = version

    def record_install(
        self, version: str, release: str, source: str, running: str
    ) -> None:
        self.installed = [item for item in self.installed if item.release != release]
        self.installed.insert(
            0,
            InstalledKernel(
                version=version,
                release=release,
                installed_at=utc_now(),
                source=source,
            ),
        )
        self.installed = self.installed[:20]
        self.last_update = utc_now()
        self.running_at_update = running
        self.last_error = None

    def record_error(self, message: str) -> None:
        self.last_error = "{}: {}".format(utc_now(), message)


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            backup = self.path.with_name(
                "{}.corrupt-{}".format(self.path.name, int(time.time()))
            )
            try:
                os.replace(str(self.path), str(backup))
                LOGGER.warning("状态文件已损坏，已移动到 %s: %s", backup, exc)
            except OSError:
                LOGGER.warning("无法读取状态文件 %s: %s", self.path, exc)
            return State()
        if not isinstance(payload, dict):
            LOGGER.warning("状态文件 %s 不是有效对象，已忽略", self.path)
            return State()

        installed = []
        for item in payload.get("installed", []):
            if not isinstance(item, dict):
                continue
            try:
                installed.append(
                    InstalledKernel(
                        version=str(item["version"]),
                        release=str(item["release"]),
                        installed_at=str(item.get("installed_at", "")),
                        source=str(item.get("source", "")),
                    )
                )
            except KeyError:
                continue

        return State(
            last_check=payload.get("last_check"),
            last_check_version=payload.get("last_check_version"),
            last_update=payload.get("last_update"),
            running_at_update=payload.get("running_at_update"),
            last_error=payload.get("last_error"),
            installed=installed,
        )

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(str(temporary), str(self.path))

