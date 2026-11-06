#!/usr/bin/env python3
"""Protected device snapshots and explicit single-device pushes. Never enrolls or clears data."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
APP = "cc.sanbox.bark"
GROUP = "group.cc.sanbox.bark"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path = Path(path)
    # Evidence must not silently overwrite an earlier run.
    with path.open("x") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def protected_json(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("Credentials must be a private file (chmod 600)")
    return json.loads(path.read_text())


def servers(path):
    prefs = plistlib.loads(Path(path).read_bytes())
    value = prefs.get("me.fin.bark.servers")
    value = json.loads(value) if isinstance(value, (str, bytes)) else value
    if not isinstance(value, list) or not value:
        raise ValueError("No saved servers in the device preferences")
    return value


def fingerprints(items):
    return sorted(sha(json.dumps(s, sort_keys=True).encode()) for s in items)


def command(args, data=None):
    result = subprocess.run(args, cwd=ROOT, input=data, capture_output=True, timeout=90)
    if result.returncode:
        # Device/service errors can contain user paths or credential-bearing URLs.
        raise ValueError(f"{args[0]} failed (exit {result.returncode}); check device connection and unlock/launch state")
    return result.stdout


def snapshot(args):
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    private = output / "private"
    private.mkdir(mode=0o700)
    base = ["xcrun", "devicectl", "device"]
    for domain, identifier, source, name in [
        ("appDataContainer", APP, ".", "app"),
        ("appGroupDataContainer", GROUP, ".", "group"),
    ]:
        command(base + ["copy", "from", "--device", args.device, "--domain-type", domain,
                        "--domain-identifier", identifier, "--source", source,
                        "--destination", str(private / name), "--timeout", "30"])
    items = servers(private / "app/Library/Preferences" / (APP + ".plist"))
    matching = [s for s in items if s["address"].rstrip("/") == args.server.rstrip("/") and s.get("key")]
    if len(matching) != 1:
        raise ValueError("Expected exactly one existing registration for the requested server")
    command(base + ["info", "apps", "--device", args.device, "--filter", f'bundleIdentifier == "{APP}"',
                    "--json-output", str(private / "installed-app.json"), "--timeout", "15"])
    apps = json.loads((private / "installed-app.json").read_text())["result"]["apps"]
    if len(apps) != 1:
        raise ValueError("Z Bark installation missing")
    realm = private / "app/Documents/bark.realm"
    group_prefs = private / "group/Library/Preferences" / (GROUP + ".plist")
    group = plistlib.loads(group_prefs.read_bytes())
    credentials = {"base_url": args.server.rstrip("/"), "device_key": matching[0]["key"]}
    if group.get("cryptoSettingFields"):
        credentials["crypto"] = json.loads(group["cryptoSettingFields"])
    save(private / "credentials.json", credentials)
    report = {"captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "version": apps[0]["version"], "build": apps[0]["bundleVersion"],
              "server_fingerprints": fingerprints(items), "server_count": len(items),
              "crypto_fingerprint": sha(group.get("cryptoSettingFields", b"")),
              "crypto_configured": bool(group.get("cryptoSettingFields")),
              "realm_sha256": sha(realm.read_bytes()),
              "boundary": "read-only copy; run history to validate database consistency; not delivery proof"}
    for path in private.rglob("*"):
        if path.is_file(): path.chmod(0o600)
    save(output / "snapshot.json", report)
    print(json.dumps(report, indent=2))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(credentials, payload, expected_server):
    base = credentials["base_url"].rstrip("/")
    url = urllib.parse.urlsplit(base)
    if base != expected_server.rstrip("/") or url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
        raise ValueError("Credentials must match the explicit HTTPS server origin")
    if any(k.lower() in {"device_key", "device_keys", "key", "devicetoken", "device_token"} for k in payload):
        raise ValueError("Payload must not override the single-device credential")
    if not credentials.get("device_key") or not isinstance(payload.get("id"), str) or not payload["id"]:
        raise ValueError("An existing key and a unique test id are required")
    request = urllib.request.Request(base + "/push", data=json.dumps({**payload, "device_key": credentials["device_key"]}, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            result = json.load(response)
            if response.status != 200 or result.get("code") != 200:
                raise ValueError("Push was not accepted")
    except urllib.error.HTTPError as error:
        # Never print response bodies or a credential-bearing request.
        status = error.code
        error.close()
        raise ValueError(f"Push HTTP status {status}; do not retry provider rejection blindly") from None
    return {"id": payload["id"], "http_status": 200, "application_code": 200,
            "sent_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "boundary": "server/provider acceptance only; correlate with device display, extension and Realm"}


def history(args):
    products = ROOT / "build/zbark-derived/Build/Products/Debug-iphonesimulator"
    if not (products / "Realm/libRealm.a").is_file():
        raise ValueError("Run zbark.py test first to build the locked simulator Realm library")
    with tempfile.TemporaryDirectory(prefix="zbark-history-") as directory:
        directory = Path(directory)
        copied = directory / "copied.realm"
        shutil.copyfile(args.realm, copied)
        binary = directory / "inspect_history"
        # Realm's dynamic read-only API inspects the real file schema; no app data
        # is installed in the simulator or replaced on the physical device.
        command(["xcrun", "--sdk", "iphonesimulator", "clang", "-arch", "arm64", "-mios-simulator-version-min=15.0",
                 "-fobjc-arc", "-fmodules", "-I", str(ROOT / "Pods/Headers/Public"), "-I", str(ROOT / "Pods/Headers/Private/Realm"),
                 "-I", str(products / "XCFrameworkIntermediates/Realm/Headers"), "-L", str(products / "Realm"),
                 "-L", str(products / "XCFrameworkIntermediates/Realm"), "-ObjC", "-lRealm", "-lrealm-monorepo",
                 "-lc++", "-lcompression", "-lz", "-framework", "UIKit", "-framework", "Security", "-framework", "Foundation",
                 str(ROOT / "scripts/inspect_history.m"), "-o", str(binary)])
        report = json.loads(command(["xcrun", "simctl", "spawn", args.simulator, str(binary), str(copied), args.prefix]))
    save(args.output, report)
    print(json.dumps({"count": report["count"], "schema": report["schema"], "test_ids": [m["id"] for m in report["test_records"]]}))


def compare(args):
    before, after = (json.loads(p.read_text()) for p in [args.before, args.after])
    # Ignore only records whose original TTL has actually elapsed.
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    required = {k: v for k, v in before["rows"].items() if v["expires"] is None or v["expires"] > now}
    changed = [k for k, v in required.items() if after["rows"].get(k) != v]
    report = {"required_records": len(required), "unchanged_records": len(required) - len(changed), "passed": not changed}
    save(args.output, report)
    print(json.dumps(report))
    if changed: raise ValueError("Unexpired historical records changed or disappeared; inspect protected evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="Copy app/group data after manually launching the existing app")
    snap.add_argument("--device", required=True)
    snap.add_argument("--server", default="https://bark.sanbox.cc")
    snap.add_argument("--output", type=Path, required=True)
    push = commands.add_parser("push", help="Send exactly one explicit test payload using a protected existing key")
    push.add_argument("--credentials", type=Path, required=True)
    push.add_argument("--server", default="https://bark.sanbox.cc")
    push.add_argument("--payload", type=Path, required=True)
    push.add_argument("--output", type=Path, required=True)
    push.add_argument("--encrypt", action="store_true", help="Encrypt with the saved client crypto configuration; requires Node.js")
    hist = commands.add_parser("history", help="Query a copied physical-device Realm; simulator is only a reader")
    hist.add_argument("--realm", type=Path, required=True)
    hist.add_argument("--simulator", required=True)
    hist.add_argument("--prefix", required=True, help="Unique test prefix; all other text is hashed")
    hist.add_argument("--output", type=Path, required=True)
    comp = commands.add_parser("compare-history")
    comp.add_argument("--before", type=Path, required=True)
    comp.add_argument("--after", type=Path, required=True)
    comp.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if hasattr(args, "output") and args.output.exists(): raise ValueError("Output already exists; choose a fresh evidence path")
        if args.command == "snapshot": snapshot(args)
        elif args.command == "history": history(args)
        elif args.command == "compare-history": compare(args)
        elif args.command == "push":
            credentials = protected_json(args.credentials)
            payload = json.loads(args.payload.read_text())
            if args.encrypt:
                if "crypto" not in credentials: raise ValueError("No saved encryption configuration")
                payload = json.loads(command(["node", str(ROOT / "scripts/zbark_encrypt.cjs")],
                                             json.dumps({"payload": payload, "fields": credentials["crypto"]}).encode()))
            result = send(credentials, payload, args.server)
            result["encrypted"] = args.encrypt
            save(args.output, result)
            print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"Device validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
