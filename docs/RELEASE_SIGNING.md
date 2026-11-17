# Release signing and verification

Local builds reuse a **persistent local signing certificate**, using the same
pattern as codex-switcher. GitHub Actions defaults to ad-hoc signing unless a
Developer ID identity is configured. Neither local signing nor ad-hoc signing
provides Apple notarization. `Live-Translation-Installer-arm64.verification.json`
records the actual mode, public certificate identity and artifact hashes.

## Persistent local signing

On this Mac, a normal build uses `Live Translation Local Build` automatically:

```sh
python3 tools/build_installer.py
```

The certificate and non-exportable private key are created once in a dedicated
keychain under `~/Library/Application Support/LiveTranslate/build-signing`.
The directory is private (`0700`); its certificate, keychain and password file
are private user-owned files. Keep this directory between builds. An incomplete
or unsafe identity fails closed rather than silently creating a replacement.
`LT_SIGNING_DIR` can select another persistent private directory.

The app, DMG and locally installed IINA launcher use the local certificate.
Signing temporarily adds the dedicated keychain to the search list and restores
the original list, including on ordinary errors or interruption. System trust
settings are not changed, and no Apple account or timestamp service is used.
Private signing files are excluded from the installer payload and release assets.

Stable bundle IDs and the same certificate preserve the designated requirement
across changed builds. Replacing a former ad-hoc identity may still require an
initial permission decision; individual macOS permission behavior is not certified
by signature verification alone. Local signing does not establish Developer ID
or Gatekeeper approval on another Mac.

Use `--sign-identity local` explicitly for this mode, or `--sign-identity -` for
an ad-hoc build. `MACOS_SIGNING_IDENTITY` overrides the default. The optional
Developer ID path below remains separate and requires its real credentials.

Native validation compared two changed builds (1.4.0 and a test-only 1.4.1):
both passed strict signature and frontend checks, their CDHashes differed, their
leaf certificate and designated requirement matched, and the new app satisfied
the previous app's requirement. The installed launcher was also signed locally.
See [the machine-readable result](validation/local-signing-1.4.0.json).

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

No Developer ID certificate or Apple notarization credential is configured.
The new local certificate was created and used for native builds, but no Apple
submission was made. The notarized path remains covered by command/protocol
tests; a build must disclose that it is not notarized until it has the successful
Apple evidence above.

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
