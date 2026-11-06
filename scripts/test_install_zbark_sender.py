from pathlib import Path
import tempfile
import plistlib
import unittest
from unittest.mock import patch

import install_zbark_sender as installer


class InstallerTests(unittest.TestCase):
    def test_interrupted_copy_never_publishes_bundle_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source"
            source.mkdir()
            for name in installer.FILES: (source / name).write_text("fixture " + name)
            config = home / ".config/zbark/sender.json"
            config.parent.mkdir(parents=True)
            config.write_text("fixture private config")
            original = Path.open
            def interrupted(path, *args, **kwargs):
                if ".install-" in str(path) and path.name == "zbark_run.py":
                    raise OSError("fixture disk failure")
                return original(path, *args, **kwargs)
            with patch.object(installer, "ROOT", source):
                with patch.object(Path, "open", interrupted), self.assertRaises(OSError):
                    installer.install(home)
                base = home / ".local/share/zbark"
                self.assertEqual(list(base.glob("sender-*")), [])
                self.assertEqual(list(base.glob(".install-*")), [])
                self.assertFalse((home / ".local/bin/zbark-send").exists())
                report = installer.install(home)
                installer.verify(Path(report["bundle"]) / "scripts", report["files"])
                self.assertTrue((home / ".local/bin/zbark-send").is_symlink())
                self.assertEqual(config.read_text(), "fixture private config")

    def test_unrelated_launcher_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            launcher = home / ".local/bin/zbark-send"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("existing user command")
            with self.assertRaises(ValueError): installer.install(home)
            self.assertEqual(launcher.read_text(), "existing user command")

    def test_schedule_pins_verified_bundle_and_preserves_previous_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest = {"bundle": str(home / ".local/share/zbark/sender-fixture")}
            first = installer.install_schedule(home, manifest)
            path = Path(first["plist"])
            value = plistlib.loads(path.read_bytes())
            self.assertEqual(value["ProgramArguments"][1], manifest["bundle"] + "/scripts/zbark_watch.py")
            self.assertEqual(value["StartCalendarInterval"], {"Minute": 45})
            self.assertFalse(first["activated"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_bytes()
            manifest["bundle"] += "-new"
            installer.install_schedule(home, manifest)
            saved = list(path.parent.glob("*.previous-*"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), original)
            self.assertNotIn("sender.json", path.read_text())


if __name__ == "__main__": unittest.main()
