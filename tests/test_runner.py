import sys
import tempfile
import unittest
from pathlib import Path

from kernel_autoupdater.runner import CommandError, CommandRunner


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


if __name__ == "__main__":
    unittest.main()

