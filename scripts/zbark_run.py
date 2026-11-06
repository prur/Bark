#!/usr/bin/env python3
"""Notify command completion with encrypted, durable, explicitly retried events."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from zbark_device import ROOT, command, protected_json, send

SERVER = "https://bark.sanbox.cc"


def fingerprint(credentials):
    return hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("Outbox must be an owner-only directory (chmod 700)")


@contextlib.contextmanager
def locked(outbox):
    private_directory(outbox)
    descriptor = os.open(outbox / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def sweep_reservations(outbox):
    # Locks disappear on process exit, so interrupted commands cannot leak capacity.
    for path in outbox.glob("*.reserved"):
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: continue
            path.unlink()
        finally: os.close(fd)


@contextlib.contextmanager
def reservation(outbox):
    with locked(outbox):
        sweep_reservations(outbox)
        if len(list(outbox.glob("*.json"))) + len(list(outbox.glob("*.reserved"))) >= 256:
            raise ValueError("Outbox is full")
        path = outbox / (uuid.uuid4().hex + ".reserved")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield path
    finally:
        with locked(outbox):
            path.unlink(missing_ok=True)
            os.close(fd)


def enqueue(outbox, credentials, payload, reserved=None):
    envelope = json.loads(command(["node", str(ROOT / "scripts/zbark_encrypt.cjs")],
                                  json.dumps({"payload": payload, "fields": credentials["crypto"]}).encode()))
    identifier = payload["id"]
    record = {"format": 1, "id": identifier, "credential_fingerprint": fingerprint(credentials),
              "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "envelope": envelope}
    with locked(outbox):
        sweep_reservations(outbox)
        if reserved is not None and not reserved.exists():
            raise ValueError("Command completion reservation is missing")
        if reserved is None and len(list(outbox.glob("*.json"))) + len(list(outbox.glob("*.reserved"))) >= 256:
            raise ValueError("Outbox is full; inspect pending events before running another command")
        path = outbox / (identifier + ".json")
        pending = path.with_suffix(".partial")
        with open(pending, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        if reserved is not None: reserved.unlink()
        sync_directory(outbox)
    return path


def validate(credentials, outbox):
    if not credentials.get("crypto") or credentials.get("base_url") != SERVER or not credentials.get("device_key"):
        raise ValueError("Expected the existing Z Bark encrypted sender configuration")
    # Exercise the actual encryption dependency before executing the user's task.
    command(["node", str(ROOT / "scripts/zbark_encrypt.cjs")],
            json.dumps({"payload": {"id": "preflight"}, "fields": credentials["crypto"]}).encode())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def deliver(outbox, credentials, only=None):
    results = []
    with locked(outbox):
        paths = [only] if only else sorted(outbox.glob("*.json"))[:10]
        for path in paths:
            if not path.exists(): continue  # Another process already acknowledged this event.
            record = protected_json(path)
            if record.get("format") != 1 or record.get("credential_fingerprint") != fingerprint(credentials):
                raise ValueError("Pending event uses a different configuration; preserve it and inspect key changes")
            result = send(credentials, record["envelope"], SERVER)
            path.unlink()
            sync_directory(outbox)
            results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="Public-safe task name; command arguments/output are never included")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/zbark/sender.json")
    parser.add_argument("--outbox", type=Path, default=Path.home() / ".local/state/zbark/outbox")
    parser.add_argument("--flush", action="store_true", help="Explicitly retry at most 10 pending encrypted events")
    parser.add_argument("--status", action="store_true", help="Print pending count without sending")
    parser.add_argument("task", nargs=argparse.REMAINDER, help="-- executable [arguments...]; no implicit shell")
    args = parser.parse_args()
    exit_code = 0
    pending = None
    try:
        if args.flush or args.status:
            if args.task or args.label or args.flush and args.status:
                parser.error("choose one of command execution, --flush, or --status")
            if args.status:
                with locked(args.outbox):
                    sweep_reservations(args.outbox)
                    print(json.dumps({"pending": len(list(args.outbox.glob('*.json'))),
                                      "running": len(list(args.outbox.glob('*.reserved')))}))
                return 0
            credentials = protected_json(args.config)
            results = deliver(args.outbox, credentials)
            print(json.dumps({"accepted": results, "boundary": "provider acceptance; device receipt is separate"}))
            return 0
        task = args.task[1:] if args.task[:1] == ["--"] else args.task
        if not args.label or not task: parser.error("provide --label and -- executable [arguments...]")
        # Fail before starting work if it cannot produce an encrypted completion event.
        credentials = protected_json(args.config)
        validate(credentials, args.outbox)
        with reservation(args.outbox) as reserved:
            started = time.monotonic()
            try:
                exit_code = subprocess.run(task).returncode
            except OSError:
                exit_code = 127
            except KeyboardInterrupt:
                exit_code = 130
            exit_code = exit_code if exit_code >= 0 else 128 - exit_code
            payload = {"id": "zbark-task-" + str(uuid.uuid4()), "title": args.label,
                       "body": f"{'完成' if exit_code == 0 else '失败'} · exit {exit_code} · {time.monotonic() - started:.1f}s",
                       "group": "Z Bark 运维", "isArchive": "1"}
            path = enqueue(args.outbox, credentials, payload, reserved)
            pending = path
        results = deliver(args.outbox, credentials, path)
        print(json.dumps({"task_exit": exit_code, "accepted": results, "encrypted": True}, ensure_ascii=False))
        return exit_code
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        state = "Encrypted event retained" if pending and pending.exists() else "No new event was queued"
        print(f"Z Bark completion notification failed. {state}; inspect zbark-run --status and diagnose before an explicit --flush.", file=sys.stderr)
        return exit_code or 75


if __name__ == "__main__": sys.exit(main())
