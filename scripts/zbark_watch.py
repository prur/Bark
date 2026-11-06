#!/usr/bin/env python3
"""Hourly backup health, verified off-host copies and encrypted transition alerts."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

from zbark_device import protected_json, send
from zbark_run import command, fingerprint, locked, private_directory, ROOT, sync_directory

HOST = "ovh-us-vps"
BACKUPS = "/opt/notify-hub/backups/"
NAME = re.compile(r"notify-data-\d{8}_\d{6}-[0-9a-f]{8}\.tar\.gz\.enc")
MAX_ARCHIVE = 256 * 1024**2
ALERT_LIFETIME = 6 * 3600
MAX_ATTEMPTS = 3
TEXT = {"ok": "备份检查已恢复；最新加密副本已在 Mac 校验。",
        "backup-check-failed": "服务器备份检查失败；请检查连接、备份新鲜度、定时器及注册策略。",
        "mirror-failed": "离机备份校验失败；旧副本已保留，请检查空间、校验和及独立保存的恢复口令。"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_json(path, value):
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with open(temporary, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def ssh(remote, output=None):
    result = subprocess.run(["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                             "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o",
                             "ServerAliveCountMax=2", HOST, remote], stdout=output or subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=120)
    if result.returncode:
        raise ValueError("Remote backup operation failed")
    return result.stdout


def validate_report(report, now):
    if not isinstance(report, dict):
        raise ValueError("Invalid backup report")
    name, checksum = report["archive"], report["sha256"]
    created = dt.datetime.fromisoformat(report["created_at_utc"])
    if created.utcoffset() is None:
        raise ValueError("Backup time must include its timezone")
    age = now - created.timestamp()
    live = report["live"]
    if not isinstance(live, dict):
        raise ValueError("Missing live backup health")
    if (not NAME.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or
            not -300 <= age <= 36 * 3600 or report.get("verified") is not True or
            report.get("restore_drill") is not True or report.get("network") != "none" or
            report.get("live_data_mounted") is not False or report.get("published_ports") is not False or
            live.get("registration_mode") != "existing" or live.get("backup_timer") != "active" or
            live.get("backup_service_result") != "success"):
        raise ValueError("Invalid or stale backup report")
    return name, checksum


def verify_encrypted(archive, password, report):
    if (password.is_symlink() or not password.is_file() or password.stat().st_uid != os.getuid() or
            password.stat().st_mode & 0o077):
        raise ValueError("Recovery password must be an existing owner-only file")
    # Stream decrypted bytes through a pipe: even forced termination leaves no plaintext file.
    process = subprocess.Popen(["/usr/bin/openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                                "-pass", "file:" + str(password), "-in", str(archive)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = threading.Timer(120, process.kill)
    deadline.start()
    hashes, seen, total, manifest, metadata = {}, set(), 0, None, None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|gz") as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                normalized = str(path)
                total += member.size
                if (path.is_absolute() or ".." in path.parts or normalized in ("", ".") or
                        normalized in seen or not (member.isfile() or member.isdir()) or total > 4 * 1024**3):
                    raise ValueError("Unsafe or oversized recovery archive")
                seen.add(normalized)
                if not member.isfile():
                    continue
                with tar.extractfile(member) as source:
                    if normalized in ("manifest.json", "metadata.json"):
                        if member.size > 1024**2:
                            raise ValueError("Oversized recovery metadata")
                        data = source.read()
                        if normalized == "manifest.json":
                            manifest = json.loads(data)
                        else:
                            metadata = json.loads(data)
                            hashes[normalized] = hashlib.sha256(data).hexdigest()
                    else:
                        hashes[normalized] = hashlib.file_digest(source, "sha256").hexdigest()
        while process.stdout.read(1024**2): pass
        if process.wait(timeout=5):
            raise ValueError("Recovery decryption failed")
    finally:
        deadline.cancel()
        if process.poll() is None: process.kill()
        process.stdout.close()
        process.wait()
    required = {"metadata.json", "hub/bark-data/bark.db", "hub/ntfy-cache/cache.db", "hub/ntfy-lib/user.db",
                "hub/docker-compose.yml", "hub/Caddyfile", "runtime-image.tar", "db-probe"}
    if (not isinstance(manifest, dict) or not isinstance(metadata, dict) or manifest.get("format") != 1 or manifest.get("files") != hashes or
            not required.issubset(hashes) or not any(p.startswith("hub/bark-certs/") for p in hashes) or
            metadata["created_at_utc"] != report["created_at_utc"] or
            metadata["info"]["commit"] != report["restored_commit"] or
            metadata["database"]["registration_fingerprint"] != report["registration_fingerprint"] or
            metadata["database"]["registration_count"] != report["registration_count"]):
        raise ValueError("Recovery manifest or source does not match")
    return {"encrypted_sha256": digest(archive), "manifest_files": len(hashes),
            "password_decryption": True, "all_manifest_hashes_match": True, "plaintext_written": False,
            "server_isolated_drill": True, "local_runtime_drill": False}


def mirror(report, root, password):
    name, checksum = validate_report(report, time.time())
    private_directory(root)
    destination = root / name.removesuffix(".tar.gz.enc")
    if destination.exists():
        private_directory(destination)
        verification = protected_json(destination / "verification.json")
        if verification["encrypted_sha256"] != checksum or digest(destination / name) != checksum:
            raise ValueError("Existing off-host copy changed; preserve it for diagnosis")
        # A cached successful drill cannot prove that the recovery password still works.
        return verify_encrypted(destination / name, password, report)
    with tempfile.TemporaryDirectory(prefix=".copy-", dir=root) as directory:
        stage = Path(directory)
        archive = stage / name
        with archive.open("xb") as output:
            # Name is strictly validated before entering the remote shell; bound the copy size.
            ssh(f"sudo head -c {MAX_ARCHIVE + 1} -- {BACKUPS}{name}", output)
            output.flush()
            os.fsync(output.fileno())
        archive.chmod(0o600)
        if archive.stat().st_size > MAX_ARCHIVE or digest(archive) != checksum:
            raise ValueError("Off-host copy checksum mismatch or archive too large")
        verification = verify_encrypted(archive, password, report)
        atomic_json(stage / "verification.json", verification)
        atomic_json(stage / "report.json", report)
        (stage / (name + ".sha256")).write_text(checksum + "\n")
        (stage / (name + ".sha256")).chmod(0o600)
        stage.rename(destination)
        sync_directory(root)
    # Only this dedicated mirror is managed. Baseline recovery/ is never pruned.
    completed = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and
                       NAME.fullmatch(p.name + ".tar.gz.enc") and (p / "verification.json").is_file())
    for old in completed[:-7]:
        if old != destination:
            shutil.rmtree(old)
    return verification


def check(root, password):
    try:
        report = json.loads(ssh("sudo /usr/local/bin/zbark-backup status"))
        validate_report(report, time.time())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return "backup-check-failed", None
    try:
        verified = mirror(report, root, password)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError):
        return "mirror-failed", None
    return "ok", {"archive": report["archive"], "created_at_utc": report["created_at_utc"], **verified}


def event(credentials, status, now):
    identifier = "zbark-watch-" + str(uuid.uuid4())
    payload = {"id": identifier, "title": "Z Bark 备份看护", "body": TEXT[status],
               "group": "Z Bark 运维", "isArchive": "1"}
    envelope = json.loads(command(["node", str(ROOT / "scripts/zbark_encrypt.cjs")],
                                  json.dumps({"payload": payload, "fields": credentials["crypto"]}).encode()))
    return {"id": identifier, "credential_fingerprint": fingerprint(credentials), "envelope": envelope,
            "expires_at": now + ALERT_LIFETIME, "attempts": 0, "blocked": False}


def cycle(state_dir, mirror_root, password, config):
    with locked(state_dir):
        path = state_dir / "state.json"
        state = protected_json(path) if path.exists() else {"format": 1}
        now = time.time()
        status, recovery = check(mirror_root, password)
        previous = state.get("status")
        if previous != status:
            state["notification"] = None  # Supersede stale failure/recovery; never replay old health.
            state.pop("accepted", None)
            state.pop("alert_result", None)
            state["notification_needed"] = previous is not None or status != "ok"
        state.update({"status": status, "checked_at": now, "recovery": recovery})
        if status == "ok": state["last_good_at"] = now
        atomic_json(path, state)
        try:
            if state.get("notification_needed") or state.get("notification"):
                credentials = protected_json(config)
                if (not isinstance(credentials, dict) or not credentials.get("crypto") or
                        credentials.get("base_url") != "https://bark.sanbox.cc"):
                    raise ValueError("Expected the existing encrypted sender configuration")
                if state.get("notification_needed"):
                    state["notification"] = event(credentials, status, now)
                    state["notification_needed"] = False
                notification = state["notification"]
                if now >= notification["expires_at"]:
                    state["notification"] = None
                    state["alert_result"] = "expired"
                elif notification["credential_fingerprint"] != fingerprint(credentials):
                    notification["blocked"] = True
                    state["alert_result"] = "configuration-changed"
                elif (not notification["blocked"] and notification["attempts"] < MAX_ATTEMPTS and
                      now >= notification.get("next_attempt_at", 0)):
                    # Persist the attempt BEFORE transport: crash/ambiguous acceptance consumes a retry.
                    notification["attempts"] += 1
                    notification["next_attempt_at"] = now + 3600
                    state["alert_result"] = "pending"
                    atomic_json(path, state)
                    try:
                        accepted = send(credentials, notification["envelope"], "https://bark.sanbox.cc")
                    except ValueError:
                        notification["blocked"] = True  # HTTP/application rejection needs explicit diagnosis.
                        state["alert_result"] = "rejected"
                    else:
                        state["accepted"] = accepted
                        state["notification"] = None
                        state["alert_result"] = "accepted"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            state["alert_result"] = "failed"
        atomic_json(path, state)
        return state


def summary(state):
    return {k: state[k] for k in ["status", "checked_at", "last_good_at", "recovery", "alert_result", "accepted"] if k in state} | {
        "pending_alert": bool(state.get("notification") or state.get("notification_needed")),
        "check_stale": not -300 <= time.time() - state.get("checked_at", 0) <= 2 * 3600,
        "alert_attempts": (state.get("notification") or {}).get("attempts", 0),
        "alert_blocked": (state.get("notification") or {}).get("blocked", False)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Read last observation; never contact the server or send")
    action.add_argument("--retry-alert", action="store_true", help="After diagnosis, request one new current-state alert (may repeat a banner)")
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/zbark/backup-watch")
    parser.add_argument("--mirror", type=Path, default=Path.home() / ".local/share/zbark/backup-mirror")
    parser.add_argument("--password-file", type=Path, default=Path.home() / ".config/zbark/recovery/notify-backup.pass")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/zbark/sender.json")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.status:
            with locked(args.state): state = protected_json(args.state / "state.json")
        else:
            if args.retry_alert:
                with locked(args.state):
                    path = args.state / "state.json"
                    state = protected_json(path)
                    state.update({"notification_needed": True, "notification": None})
                    atomic_json(path, state)
            state = cycle(args.state, args.mirror, args.password_file, args.config)
        result = summary(state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] != "ok" or result["check_stale"]: return 1
        alert_failed = state.get("alert_result") in ("expired", "failed", "rejected", "configuration-changed")
        return 75 if result["pending_alert"] or alert_failed else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Backup watch failed; existing recovery points and keys preserved. Inspect private state and --status.", file=sys.stderr)
        return 75


if __name__ == "__main__": sys.exit(main())
