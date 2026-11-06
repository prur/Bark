import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import zbark_run as runner


class CompletionTests(unittest.TestCase):
    def config(self):
        return {"base_url": runner.SERVER, "device_key": "fixture-private-key",
                "crypto": {"algorithm": "AES256", "mode": "GCM", "padding": "noPadding", "key": "a" * 32}}

    def test_failure_retains_only_ciphertext_and_explicit_retry_reuses_same_id(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory) / "outbox"
            credentials = self.config()
            payload = {"id": "fixture-id", "body": "fixture-private-body"}
            path = runner.enqueue(outbox, credentials, payload)
            record = json.loads(path.read_text())
            for secret in ["fixture-private-key", "fixture-private-body", "a" * 32]:
                self.assertNotIn(secret, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch.object(runner, "send", side_effect=ValueError("provider failure")):
                with self.assertRaises(ValueError): runner.deliver(outbox, credentials)
            self.assertTrue(path.exists())
            with patch.object(runner, "send", return_value={"id": "fixture-id"}) as send:
                self.assertEqual(runner.deliver(outbox, credentials), [{"id": "fixture-id"}])
                self.assertEqual(send.call_args.args[1], record["envelope"])
            self.assertFalse(path.exists())

    def test_changed_key_never_retargets_a_pending_event(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory) / "outbox"
            credentials = self.config()
            path = runner.enqueue(outbox, credentials, {"id": "fixture-id"})
            credentials["device_key"] = "replacement-key"
            with patch.object(runner, "send") as send:
                with self.assertRaises(ValueError): runner.deliver(outbox, credentials)
                send.assert_not_called()
            self.assertTrue(path.exists())

    def test_command_output_and_arguments_are_not_copied_into_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["run", "--label", "Fixture task", "--outbox", str(Path(directory) / "outbox"),
                    "--", "fixture-command", "private-command-argument"]
            with patch.object(sys, "argv", args), patch.object(runner, "protected_json", return_value=self.config()), \
                 patch.object(runner, "validate"), \
                 patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)), \
                 patch.object(runner, "enqueue", return_value=Path(directory) / "event") as enqueue, \
                 patch.object(runner, "deliver", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(), 3)
            payload = enqueue.call_args.args[2]
            self.assertNotIn("private-command-argument", json.dumps(payload))
            self.assertIn("exit 3", payload["body"])

    def test_notification_failure_is_visible_but_preserves_command_failure(self):
        for code in [0, 7]:
            with tempfile.TemporaryDirectory() as directory:
                args = ["run", "--label", "Fixture", "--outbox", str(Path(directory) / "outbox"), "--", "fixture"]
                with patch.object(sys, "argv", args), patch.object(runner, "protected_json", return_value=self.config()), \
                     patch.object(runner, "validate"), \
                     patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], code)), \
                     patch.object(runner, "enqueue", return_value=Path(directory) / "event"), \
                     patch.object(runner, "deliver", side_effect=ValueError("fixture")), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(runner.main(), code or 75)

    def test_shared_or_linked_outbox_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared"
            path.mkdir(mode=0o755)
            with self.assertRaises(ValueError): runner.private_directory(path)
            path.chmod(0o700)
            link = Path(directory) / "linked"
            link.symlink_to(path)
            with self.assertRaises(ValueError): runner.private_directory(link)


if __name__ == "__main__": unittest.main()
