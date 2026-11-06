#!/usr/bin/env python3
"""Send an authenticated encrypted push using an owner-only local configuration."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
import uuid

from zbark_device import ROOT, command, protected_json, send


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("body", nargs="?", help="Message body; omit to read it from stdin")
    parser.add_argument("--title", default="Z Bark")
    parser.add_argument("--group")
    parser.add_argument("--id", help="Optional caller-managed update/correlation id")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/zbark/sender.json")
    args = parser.parse_args()
    try:
        body = args.body
        if body is None:
            if sys.stdin.isatty(): parser.error("provide a body or pipe it on stdin")
            body = sys.stdin.read()
        if not body: parser.error("message body must not be empty")
        credentials = protected_json(args.config)
        if "crypto" not in credentials: raise ValueError("No client encryption configuration; refusing plaintext fallback")
        payload = {"id": args.id or "zbark-" + str(uuid.uuid4()), "title": args.title, "body": body, "isArchive": "1"}
        if args.group: payload["group"] = args.group
        encrypted = json.loads(command(["node", str(ROOT / "scripts/zbark_encrypt.cjs")],
                                       json.dumps({"payload": payload, "fields": credentials["crypto"]}).encode()))
        result = send(credentials, encrypted, "https://bark.sanbox.cc")
        result["encrypted"] = True
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"Z Bark send failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__": sys.exit(main())
