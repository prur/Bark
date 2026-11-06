#!/usr/bin/env python3
"""Install a hash-verified standalone sender bundle without reading or changing keys."""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parent
FILES = ("zbark_send.py", "zbark_run.py", "zbark_device.py", "zbark_encrypt.cjs")


def main():
    home = Path.home()
    base = home / ".local/share/zbark"
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}
    version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:12]
    bundle = base / ("sender-" + version)
    scripts = bundle / "scripts"
    bin_dir = home / ".local/bin"
    for name in ["zbark-send", "zbark-run"]:
        launcher = bin_dir / name
        if launcher.is_symlink():
            if base.resolve() not in launcher.resolve().parents:
                raise ValueError("Refusing to replace an unrelated launcher")
        elif launcher.exists():
            raise ValueError("Refusing to replace an existing non-symlink launcher")
    if not bundle.exists():
        scripts.mkdir(parents=True, mode=0o700)
        bundle.chmod(0o700)
        for name in FILES:
            destination = scripts / name
            with destination.open("xb") as stream:
                stream.write((ROOT / name).read_bytes())
            destination.chmod(0o755)
    for name, expected in hashes.items():
        if hashlib.sha256((scripts / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Installed bundle does not match source; launcher was not changed")
    manifest = {"bundle": str(bundle), "files": hashes, "config": str(home / ".config/zbark/sender.json"),
                "credentials_copied": False}
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, source in [("zbark-send", "zbark_send.py"), ("zbark-run", "zbark_run.py")]:
        temporary = bin_dir / ("." + name + "-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to(scripts / source)
            os.replace(temporary, bin_dir / name)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    try: main()
    except (OSError, ValueError):
        print("Sender installation failed; existing keys were not changed.", file=sys.stderr)
        sys.exit(1)
