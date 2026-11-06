import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import zbark_device
import zbark_send


class SenderTests(unittest.TestCase):
    def test_missing_encryption_never_sends_plaintext_or_logs_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sender.json"
            zbark_device.save(config, {"base_url": "https://bark.sanbox.cc", "device_key": "private-fixture-key"})
            error = io.StringIO()
            with patch.object(sys, "argv", ["send", "private-fixture-body", "--config", str(config)]), \
                 patch.object(zbark_send, "command") as encrypt, patch.object(zbark_send, "send") as send, \
                 contextlib.redirect_stderr(error):
                self.assertEqual(zbark_send.main(), 1)
                encrypt.assert_not_called(); send.assert_not_called()
            self.assertNotIn("private-fixture-key", error.getvalue())
            self.assertNotIn("private-fixture-body", error.getvalue())
