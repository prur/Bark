#!/usr/bin/env python3
"""Local Z Bark builds; never creates/revokes certificates or clears app data."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TEAM = "46PQ4DZSV6"
APP = "cc.sanbox.bark"
GROUP = "group.cc.sanbox.bark"
BUNDLES = {APP, APP + ".NotificationServiceExtension", APP + ".NotificationContent", APP + ".Widget"}
CRITICAL = "com.apple.developer.usernotifications.critical-alerts"


def run(args, log=None):
    if log:
        print(f"Running {args[0]} {args[1]} (log: {log})", flush=True)
        with Path(log).open("w") as stream:
            subprocess.run(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=True)
        return b""
    return subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def source():
    return {"commit": run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip(),
            "dirty": bool(run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"]).strip())}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def check_dependencies():
    lock = ROOT / "Podfile.lock"
    manifest = ROOT / "Pods/Manifest.lock"
    if not manifest.exists() or lock.read_bytes() != manifest.read_bytes():
        raise ValueError("Pods do not match Podfile.lock; use CocoaPods 1.16.2: bundle exec pod install --deployment")


def signing_profiles():
    identities = run(["security", "find-identity", "-v", "-p", "codesigning"]).decode()
    valid_certificates = set(re.findall(r"\b[0-9A-F]{40}\b", identities))
    profiles = {}
    directories = [Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles",
                   Path.home() / "Library/MobileDevice/Provisioning Profiles"]
    for directory in directories:
        for file in directory.glob("*.mobileprovision"):
            try:
                profile = plistlib.loads(run(["security", "cms", "-D", "-i", str(file)]))
                entitlement = profile["Entitlements"]
                bundle = entitlement.get("application-identifier", "").removeprefix(TEAM + ".")
                certificates = {hashlib.sha1(c).hexdigest().upper() for c in profile["DeveloperCertificates"]}
                candidates = valid_certificates & certificates
                if (bundle not in BUNDLES or profile.get("TeamIdentifier") != [TEAM]
                        or profile["ExpirationDate"] <= dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                        or not profile.get("ProvisionedDevices") or entitlement.get("get-task-allow", True)
                        or entitlement.get(CRITICAL) or GROUP not in entitlement.get("com.apple.security.application-groups", [])
                        or (bundle == APP and entitlement.get("aps-environment") != "production") or not candidates):
                    continue
                item = (profile, candidates)
                if bundle not in profiles or profile["ExpirationDate"] > profiles[bundle][0]["ExpirationDate"]:
                    profiles[bundle] = item
            except (subprocess.CalledProcessError, KeyError, ValueError, plistlib.InvalidFileException):
                continue
    if set(profiles) != BUNDLES:
        raise ValueError("Missing valid existing Ad Hoc profiles/signing keys for: " + ", ".join(sorted(BUNDLES - profiles.keys())))
    common = set.intersection(*(certificates for _, certificates in profiles.values()))
    if not common:
        raise ValueError("Four profiles do not share an installed signing identity")
    return profiles, sorted(common)[0]


def doctor():
    check_dependencies()
    profiles, _ = signing_profiles()
    return {"source": source(), "xcode": run(["xcodebuild", "-version"]).decode().strip(),
            "pods_match_lockfile": True,
            "profiles": {bundle: {"uuid": p["UUID"], "expires": p["ExpirationDate"].isoformat(),
                                  "registered_devices": len(p["ProvisionedDevices"])} for bundle, (p, _) in profiles.items()}}


def verify_app(app, expected_device=None):
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    bundles = [app, *sorted((app / "PlugIns").glob("*.appex"))]
    result = []
    for bundle in bundles:
        info = plistlib.loads((bundle / "Info.plist").read_bytes())
        identifier = info["CFBundleIdentifier"]
        entitlement = plistlib.loads(run(["codesign", "-d", "--entitlements", ":-", str(bundle)]))
        profile = plistlib.loads(run(["security", "cms", "-D", "-i", str(bundle / "embedded.mobileprovision")]))
        granted = profile["Entitlements"]
        for data in [entitlement, granted]:
            if data.get("com.apple.developer.team-identifier") != TEAM or data.get("application-identifier") != TEAM + "." + identifier:
                raise ValueError(f"Signing identity mismatch: {identifier}")
            if GROUP not in data.get("com.apple.security.application-groups", []) or data.get(CRITICAL) or data.get("get-task-allow", True):
                raise ValueError(f"Invalid App Group, Critical Alerts or debug entitlement: {identifier}")
            if identifier == APP and data.get("aps-environment") != "production":
                raise ValueError("Export must use production APNs")
        if profile["ExpirationDate"] <= dt.datetime.now(dt.timezone.utc).replace(tzinfo=None):
            raise ValueError(f"Expired profile: {identifier}")
        if expected_device and expected_device not in profile.get("ProvisionedDevices", []):
            raise ValueError(f"Device absent from profile: {identifier}")
        result.append({"bundle": identifier, "version": info["CFBundleShortVersionString"],
                       "build": info["CFBundleVersion"], "profile_uuid": profile["UUID"],
                       "profile_expires": profile["ExpirationDate"].isoformat(), "signature_verified": True,
                       "aps_environment": entitlement.get("aps-environment"), "critical_alerts": False})
    if {item["bundle"] for item in result} != BUNDLES or len(result) != 4:
        raise ValueError("Expected the main app and exactly three Z Bark extensions")
    if len({(item["version"], item["build"]) for item in result}) != 1:
        raise ValueError("App and extensions have inconsistent version/build numbers")
    if plistlib.loads((app / "Info.plist").read_bytes()).get("CFBundleDisplayName") != "Z Bark":
        raise ValueError("Unexpected application display name")
    return result


def unpack(ipa, destination):
    with zipfile.ZipFile(ipa) as archive:
        for item in archive.infolist():
            path = (destination / item.filename).resolve()
            if not path.is_relative_to(destination.resolve()):
                raise ValueError("Unsafe IPA archive path")
        archive.extractall(destination)
    apps = list((destination / "Payload").glob("*.app"))
    if len(apps) != 1:
        raise ValueError("Expected exactly one app in the IPA")
    return apps[0]


def verify_ipa(ipa, device=None):
    with tempfile.TemporaryDirectory(prefix="zbark-verify-") as directory:
        bundles = verify_app(unpack(ipa, Path(directory)), device)
    return {"ipa": str(ipa.resolve()), "sha256": hashlib.sha256(ipa.read_bytes()).hexdigest(), "bundles": bundles,
            "verified_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "physical_device_acceptance": "not performed by this command"}


def output_directory(value, kind):
    path = Path(value).resolve() if value else ROOT / "build/zbark" / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + kind)
    path.mkdir(parents=True, exist_ok=False)
    return path


def archive(args):
    baseline = doctor()
    if baseline["source"]["dirty"]:
        raise ValueError("Commit source changes before a release archive")
    profiles, certificate = signing_profiles()
    output = output_directory(args.output, "archive")
    write_json(output / "baseline.json", baseline)
    archive_path = output / "ZBark.xcarchive"
    run(["xcodebuild", "archive", "-workspace", "Bark.xcworkspace", "-scheme", "Bark", "-configuration", "Release",
         "-destination", "generic/platform=iOS", "-archivePath", str(archive_path), "-derivedDataPath", str(ROOT / "build/zbark-derived"),
         f"CURRENT_PROJECT_VERSION={args.build_number}"], output / "archive.log")
    options = {"method": "release-testing", "signingStyle": "manual", "teamID": TEAM, "signingCertificate": certificate,
               "provisioningProfiles": {bundle: profile["UUID"] for bundle, (profile, _) in profiles.items()},
               "manageAppVersionAndBuildNumber": False}
    options_path = output / "ExportOptions.plist"
    options_path.write_bytes(plistlib.dumps(options))
    run(["xcodebuild", "-exportArchive", "-archivePath", str(archive_path), "-exportOptionsPlist", str(options_path),
         "-exportPath", str(output / "Export")], output / "export.log")
    ipas = list((output / "Export").glob("*.ipa"))
    if len(ipas) != 1:
        raise ValueError("Export did not produce exactly one IPA")
    report = verify_ipa(ipas[0], args.device)
    report["source"] = baseline["source"]
    write_json(output / "validation.json", report)
    print(json.dumps(report, indent=2))


def test(args):
    check_dependencies()
    devices = json.loads(run(["xcrun", "simctl", "list", "devices", "available", "--json"]))
    if not any(d["udid"] == args.simulator for group in devices["devices"].values() for d in group):
        raise ValueError("The specified simulator is unavailable")
    output = output_directory(args.output, "tests")
    write_json(output / "baseline.json", {"source": source(), "simulator": args.simulator})
    run(["xcodebuild", "test", "-workspace", "Bark.xcworkspace", "-scheme", "Bark", "-destination", f"platform=iOS Simulator,id={args.simulator}",
         "-derivedDataPath", str(ROOT / "build/zbark-derived"), "-resultBundlePath", str(output / "Tests.xcresult"), "CODE_SIGNING_ALLOWED=NO"], output / "tests.log")
    print(f"Simulator tests passed: {output / 'Tests.xcresult'}")


def smoke(args):
    base = args.server.rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("Smoke checks require HTTPS with normal certificate verification")
    registration_status = 400 if args.registration_mode == "open" else 403
    checks = [("GET", "/ping", None, 200), ("GET", "/healthz", None, 200),
              ("POST", "/register", b"{}", registration_status), ("POST", "/push", b'{"body":"validation-without-device-key"}', 400),
              ("POST", "/push", b"{", 400)]
    results = []
    for method, path, data, expected in checks:
        request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.code != expected:
                raise ValueError(f"{method} {path}: expected {expected}, received {response.code}")
            if path != "/healthz":
                payload = json.loads(body)
                if payload.get("code") != expected or (path == "/ping" and payload.get("message") != "pong"):
                    raise ValueError(f"{method} {path}: invalid JSON result")
            results.append({"method": method, "path": path, "http_status": response.code})
    report = {"server": base, "checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": results,
              "boundary": "HTTPS and non-mutating rejection checks only; no APNs delivery"}
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    tests = commands.add_parser("test")
    tests.add_argument("--simulator", required=True, help="Use a dedicated simulator UDID")
    tests.add_argument("--output")
    release = commands.add_parser("archive")
    release.add_argument("--build-number", required=True, type=lambda s: s if re.fullmatch(r"[1-9][0-9]*", s) else parser.error("build number must be a positive integer"))
    release.add_argument("--output")
    release.add_argument("--device", help="Also check this device UDID is present in all exported profiles")
    verify = commands.add_parser("verify-ipa")
    verify.add_argument("ipa", type=Path)
    verify.add_argument("--device")
    verify.add_argument("--output")
    health = commands.add_parser("smoke")
    health.add_argument("--server", default="https://bark.sanbox.cc")
    health.add_argument("--output")
    health.add_argument("--registration-mode", choices=["open", "existing", "disabled"], default="existing")
    args = parser.parse_args()  # Parse everything before any build/signing side effect.
    try:
        if args.command == "doctor":
            print(json.dumps(doctor(), indent=2))
        elif args.command == "archive": archive(args)
        elif args.command == "test": test(args)
        elif args.command == "smoke": smoke(args)
        elif args.command == "verify-ipa":
            report = verify_ipa(args.ipa, args.device)
            if args.output: write_json(args.output, report)
            print(json.dumps(report, indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f"Z Bark: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
