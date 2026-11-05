import json
import tempfile
import unittest
from pathlib import Path

from kernel_autoupdater.state import StateStore


class StateTests(unittest.TestCase):
    def test_roundtrip_and_record_install(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            store = StateStore(path)
            state = store.load()
            state.record_check("7.3.1")
            state.record_install("7.3.1", "7.3.1-kau", "https://example/linux.tar.xz", "6.8.0")
            store.save(state)

            loaded = store.load()
            self.assertEqual(loaded.last_check_version, "7.3.1")
            self.assertEqual(len(loaded.installed), 1)
            self.assertEqual(loaded.installed[0].release, "7.3.1-kau")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["installed"][0]["version"], "7.3.1")

    def test_install_replaces_same_release(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.json")
            state = store.load()
            state.record_install("7.3.1", "7.3.1-kau", "x", "6.8.0")
            state.record_install("7.3.1", "7.3.1-kau", "x", "6.8.0")
            self.assertEqual(len(state.installed), 1)

    def test_corrupt_state_is_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            state = StateStore(path).load()
            self.assertEqual(state.installed, [])
            self.assertFalse(path.exists())
            self.assertTrue(list(Path(temp).glob("state.json.corrupt-*")))

    def test_error_is_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.json")
            state = store.load()
            state.record_error("build failed")
            store.save(state)
            self.assertIn("build failed", store.load().last_error)


if __name__ == "__main__":
    unittest.main()

