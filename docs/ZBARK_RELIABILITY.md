# Z Bark reliability and maintenance

## Delivery checklist (2026-11-06)

Acceptance follows the current source and deployed image. The 2026-10-27 report is
historical evidence only. Simulator and provider responses do not prove delivery
on a physical iPhone.

- [x] Inventory both repositories, signing assets, HTTPS and device availability.
- [x] Isolate work from existing checkouts and preserve server untracked files.
- [x] Make registration safe to retry without losing a returned device key.
- [x] Preserve pending history when Realm writes fail; cover retry and updates.
- [x] Make notification extension completion safe on timeout and early failure.
- [x] Make server regression tests independent of production APNs credentials.
- [x] Verify APNs configuration, payload and provider failure behavior.
- [x] Add repeatable signed build, verification and maintenance instructions.
- [x] Run appropriate tests and produce a current validation record.
- [x] Commit changes and record the submission and deployment boundary.
- [x] Physical device: current registration, foreground/background history, operator-confirmed
  lock-screen display, extension decryption, app/system restart and in-place update persistence.

Baseline: client `ef22721`, server `67235ff`. Client worktree was clean. Server
had untracked `.DS_Store` and `bark-server-linux-amd64`; neither belongs to this
change. Public `/ping` and `/healthz` passed TLS verification. Existing Ad Hoc
profiles expire 2027-08-31 and do not grant Critical Alerts. Do not revoke or
regenerate signing assets, uninstall the app, or clear production data.

## Local build and verification

Use Xcode 27.0 (27A266a), `Bark.xcworkspace`, and the committed Podfile/SPM
lockfiles. CocoaPods is pinned to 1.16.2. On a new checkout use Ruby 3.4,
`bundle install`, then `bundle exec pod install --deployment`; do not update
Pods merely to make a build pass.

```sh
python3 scripts/zbark.py doctor
python3 scripts/zbark.py test --simulator <dedicated-simulator-UDID>
python3 scripts/zbark.py archive --build-number 2 --device <registered-iPhone-UDID>
python3 scripts/zbark.py verify-ipa <exported.ipa> --device <registered-iPhone-UDID>
python3 scripts/zbark.py smoke --registration-mode existing
```

Each test/archive run gets a new evidence directory. An archive requires clean,
committed source and a positive build number. Increase that number for every
update. `doctor` checks Pods and all four existing Ad Hoc profiles against
installed signing identities; local automatic export reuses Xcode-managed Ad Hoc
profiles, then verifies that their exact UUIDs match the preflight inventory. No
`-allowProvisioningUpdates`, Match, certificate creation or revocation is used.
The upstream TestFlight workflow was removed and the fastlane beta lane refuses
to access upstream signing assets. `fastlane tests` now uses the workspace.

The archive writes source/toolchain metadata, logs, an xcarchive, an IPA and
`validation.json` with SHA-256 and four bundle/profile checks. All bundles must
have the Sanbox Team, matching identifiers, the shared App Group and matching
versions. The exported main app must use production APNs and all bundles must
exclude Critical Alerts and debug entitlements. Profile expiry is recorded.
Renew profiles before expiry only as a separate, explicit signing operation.

`smoke` sends only health and invalid requests that cannot create registrations
or deliver pushes. It requires trusted HTTPS and checks JSON application status.
An edge-generated empty 403 fails this check: it cannot prove the server policy.

## Safe client update

### Repeatable physical-device evidence

Keep the phone connected, unlocked and trusted, and launch the installed app
before taking a snapshot. CoreDevice container reads can stall until that first
launch. A failed or partial snapshot is not a complete backup. The device helper
copies both the main container and App Group into a new directory with mode 0700;
raw preferences, history and the derived push credential remain private. Never
upload `private/`, screenshots with user history, IPA profiles or device logs to a PR.

```sh
python3 scripts/zbark_device.py snapshot --device <UDID> --output build/before-update
# Create a JSON payload containing a unique id, title/body and isArchive="1".
# Do not put a key or device_keys in it. This sends one real test notification:
python3 scripts/zbark_device.py push \
  --credentials build/before-update/private/credentials.json \
  --payload build/test-payload.json --output build/push-result.json
```

The push helper checks the explicit HTTPS origin, rejects batch/credential
overrides and redirects, and records only a safe acceptance result. It never
registers a device or changes the registration policy. Keep test IDs unique and
use a fresh output path for each invocation; evidence is never overwritten.

For database proof, query the copied file with the project's locked Realm
library after running the simulator regression build:

```sh
python3 scripts/zbark_device.py history \
  --realm build/before-update/private/app/Documents/bark.realm \
  --simulator <booted-dedicated-simulator-UDID> --prefix <unique-test-prefix> \
  --output build/history-before.json
# Snapshot again after restart/update, then run history for that new copy.
python3 scripts/zbark_device.py compare-history \
  --before build/history-before.json --after build/history-after.json \
  --output build/preservation.json
python3 -m unittest discover -s scripts -p 'test_*.py'
```

