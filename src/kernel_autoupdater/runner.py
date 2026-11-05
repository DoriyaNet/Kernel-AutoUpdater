"""Small wrapper around subprocess with dry-run support."""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Union

LOGGER = logging.getLogger(__name__)
_OUTPUT_SINK: Optional[Callable[[str], None]] = None
_OUTPUT_SINK_LOCK = threading.Lock()


@contextmanager
def command_output_sink(sink: Callable[[str], None]) -> Iterator[None]:
    """Route subprocess output to *sink* for the duration of the block."""

    global _OUTPUT_SINK
    with _OUTPUT_SINK_LOCK:
        previous = _OUTPUT_SINK
        _OUTPUT_SINK = sink
    try:
        yield
    finally:
        with _OUTPUT_SINK_LOCK:
            _OUTPUT_SINK = previous


def _current_output_sink() -> Optional[Callable[[str], None]]:
    with _OUTPUT_SINK_LOCK:
        return _OUTPUT_SINK


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str = ""):
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr
        command = " ".join(shlex.quote(str(part)) for part in args)
        message = "命令执行失败（{}）: {}".format(returncode, command)
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
        sink = _current_output_sink()
        if self.dry_run:
            LOGGER.info("[试运行] %s", pretty)
            if sink is not None:
                sink("[试运行] {}".format(pretty))
            return CommandResult(command, 0)

        LOGGER.debug("正在运行: %s", pretty)
        if sink is not None:
            return self._run_with_sink(
                command, cwd=cwd, check=check, capture=capture, env=env
            )
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

    def _run_with_sink(
        self,
        command: List[str],
        cwd: Optional[Union[str, "object"]] = None,
        check: bool = True,
        capture: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> CommandResult:
        sink = _current_output_sink()
        assert sink is not None
        if capture:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(cwd) if cwd is not None else None,
                    env=env,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                )
            except OSError as exc:
                raise CommandError(command, 127, str(exc)) from exc
            result = CommandResult(
                command,
                completed.returncode,
                completed.stdout or "",
                completed.stderr or "",
            )
            if check and completed.returncode != 0:
                raise CommandError(command, completed.returncode, result.stderr)
            return result

        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
        except OSError as exc:
            raise CommandError(command, 127, str(exc)) from exc

        if process.stdout is not None:
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                sink(line.rstrip("\n"))
            process.stdout.close()
        returncode = process.wait()
        result = CommandResult(command, returncode)
        if check and returncode != 0:
            raise CommandError(command, returncode)
        return result

