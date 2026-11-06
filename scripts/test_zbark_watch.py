import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import zbark_watch as watch


class WatchTests(unittest.TestCase):
    def credentials(self):
        return {"base_url": "https://bark.sanbox.cc", "device_key": "private-fixture-key",
                "crypto": {"algorithm": "AES256", "mode": "GCM", "padding": "noPadding", "key": "a" * 32}}

    def report(self, **changes):
        value = {"archive": "notify-data-20261106_015421-520eabc3.tar.gz.enc", "sha256": "a" * 64,
                 "created_at_utc": "2026-11-06T01:54:22+00:00", "verified": True, "restore_drill": True,
                 "network": "none", "live_data_mounted": False, "published_ports": False,
                 "restored_commit": "fixture-commit", "registration_count": 3, "registration_fingerprint": "fixture-fingerprint",
                 "live": {"registration_mode": "existing", "backup_timer": "active", "backup_service_result": "success"}}
        value.update(changes)
        return value

    def run_cycle(self, root, status="ok", now=1000, failure=None, credentials=None):
        with patch.object(watch, "check", return_value=(status, {"fixture": "recovery"})), \
             patch.object(watch, "protected_json", side_effect=lambda path: (credentials or self.credentials())
                          if path.name == "sender.json" else json.loads(path.read_text())), \
             patch.object(watch.time, "time", return_value=now), \
             patch.object(watch, "send", side_effect=failure, return_value={"id": "accepted"}) as transport:
            state = watch.cycle(root / "state", root / "mirror", root / "password", root / "sender.json")
            return state, transport

    def test_initial_health_silent_and_unchanged_fault_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, send = self.run_cycle(root)
            send.assert_not_called()
            state, send = self.run_cycle(root, "backup-check-failed")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(state["alert_result"], "accepted")
            _, send = self.run_cycle(root, "backup-check-failed", now=10000)
            send.assert_not_called()
            _, send = self.run_cycle(root, "ok", now=10001)
            self.assertEqual(send.call_count, 1)
            _, send = self.run_cycle(root, "ok", now=10002)
            send.assert_not_called()

    def test_transient_retry_same_ciphertext_minimum_hour_and_three_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, _ = self.run_cycle(root, "mirror-failed", failure=OSError("private URL"))
            first = copy.deepcopy(state["notification"])
            state, send = self.run_cycle(root, "mirror-failed", now=1100)
            send.assert_not_called()
            for now in (4600, 8200):
                state, send = self.run_cycle(root, "mirror-failed", now=now, failure=OSError())
                self.assertEqual(send.call_args.args[1], first["envelope"])
            state, send = self.run_cycle(root, "mirror-failed", now=11800)
            send.assert_not_called()
            self.assertEqual(state["notification"]["attempts"], 3)
            text = (root / "state/state.json").read_text()
            for secret in ["private-fixture-key", "a" * 32, "private URL", watch.TEXT["mirror-failed"]]:
                self.assertNotIn(secret, text)

    def test_failed_alert_recovers_once_and_supersedes_old_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, _ = self.run_cycle(root, "mirror-failed", failure=OSError())
            old = state["notification"]["envelope"]
            state, send = self.run_cycle(root, now=1001)
            self.assertNotEqual(send.call_args.args[1], old)
            self.assertIsNone(state["notification"])
            _, send = self.run_cycle(root, now=5000)
            send.assert_not_called()

    def test_expired_alert_is_not_sent_and_configuration_change_blocks_retarget(self):
        for expired in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.run_cycle(root, "mirror-failed", failure=OSError())
                changed = self.credentials() | {"device_key": "changed-key"}
                state, send = self.run_cycle(root, "mirror-failed", now=1000 + (watch.ALERT_LIFETIME if expired else 3600),
                                            credentials=changed)
                send.assert_not_called()
                self.assertEqual(state["alert_result"], "expired" if expired else "configuration-changed")

    def test_http_rejection_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cycle(root, "mirror-failed", failure=ValueError("provider rejection"))
            state, send = self.run_cycle(root, "mirror-failed", now=5000)
            send.assert_not_called()
            self.assertTrue(state["notification"]["blocked"])

    def test_state_is_private_and_missed_schedule_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cycle(root)
            self.assertEqual((root / "state").stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "state/state.json").stat().st_mode & 0o777, 0o600)
            self.assertFalse(watch.summary({"status": "ok", "checked_at": time.time()})["check_stale"])
            self.assertTrue(watch.summary({"status": "ok", "checked_at": 0})["check_stale"])

    def test_invalid_report_never_reaches_copy_or_remote_shell(self):
        now = watch.dt.datetime.fromisoformat(self.report()["created_at_utc"]).timestamp()
        for changes in [{"archive": "../../private; echo unsafe"}, {"restore_drill": False},
                        {"created_at_utc": "2000-01-01T00:00:00+00:00"}, {"network": "bridge"},
                        {"sha256": "invalid"}, {"published_ports": True},
                        {"live": {"registration_mode": "open"}}]:
            with self.assertRaises(ValueError): watch.validate_report(self.report(**changes), now)

    def make_archive(self, root, corrupt=False, unsafe=False):
        files = {"metadata.json": json.dumps({"created_at_utc": self.report()["created_at_utc"], "info": {"commit": "fixture-commit"},
                                            "database": {"registration_count": 3, "registration_fingerprint": "fixture-fingerprint"}}).encode()}
        for name in ["hub/bark-data/bark.db", "hub/ntfy-cache/cache.db", "hub/ntfy-lib/user.db", "runtime-image.tar",
                     "hub/docker-compose.yml", "hub/Caddyfile", "hub/bark-certs/key", "db-probe"]:
            files[name] = b"fixture-private-data"
        manifest = {"format": 1, "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
        if corrupt: files["hub/bark-data/bark.db"] = b"changed"
        files["manifest.json"] = json.dumps(manifest).encode()
        if unsafe: files["../escape"] = b"unsafe"
        plain = root / "fixture.tar.gz"
        with tarfile.open(plain, "w:gz") as tar:
            for name, content in files.items():
                info = tarfile.TarInfo(name); info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        password = root / "password"
        password.write_text("fixture-password"); password.chmod(0o600)
        archive = root / "fixture.enc"
        subprocess.run(["/usr/bin/openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                        "-pass", "file:" + str(password), "-in", str(plain), "-out", str(archive)],
                       check=True, capture_output=True)
        plain.unlink()
        return archive, password

    def test_real_encrypted_manifest_verification_and_corruption_rejection(self):
        for corrupt, unsafe in [(False, False), (True, False), (False, True)]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive, password = self.make_archive(root, corrupt, unsafe)
                stage = root / "private"; stage.mkdir(mode=0o700)
                if corrupt or unsafe:
                    with self.assertRaises((ValueError, tarfile.TarError)): watch.verify_encrypted(archive, password, self.report())
                else:
                    result = watch.verify_encrypted(archive, password, self.report())
                    self.assertTrue(result["all_manifest_hashes_match"])
                    self.assertFalse((stage / "archive.tar.gz").exists())
                self.assertFalse((root / "escape").exists())

    def test_failed_copy_or_verification_preserves_older_copies(self):
        for failure in [True, False]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "mirror"; root.mkdir(mode=0o700)
                previous = root / "notify-data-20261105_015421-520eabc3"; previous.mkdir()
                (previous / "keep").write_text("previous recovery point")
                def copy_archive(_, output): output.write(b"corrupt")
                report = self.report(sha256=hashlib.sha256(b"corrupt").hexdigest())
                with patch.object(watch, "validate_report", return_value=(report["archive"], report["sha256"])), \
                     patch.object(watch, "ssh", side_effect=OSError() if failure else copy_archive), \
                     patch.object(watch, "verify_encrypted", side_effect=ValueError("corruption")):
                    with self.assertRaises((OSError, ValueError)): watch.mirror(report, root, root / "password")
                self.assertEqual((previous / "keep").read_text(), "previous recovery point")
                self.assertEqual(list(root.glob(".copy-*")), [])
                self.assertEqual(len(list(root.iterdir())), 1)

    def test_wrong_password_and_shared_password_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, password = self.make_archive(root)
            for shared in (True, False):
                password.chmod(0o644 if shared else 0o600)
                if not shared: password.write_text("wrong-password")
                with self.assertRaises((ValueError, tarfile.TarError)): watch.verify_encrypted(archive, password, self.report())

    def test_successful_new_copy_prunes_only_its_seven_completed_mirrors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "mirror"; root.mkdir(mode=0o700)
            baseline = root / "20261106-520eabc3"; baseline.mkdir()
            (baseline / "keep").write_text("untouched")
            for day in range(1, 9):
                old = root / f"notify-data-202610{day:02d}_015421-520eabc3"
                old.mkdir(mode=0o700)
                (old / "verification.json").write_text("{}")
            report = self.report(sha256=hashlib.sha256(b"encrypted fixture").hexdigest())
            def copy_archive(_, output): output.write(b"encrypted fixture")
            with patch.object(watch, "validate_report", return_value=(report["archive"], report["sha256"])), \
                 patch.object(watch, "ssh", side_effect=copy_archive), \
                 patch.object(watch, "verify_encrypted", return_value={"encrypted_sha256": report["sha256"]}) as verify:
                watch.mirror(report, root, root / "password")
                verify.assert_called_once()
                watch.mirror(report, root, root / "password")
                self.assertEqual(verify.call_count, 2)  # unchanged ciphertext still needs the current recovery password
            self.assertEqual(len(list(root.glob("notify-data-*"))), 7)
            self.assertEqual((baseline / "keep").read_text(), "untouched")
            latest = root / report["archive"].removesuffix(".tar.gz.enc") / report["archive"]
            latest.write_bytes(b"corruption")
            with patch.object(watch, "validate_report", return_value=(report["archive"], report["sha256"])), \
                 self.assertRaises(ValueError): watch.mirror(report, root, root / "password")

    def test_failed_status_does_not_copy_and_valid_status_with_failed_mirror_is_distinct(self):
        with patch.object(watch, "ssh", side_effect=OSError()), patch.object(watch, "mirror") as mirror:
            self.assertEqual(watch.check(Path("fixture"), Path("password"))[0], "backup-check-failed")
            mirror.assert_not_called()
        with patch.object(watch, "ssh", return_value=json.dumps(self.report()).encode()), \
             patch.object(watch, "validate_report"), patch.object(watch, "mirror", side_effect=ValueError()):
            self.assertEqual(watch.check(Path("fixture"), Path("password"))[0], "mirror-failed")

    def test_cached_copy_detects_password_loss_and_corruption_without_replacing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, password = self.make_archive(root)
            mirror = root / "mirror"
            report = self.report(sha256=watch.digest(archive))
            data = archive.read_bytes()
            def copy_archive(_, output): output.write(data)
            with patch.object(watch, "validate_report", return_value=(report["archive"], report["sha256"])), \
                 patch.object(watch, "ssh", side_effect=copy_archive) as remote:
                watch.mirror(report, mirror, password)
                password.unlink()
                with self.assertRaises(ValueError): watch.mirror(report, mirror, password)
                password.write_text("wrong-password"); password.chmod(0o600)
                with self.assertRaises((ValueError, tarfile.TarError)): watch.mirror(report, mirror, password)
                password.write_text("fixture-password")
                self.assertTrue(watch.mirror(report, mirror, password)["password_decryption"])
                self.assertEqual(remote.call_count, 1)
                restored = mirror / report["archive"].removesuffix(".tar.gz.enc") / report["archive"]
                self.assertEqual(restored.read_bytes(), data)


if __name__ == "__main__": unittest.main()