The simulator here only runs a read-only reader of a **copied physical-device
database**. It does not simulate delivery. The reader checks actual Realm records,
hashes all non-test content, and reports plaintext only for the supplied unique
test prefix. Compare requires every original unexpired record to retain its ID
and content; naturally elapsed TTLs are excluded. Separately compare snapshot
server and crypto fingerprints, observe display/extension execution, and
correlate timestamps with the deployed server's successful POST registration.

Backups contain bearer keys and may contain encryption settings. Preserve them
locally with the old IPA; file copies are recovery evidence, not a promise that
CoreDevice supports arbitrary App Group restoration. Do not restore over a newer
live database or downgrade across an incompatible schema.

1. Preserve the previous IPA and validation record. Record the server list/key
   without posting it in an issue, terminal log or screenshot shared publicly.
   Back up available app data before an update; do not uninstall or clear data.
2. Verify the new IPA with the connected device's UDID. Unpack it in a fresh
   directory, then install **over** `cc.sanbox.bark`:
   `xcrun devicectl device install app --device <UDID> <Payload/Bark.app>`.
   Do not use an uninstall/reinstall flow. Bundle ID, Team and App Group must
   remain unchanged. This app uses schema 19; this change does not alter it.
3. Open the app, confirm the existing server and key remain, and check old
   history. Foreground entry refreshes APNs registration without a permission
   prompt. A failed server request preserves the key; successful responses are
   persisted immediately. Registration uses POST and logs omit credentials.
4. Send uniquely identified test notifications using POST JSON (read the key from
   protected input, do not put it in a command-line URL). Correlate server result,
   notification display, extension execution and the exact IDs in saved history.
   Repeat after app restart and after the in-place update.
5. If the device disconnects, stop deployment/retries. Retain logs and mark the
   physical-device cases unverified. A signed IPA or simulator success cannot
   substitute for these steps. A previous IPA is a recovery artifact; do not
   assume a downgrade preserves data without checking its Realm schema.

## Server policy and important behavior

The initial live image was `sha256:25692bf18313d45bc0b718a1f099417d50590a6a6916c9efe8029f1fc4f10c98`.
It did not expose source metadata. Caddy unconditionally blocked `/register`,
including token refresh for existing devices. The server change adds
`BARK_SERVER_REGISTRATION_MODE=existing`: known keys can refresh, while new
pairing and reset-to-new-key remain closed. Proxy forwarding is enabled only
once that policy passes canary verification. See the server repository's
`docs/ZBARK_MAINTENANCE.md` for deployment, backup and rollback.

Reset first saves a successful replacement, then revokes the old link. If
registration fails under closed enrollment, the old key stays usable. If old-link
revocation fails, the new key remains saved but the UI reports failure; investigate
before treating the old URL as revoked. A push key is a secret bearer credential.

Pending history is written atomically to a unique file per delivery. The app
acknowledges only successfully imported or expired valid files. Realm failures
retain files for retry; malformed files remain for diagnosis. Duplicate IDs
update one record in delivery-time order, and an older retry cannot overwrite a
newer record. Archiving signals the app before slow media processors. Extension
completion uses an immutable best-attempt snapshot and consumes its handler once.

## Physical acceptance protocol

Use a connected, unlocked and trusted physical device. Record current client
commit/build, server commit/image, profile environment and each unique request ID.

| Case | Required evidence |
| --- | --- |
| Existing registration | same key; refreshed token accepted by current server |
| Foreground/background/locked | actual notification plus matching history ID |
| Archive opt-out and TTL | visible notification where applicable; expected history omission/expiry |
| Repeated ID | one final record containing the newer payload |
| Encryption | correct-key decrypt plus wrong-key/invalid ciphertext graceful failure |
| Media timeout/offline | notification delivered once; archived history survives |
| Restart and in-place update | earlier IDs and server key preserved |
| Notification disabled | no claim of delivery; UI reflects permission state |

Critical Alerts remain unapproved and disabled. Sound, Focus/DND bypass, expanded
notification UI and Widget placement need separate device evidence before being
claimed. No physical-device acceptance is asserted by this task's automated tests.

