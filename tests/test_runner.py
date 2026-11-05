import sys
import tempfile
import unittest
from pathlib import Path

from kernel_autoupdater.runner import (
    CommandError,
    CommandRunner,
    command_output_sink,
)


class RunnerTests(unittest.TestCase):
    def test_dry_run_does_not_execute(self):
        runner = CommandRunner(dry_run=True)
        result = runner.run(["definitely-not-a-real-command-xyz"])
        self.assertEqual(result.returncode, 0)

    def test_capture_stdout(self):
        runner = CommandRunner()
        result = runner.run(
            [sys.executable, "-c", "print('hello')"], capture=True
        )
        self.assertEqual(result.stdout.strip(), "hello")

    def test_failure_raises(self):
        runner = CommandRunner()
        with self.assertRaises(CommandError):
            runner.run(
                [sys.executable, "-c", "import sys; sys.exit(3)"], capture=True
            )

    def test_failure_can_be_ignored(self):
        runner = CommandRunner()
        result = runner.run(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            capture=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)

    def test_missing_binary(self):
        runner = CommandRunner()
        with self.assertRaises(CommandError):
            runner.run(["definitely-not-a-real-command-xyz"])

    def test_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = CommandRunner()
            result = runner.run(
                [sys.executable, "-c", "import os; print(os.getcwd())"],
                cwd=Path(temp),
                capture=True,
            )
            self.assertEqual(Path(result.stdout.strip()).resolve(), Path(temp).resolve())

    def test_output_sink_streams_stdout_and_stderr(self):
        lines = []
        runner = CommandRunner()
        with command_output_sink(lines.append):
            result = runner.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('hello'); print('oops', file=sys.stderr)",
                ]
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", lines)
        self.assertIn("oops", lines)

    def test_output_sink_does_not_duplicate_captured_queries(self):
        lines = []
        runner = CommandRunner()
        with command_output_sink(lines.append):
            result = runner.run(
                [sys.executable, "-c", "print('secret')"],
                capture=True,
            )
        self.assertEqual(result.stdout.strip(), "secret")
        self.assertEqual(lines, [])

    def test_output_sink_keeps_captured_stderr_separate(self):
        lines = []
        runner = CommandRunner()
        with command_output_sink(lines.append):
            result = runner.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('out'); print('err', file=sys.stderr)",
                ],
                capture=True,
                check=False,
            )
        self.assertEqual(result.stdout.strip(), "out")
        self.assertEqual(result.stderr.strip(), "err")
        self.assertEqual(lines, [])

    def test_output_sink_flushes_a_final_line_without_newline(self):
        lines = []
        runner = CommandRunner()
        with command_output_sink(lines.append):
            result = runner.run(
                [sys.executable, "-c", "print('last', end='')"]
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["last"])


if __name__ == "__main__":
    unittest.main()

