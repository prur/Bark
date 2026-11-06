import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.error
import urllib.request

import zbark_device as device


class DeviceValidationTests(unittest.TestCase):
    def test_credentials_require_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text('{"device_key":"test-secret"}')
            path.chmod(0o644)
            with self.assertRaises(ValueError): device.protected_json(path)
            path.chmod(0o600)
            self.assertEqual(device.protected_json(path)["device_key"], "test-secret")

    def test_invalid_destination_and_batch_override_never_send(self):
        with patch.object(urllib.request, "build_opener") as network:
            for base in ["http://example.com", "https://other.example", "https://user@example.com", "https://example.com/path", "https://example.com?key=secret"]:
                with self.subTest(base=base), self.assertRaises(ValueError):
                    device.send({"base_url": base, "device_key": "secret"}, {"id": "test"}, "https://example.com")
            for field in ["device_key", "device_keys", "Device_Keys", "key", "devicetoken", "device_token"]:
                with self.subTest(field=field), self.assertRaises(ValueError):
                    device.send({"base_url": "https://example.com", "device_key": "secret"}, {"id": "test", field: "override"}, "https://example.com")
            network.assert_not_called()

    def test_http_redirect_does_not_forward_secret(self):
        received = []
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(307)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/push", data=b"test-secret")
            with self.assertRaises(urllib.error.HTTPError) as rejection:
                urllib.request.build_opener(urllib.request.ProxyHandler({}), device.NoRedirect()).open(request, timeout=5)
            rejection.exception.close()
            self.assertEqual(received, ["/push"])
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_provider_response_and_error_never_expose_key_or_body(self):
        secret = "private-device-key"
        response = urllib.error.HTTPError("https://example.com/push", 400, secret, {}, io.BytesIO(secret.encode()))
        with patch.object(urllib.request.OpenerDirector, "open", side_effect=response):
            with self.assertRaises(ValueError) as error:
                device.send({"base_url": "https://example.com", "device_key": secret}, {"id": "test"}, "https://example.com")
        self.assertNotIn(secret, str(error.exception))
        self.assertIn("400", str(error.exception))

    def test_history_comparison_requires_all_unexpired_rows_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            before = {"rows": {"old": {"digest": "unchanged", "expires": None}, "expired": {"digest": "expired", "expires": 1}}}
            after = {"rows": {"old": {"digest": "unchanged", "expires": None}, "new": {"digest": "new", "expires": None}}}
            for name, data in [("before", before), ("after", after)]:
                (directory / name).write_text(json.dumps(data))
            args = type("Args", (), {"before": directory / "before", "after": directory / "after", "output": directory / "report"})()
            with contextlib.redirect_stdout(io.StringIO()): device.compare(args)
            self.assertTrue(json.loads(args.output.read_text())["passed"])
            after["rows"]["old"]["digest"] = "changed"
            (directory / "after").write_text(json.dumps(after)); args.output = directory / "failure"
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError): device.compare(args)

    def test_evidence_is_private_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            device.save(path, {"result": "original"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): device.save(path, {"result": "replacement"})
            self.assertEqual(json.loads(path.read_text())["result"], "original")


if __name__ == "__main__": unittest.main()
