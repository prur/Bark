#!/usr/bin/env python3
"""Install a hash-verified standalone sender bundle without reading or changing keys."""
import argparse
import hashlib
import fcntl
import json
import os
import plistlib
import shutil
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
FILES = ("zbark_send.py", "zbark_run.py", "zbark_device.py", "zbark_encrypt.cjs", "zbark_watch.py")


def install(home):
    base = home / ".local/share/zbark"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (base / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return install_locked(home, base)


def verify(scripts, hashes):
    for name, expected in hashes.items():
        if hashlib.sha256((scripts / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Installed bundle does not match source; launcher was not changed")


def install_locked(home, base):
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}
    version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:12]
    bundle = base / ("sender-" + version)
    scripts = bundle / "scripts"
    bin_dir = home / ".local/bin"
    for name in ["zbark-send", "zbark-run", "zbark-backup-watch"]:
        launcher = bin_dir / name
        if launcher.is_symlink():
            if base.resolve() not in launcher.resolve().parents:
                raise ValueError("Refusing to replace an unrelated launcher")
        elif launcher.exists():
            raise ValueError("Refusing to replace an existing non-symlink launcher")
    if not bundle.exists():
        with tempfile.TemporaryDirectory(prefix=".install-", dir=base) as directory:
            staged = Path(directory) / bundle.name
            copied = staged / "scripts"
            copied.mkdir(parents=True, mode=0o700)
            staged.chmod(0o700)
            for name in FILES:
                destination = copied / name
                with destination.open("xb") as stream:
                    stream.write((ROOT / name).read_bytes())
                destination.chmod(0o755)
            verify(copied, hashes)
            staged.rename(bundle)
    verify(scripts, hashes)
    manifest = {"bundle": str(bundle), "files": hashes, "config": str(home / ".config/zbark/sender.json"),
                "credentials_copied": False}
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, source in [("zbark-send", "zbark_send.py"), ("zbark-run", "zbark_run.py"),
                         ("zbark-backup-watch", "zbark_watch.py")]:
        temporary = bin_dir / ("." + name + "-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to(scripts / source)
            os.replace(temporary, bin_dir / name)
        finally:
            temporary.unlink(missing_ok=True)
    return manifest


def install_schedule(home, manifest):
    # Only writes the definition. Activation/reload is an explicit launchctl operation.
    node = shutil.which("node")
    if not node or sys.version_info < (3, 11):
        raise ValueError("Backup watch requires Node and Python 3.11+")
    label = "cc.sanbox.zbark.backup-watch"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / (label + ".plist")
    logs = home / ".local/state/zbark/backup-watch"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    if logs.is_symlink() or logs.stat().st_mode & 0o077:
        raise ValueError("Watch state must be an owner-only directory")
    value = {"Label": label, "ProgramArguments": [sys.executable, str(Path(manifest["bundle"]) / "scripts/zbark_watch.py")],
             "EnvironmentVariables": {"PATH": str(Path(node).parent) + ":/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                      "PYTHONDONTWRITEBYTECODE": "1"},
             "StartCalendarInterval": {"Minute": 45}, "RunAtLoad": True,
             "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null", "ProcessType": "Background"}
    data = plistlib.dumps(value)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or plistlib.loads(path.read_bytes()).get("Label") != label:
            raise ValueError("Refusing to replace an unrelated launch agent")
        if path.read_bytes() != data:
            saved = path.with_name(path.name + ".previous-" + uuid.uuid4().hex)
            with saved.open("xb") as output: output.write(path.read_bytes())
            saved.chmod(0o600)
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output: output.write(data)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"plist": str(path), "label": label, "activated": False,
            "schedule": "hourly at minute 45 and on load; catches up on wake", "bundle": manifest["bundle"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-watch", action="store_true", help="Also write the hourly backup-watch launch agent; activate with launchctl")
    args = parser.parse_args()
    manifest = install(Path.home())
    if args.backup_watch: manifest["backup_watch"] = install_schedule(Path.home(), manifest)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    try: main()
    except (OSError, ValueError):
        print("Sender installation failed; existing keys were not changed.", file=sys.stderr)
        sys.exit(1)
