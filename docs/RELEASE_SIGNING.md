# Release signing and verification

The default community build is **ad-hoc signed and not notarized by Apple**.
Adding notarization support does not turn an ad-hoc release into a notarized
release. `Live-Translation-Installer-arm64.verification.json` records the result
for each specific build, together with its artifact hashes.

## What the build verifies

`tools/build_installer.py` produces:

- `Live-Translation-Installer-arm64.dmg`
- `Live-Translation-Installer-arm64.zip`
- `Live-Translation-IINA.iinaplgz`
- `Live-Translation-Installer-arm64.verification.json`
- `SHA256SUMS`

The standalone `.iinaplgz` follows [IINA's ZIP package format](https://docs.iina.io/pages/creating-plugins).
It contains source plugin files and the license, with no embedded local token,
Python environment, service, cached subtitles, models, or signing material.
Use the full installer first when the background service and models are missing.
Avoid installing both a development link and a packaged copy of the same plugin.

With notarization enabled, the builder:

1. signs the AppKit application with a Developer ID Application identity,
   Hardened Runtime, and a secure timestamp;
2. checks the observed certificate authority, team, runtime flag, and timestamp;
3. submits the ZIP using `notarytool` and requires a valid submission ID and
   explicit JSON `status: Accepted`, then fetches and reviews the notary log;
4. staples and validates the app ticket, verifies its signature, and assesses it
   with Gatekeeper and `syspolicy_check` when that tool is available;
5. rebuilds the ZIP around the stapled app, then signs, notarizes, staples,
   validates, and assesses the DMG;
6. verifies the disk image and publishes the archives only after all required
   checks pass.

The public verification report includes submission IDs, status, warning counts,
notary-log hashes, and artifact hashes. It contains no keychain profile, private
key, certificate password, account credential, or machine-specific file path.
Review any reported warnings using the corresponding `notarytool log` before
shipping. Gatekeeper assessment on a build machine complements a fresh-machine
launch test; it does not replace it.

## Local notarized build

The maintainer needs a usable **Developer ID Application certificate and its
private key**, plus authorized Apple notarization credentials. A Developer ID
Installer certificate is for signed `.pkg` installers; this project distributes
an `.app` in ZIP and DMG files.

With an already configured notarytool keychain profile:

```sh
python3 tools/build_installer.py \
  --version 1.4.0 \
  --sign-identity "Developer ID Application: Your Name (TEAMID)" \
  --notary-profile release-notary
```

Use `--keychain /path/to/temporary.keychain-db` when both the identity and notary
profile are stored in a dedicated keychain. Equivalent environment settings are
`MACOS_SIGNING_IDENTITY`, `MACOS_NOTARY_PROFILE`, and `MACOS_SIGNING_KEYCHAIN`.
An explicitly requested notarized build fails on missing credentials, rejected
or unfinished Apple submissions, malformed status responses, or failed ticket
and Gatekeeper checks. It never silently falls back to an ad-hoc release.

Apple documents the requirements and tools in
[Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
and [Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow).

## Optional GitHub Actions configuration

The release workflow keeps ad-hoc builds available without Apple credentials.
For a notarized build, configure these GitHub Actions secrets through GitHub's
settings; do not put their values in files, issues, logs, or chat:

| Secret | Required value |
| --- | --- |
| `MACOS_CERTIFICATE_P12_BASE64` | Base64 of a password-protected Developer ID Application P12 export including its private key |
| `MACOS_CERTIFICATE_PASSWORD` | Password for that P12 export |
| `MACOS_SIGNING_IDENTITY` | Exact `Developer ID Application: … (TEAMID)` identity name |
| `MACOS_NOTARY_KEY_P8_BASE64` | Base64 of an authorized App Store Connect team API private key |
| `MACOS_NOTARY_KEY_ID` | API key ID |
| `MACOS_NOTARY_ISSUER_ID` | Team API issuer ID |

Dispatch **Build macOS installer** for an existing release tag and enable
`notarize`. To require notarization for release-published events, set the
repository variable `MACOS_NOTARIZE_RELEASES` to `true`. Missing configuration
then fails the release build instead of publishing an unsigned substitute.
Restrict release tags and workflow changes to trusted maintainers and use
GitHub's protected-environment controls when approval is required by the project.

The workflow imports signing material into a temporary runner keychain, stores
the notary profile there, and avoids printing credential-bearing commands. The
source P12 and P8 files are deleted after setup. An `always()` cleanup step
deletes the temporary keychain and remaining material after success or failure.

Existing complete release asset sets are left unchanged. A partial set is an
error. Use a new release tag when changing the signing status of an already
published release; a CI rerun does not replace its existing installer bytes.

## Verification limits and external prerequisites

During the current development session the host reported **zero valid code
signing identities**. No signing certificate, private key, Apple account, or
notarization credential was inspected or imported. Consequently the notarized
path is covered by automated command/protocol tests, and an actual Apple
submission remains pending authorized signing material. A community release
must continue to disclose that it is not notarized until a particular build has
the successful evidence above.

A fresh macOS VM or separate Mac should independently verify the downloaded
artifact's first launch, a single-click installation from absent prerequisites,
model integrity, real local inference, automatic native subtitles, cache reuse,
and uninstall/update behavior. Keep host folders and model caches unshared, start
from a pre-install snapshot, and retain the exact installer SHA-256 with the
results. Test both an online first setup and a new uncached video after network
access is disabled. Probe Metal and run actual MLX inference in the guest;
virtual-machine graphics support must not be assumed to match physical hardware.

The available host has virtualization support and UTM/Parallels installed, but
only about 19 GiB free at the time of inspection and no restore image in the
examined download locations. A new VM, restore image, and downloaded models need
additional working storage. Existing personal VMs were not changed. The prior
[isolated cold-install test](COLD_INSTALL_TEST.md) is useful evidence, but uses
the host macOS and is explicitly not a fresh-OS VM test. Apple's
[distribution testing guidance](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
recommends testing a fresh distribution on another Mac where the product has not
been used before.
