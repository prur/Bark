from pathlib import Path
import tempfile
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


if __name__ == "__main__": unittest.main()
