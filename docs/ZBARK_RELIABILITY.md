# Z Bark reliability and maintenance

## Current task (2026-11-06)

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
- [ ] Add repeatable signed build, verification and maintenance instructions.
- [ ] Run appropriate tests and produce a current validation record.
- [ ] Commit changes and record the submission and deployment boundary.
- [ ] Physical device: registration, foreground/background/locked delivery,
  extension processing, restart and update persistence (device unavailable at
  initial inventory; not accepted from the old report).

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

## Physical acceptance still required

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
