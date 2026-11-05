"""Small wrapper around subprocess with dry-run support."""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Union

LOGGER = logging.getLogger(__name__)


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str = ""):
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr
        command = " ".join(shlex.quote(str(part)) for part in args)
        message = "command failed ({}): {}".format(returncode, command)
        if stderr.strip():
            message += "\n" + stderr.strip()
        super().__init__(message)


@dataclass
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    @staticmethod
    def which(command: str) -> Optional[str]:
        return shutil.which(command)

    def run(
        self,
        args: Sequence[Union[str, "object"]],
        cwd: Optional[Union[str, "object"]] = None,
        check: bool = True,
        capture: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> CommandResult:
        command = [str(part) for part in args]
        pretty = " ".join(shlex.quote(part) for part in command)
        if self.dry_run:
            LOGGER.info("[dry-run] %s", pretty)
            return CommandResult(command, 0)

        LOGGER.debug("running: %s", pretty)
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                text=True,
                capture_output=capture,
                check=False,
            )
        except OSError as exc:
            raise CommandError(command, 127, str(exc)) from exc

        stdout = (completed.stdout or "") if capture else ""
        stderr = (completed.stderr or "") if capture else ""
        result = CommandResult(command, completed.returncode, stdout, stderr)
        if check and completed.returncode != 0:
            raise CommandError(command, completed.returncode, result.stderr)
        return result