Authoritative references: [APNs registration](https://developer.apple.com/documentation/usernotifications/registering-your-app-with-apns),
[extension time budget](https://developer.apple.com/documentation/usernotifications/unnotificationserviceextension),
[APNs failure reasons](https://developer.apple.com/documentation/usernotifications/handling-notification-responses-from-apns).

## Initial validation record (2026-11-06, before continuation)

- Client release source: `bc854b2044e5050485e48981fa08c28e953254f3`;
  version 1.0.0, build 2. Archive, export and all four signature/profile checks
  passed. SHA-256: `7b2675d3be997a2bb829b6bfea67a6dc3721a8ddea33c128a434850248130df1`.
  The exact existing profile UUIDs were reused. Signing identities were unchanged.
- Client simulator regression: 18 invocations (9 XCTest methods and the existing
  9-case parameterized time-range test), zero failures, no xcresult runtime warnings.
  The tested app code is the code in the exported release; later edits are tooling
  and this validation ledger.
- Server runtime source: `fb6a43e039078ed52dded5aadc438be992601af6`, deployed as
  `bark-server:zbark-fb6a43e03907`. The database-copy canary passed existing-key
  replay and closed-enrollment checks. Production retained its registration count,
  data/credential mounts and hardening; other service containers were not restarted.
- Server offline HTTP/2 APNs tests, Go race detection/vet, bbolt restart persistence
  and registration-window tests passed. A clean Go binary rebuild matched SHA-256
  byte for byte (same toolchain and source).
- Public HTTPS smoke passed after deployment. A single intentionally invalid-token
  production APNs request returned `400 BadDeviceToken`; no real device token was
  used. This confirms the observed negative response, not physical delivery.
- Physical iPhone unavailable: no new client installation, foreground/background/
  locked delivery, encrypted/rich notification processing, or in-place update and
  history acceptance was performed. The earlier 2026-10-27 results remain historical.
- Both repositories use isolated `fix/zbark-reliability` worktrees. Changes are
  locally committed. Only the read-only Finb upstream origin is configured, and no
  personal fork exists at `prur/Bark` or `prur/bark-server`; no remote PR is claimed.

The current handoff directory is `build/zbark-reliability-20261106/` in the original
client checkout. It contains the new IPA and combined validation report, with
paths to xcresult, signing and deployment evidence in the isolated worktrees.

## Encryption setup and sender maintenance

Encryption is opt-in; a missing `cryptoSettingFields` entry means no key is
configured. Never infer a key from a device token or push key. The new
**Generate an AES-256-GCM key** action fills an empty form using 32 random bytes
from Apple's secure random generator. Save with Done. The key field is masked;
generation refuses to overwrite a nonempty key. Existing CBC/ECB/GCM text keys
remain supported, with lengths checked in UTF-8 bytes.

Generated keys use `keyEncoding=base64` in the shared client configuration. Each
message must supply a fresh 12-byte GCM nonce as base64 in `iv`, with
`iv_encoding=base64`. The GCM ciphertext is base64 of ciphertext followed by the
16-byte authentication tag. Raw legacy IV strings remain supported when no
encoding is supplied. Neither key nor plaintext belongs on the server.

After saving the device configuration, take a new protected snapshot. Its
`private/credentials.json` contains the matching crypto settings for local senders:

```sh
python3 scripts/zbark_device.py push --encrypt \
  --credentials build/encrypted-baseline/private/credentials.json \
  --payload build/test-payload.json --output build/encrypted-push.json
node --test scripts/test_zbark_encrypt.cjs
```

The Python helper passes private configuration to the Node encryption helper on
stdin; keys never appear in argv or evidence output. Encryption is opt-in per
sender: an existing plain HTTP/JSON sender does not become encrypted merely by
configuring the client. Existing plaintext history is preserved. Copy Example
now produces a reusable Node script that reads keys from private environment
configuration, generates a nonce on every run, sends POST JSON and refuses
redirects. It no longer embeds a reusable fixed nonce or secrets in the script.

Back up the private configuration outside the repository with access restricted
to the owner. Loss of the encryption key makes encrypted payloads unrecoverable.
After adopting encoded keys/nonces, rollback only to a build supporting this
format (build 3 or later); earlier builds still read history but cannot decrypt
this new wire format. Do not change App Group or overwrite an existing key to
troubleshoot a sender mismatch.

References: [Node crypto](https://nodejs.org/api/crypto.html#cryptocreatecipherivalgorithm-key-iv-options),
[Apple secure random](https://developer.apple.com/documentation/security/secrandomcopybytes(_:_:_:)).

## Remote maintenance boundary

The personal client fork is [prur/Bark](https://github.com/prur/Bark). The `fork`
remote is separate from the read-only Finb `origin`. The maintained client base
is `zbark`, initially the physically installed `ef22721` iOS baseline; reliability
changes are delivered by PR from `fix/zbark-reliability`. Upstream master was
rechecked and is three commits ahead, including a broad watchOS integration that
changes targets, App Groups and dependency locks. Importing it now would expand
the four-bundle signing and device-acceptance boundary; it is deliberately deferred
to a separately validated update. Do not force-push or silently rebase the tested
release onto that branch. The inherited Cloudflare documentation publishing job
is guarded to Finb's repository; this fork only needs credential-free regression.

## Accepted build 4 checkpoint (2026-11-06 continuation)

The phone now runs **Z Bark 1.0.0 build 4**, exported from clean source
`85a02d69ef8c3389cb810509bd4d08c4b13658fc`. IPA SHA-256:
`4f3f5dea3ae2257b9b80ceb33a0467b00e7324c08ccacd4bf0ddec210536617b`.
All four original Ad Hoc profile UUIDs were reused, with production APNs and
no Critical Alerts or debug entitlement. Signing identities were unchanged.
This is the build 4 checkpoint. The reviewed native follow-up below is tested
and exported separately; it does not change Bundle IDs, dependencies or schema.

The physical iPhone 16 Pro (iOS 27.0.1) completed build 1 → 2 → 3 → 4 in-place
updates. All 29 original Realm records retained their complete content. A full
phone reboot retained all 39 then-current records, the existing server/key and
the new encryption configuration. Build 3 → 4 retained all 40 pre-update records.
After new background encrypted sends and a final app restart, Realm contained
42 live records, including all expected test IDs and no wrong-key plaintext.
The server observed successful POST registration against deployed runtime
`6e19b8a6acb486645960226a1dfcdfd06effd8f5`; all 3 server registrations remained.

Observed cases include foreground/background history, one latest record for a
repeated ID, archive opt-out, TTL=0 omission, Markdown, missing-image failure,
correct-key decryption, wrong-key omission followed by successful recovery, and
new encrypted sends after reboot/update. The operator explicitly confirmed the
unique lock-screen notification on build 2. This is a physical observation,
separate from API/provider acceptance. Correct decrypted content was observed
in the device UI and queried from the copied physical Realm. The simulator was
only the read-only database reader and the automated test host.

Local xcresult: 15 reported test methods/suites including parameterized cases,
zero failures. Sender/device tooling: 7 Python tests; Node GCM: 1 test. Native
release source passed push and PR CI. Server race/vet, 7 Python regressions,
closed-enrollment/empty-token canary and fresh nonroot database initialization
passed. Current live source/image/hash and rollback location are in the server
maintenance document.

The retained handoff is `build/zbark-reliability-20261106-device/` in the original
client checkout: final IPA, combined validation JSON, xcresult, signing and
request records, protected raw snapshots and the versioned sender manifest.
The earlier handoff/report was preserved. Remote PRs:
[client](https://github.com/prur/Bark/pull/1), [server](https://github.com/prur/bark-server/pull/1).

Remaining boundaries: no live public enrollment window was opened; its failure
ordering/emergency-disable behavior is covered by local regressions. The
wrong-key fallback banner itself was not independently observed, although GCM
authentication rejection, physical history omission and subsequent successful
physical delivery were verified. Focus/DND behavior, sound, Widget placement,
positive-TTL expiry on device and the new encrypted format on a locked display
were not separately accepted. Critical Alerts remain unapproved and disabled.

### Ready-to-use local encrypted sender

The owner-only configuration is `~/.config/zbark/sender.json`. It contains the
existing push key plus the new encryption key; it must never enter git, a PR,
screenshots or process arguments. The installed `~/.local/bin/zbark-send` points
to a versioned standalone bundle under `~/.local/share/zbark/`; its manifest
records each source hash. It does not depend on keeping this task worktree.

```sh
~/.local/bin/zbark-send '测试推送' --title 'Z Bark'
# For sensitive content, read from a protected file instead of shell history:
~/.local/bin/zbark-send --title 'Z Bark' < message.txt
# Equivalent directly from an updated source checkout:
python3 scripts/zbark_send.py '测试推送' --title 'Z Bark'
```

Each invocation generates a fresh id and 96-bit nonce and requires encryption;
missing crypto settings fail without plaintext fallback. `--id` allows explicit
message updates/correlation and `--group` selects a history group. Updating the
sender changes only the three scripts (`zbark_send.py`, `zbark_device.py`,
`zbark_encrypt.cjs`), copied to a new versioned bundle with verified hashes before
changing the launcher. Preserve `~/.config/zbark/sender.json` throughout.
Other existing senders must explicitly adopt encryption; enabling it on the
client does not transform their plaintext requests.

## Review follow-up after build 4

The reusable encrypted example now appends `push` to the configured server
path, including path prefixes and trailing slashes. Persisting a replacement
server key emits a list update immediately, before old-key revocation finishes;
a revocation error still reports failure while the visible key matches storage.
Pending history resolves the newest version per ID before checking TTL. An
expired replacement removes an older saved version rather than reviving an
older queued body. If acknowledging an older file fails, its expired replacement
is retained until that older file is removed, so the next retry stays safe.
Focused regression covers prefixed URLs, persisted-key UI events, expired
replacements and acknowledgement failure, plus stale expiry against newer data.
