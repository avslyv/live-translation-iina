# macOS installer

The graphical installer sets up native IINA subtitles without Terminal commands,
Homebrew, or preinstalled Python, FFmpeg, Ollama, and IINA.

**[Download for Apple silicon](https://github.com/avslyv/live-translation-iina/releases/latest/download/Live-Translation-Installer-arm64.dmg)**

An alternative [ZIP archive](https://github.com/avslyv/live-translation-iina/releases/latest/download/Live-Translation-Installer-arm64.zip)
and `SHA256SUMS` are attached to the same GitHub release.

## Install

1. Open the DMG, then open **Live Translation Installer.app**.
2. Click **Install Everything** and leave the disk image mounted while setup runs.
3. Wait for prerequisites and model downloads to finish. The window displays the
   current step, progress, and a detailed log.
4. Quit and reopen IINA, then open a local English video. The installer also offers
   **Open IINA** when setup is complete.

Requires Apple silicon, macOS 14 or newer, and internet access for the initial
installation. Models are several gigabytes. Speech recognition and translation
use the installed local weights during playback.

The v1.4.0 release is signed with a persistent local certificate and is
**not notarized by Apple**. Its verification report records the public certificate
identity, signing mode and artifact hashes.
If macOS blocks its first launch, review the release source and follow
[Apple's instructions](https://support.apple.com/en-us/102445) for opening an app
you trust. The relevant control is in **System Settings → Privacy & Security →
Open Anyway** after trying to open the app. A managed Mac may prevent this.

## What it installs

- A private Python runtime and the speech-recognition dependencies.
- IINA, Ollama, and FFmpeg when a compatible local copy is unavailable.
- The IINA subtitle plugin and an authenticated local translation service.
- Local Whisper and Gemma model weights, reusing completed downloads.
- A **Live Translation for IINA.app** launcher in your user Applications folder.

IINA 1.5+ requires its filesystem permission for loading subtitle files through
native player commands. The installer accounts for that API requirement; older
IINA versions use only loopback networking and OSD permissions for the plugin.

Installation uses per-user folders and does not require administrator access.
Existing downloaded models and subtitle caches remain in place. Integration
settings that need replacement are backed up before they are changed.

The bootstrap checks pinned prerequisite downloads before using them. Download
sources and artifact identities are recorded in the installer manifests in this
repository. Model weights retain their own licenses and are not bundled in the
DMG or ZIP.

## Networks and local services

Setup normally asks the local Ollama server to download Gemma. On affected DNS
routes, that native transfer can reject an official CDN destination. The installer
then has a restricted download path for the official registry and its approved
CDN: HTTPS destinations are validated before following redirects, artifacts are
checked against their expected sizes and SHA-256 digests, and the manifest becomes
available only after every artifact is verified. Bounded parallel ranges retain
completed progress for a retry.

TLS verification stays enabled. This path does not change system DNS, global
proxy settings, or Ollama's own network protections. Whisper downloads use the
pinned Hugging Face transport with macOS certificate trust.

Python requests to the local Ollama and translation services connect directly to
explicit loopback HTTP endpoints. They do not inherit a system HTTP proxy, and
redirects must keep the same origin, including its port. Remote HTTPS downloads
retain certificate verification.

These paths are automatic; setup requires no Terminal commands or Homebrew.

## Validation

Cold-install validation is tracked separately from updates of an already prepared
Mac. The [cold-install report](COLD_INSTALL_TEST.md) is the source for the current
status, baseline checks, recorded results, and test limits. The earlier 1.3.0 GUI
check reused installed dependencies and models; it was an upgrade test.

## Retry or update

If a network request fails, read the detail shown in the installer and retry.
Keep completed downloads and the Application Support folder so that reusable
files do not need to be fetched again. Cancellation stops the installer's work;
it does not remove existing videos, subtitle caches, or model weights.

To update, download and run a newer installer from this repository's releases.
The installer restarts the translation service and verifies the newly loaded
code and process before reporting completion. The plugin reconnects and restores
its native Russian track automatically; pauses and explicit subtitle choices are
preserved. No manual service command or repeated enable action is needed.

IINA still needs to load changed plugin JavaScript, either when its plugins reload
or when the player is normally reopened. The installer offers **Open IINA** and
a normal restart reminder; it does not force quit an active viewing session.
See the [update recovery test](UPDATE_TEST.md).

The separate system-audio overlay still has its own
[setup guide](OVERLAY.md). The graphical installer covers the native IINA mode.

## Build from source

Maintainers can build the installer on an Apple silicon Mac with Xcode Command
Line Tools and Python 3.12+:

```bash
python3 tools/build_installer.py --version 1.4.0
```

This creates an application bundle, a DMG, a ZIP, and `SHA256SUMS` in `dist/`.
The build compiles a native AppKit frontend, copies an explicit source payload,
signs the app, and runs the frontend's noninteractive self-test. It does not run
the actual installer, download models, or copy local installation data.

Local builds create and reuse a private `Live Translation Local Build`
certificate, and the installed launcher uses the same persistent local identity.
No system trust settings are changed. This preserves the signing requirement
between local builds; see [local signing](RELEASE_SIGNING.md#persistent-local-signing).
GitHub Actions keeps its ad-hoc default, and `--sign-identity -` selects it explicitly.
A maintainer with a Developer ID Application certificate and an existing
`notarytool` keychain profile can produce a notarized build:

```bash
python3 tools/build_installer.py \
  --version 1.4.0 \
  --sign-identity "Developer ID Application: Your Name (TEAMID)" \
  --notary-profile release-notary
```

`MACOS_SIGNING_IDENTITY` and `MACOS_NOTARY_PROFILE` provide the same optional
configuration. The build uses `notarytool`, staples accepted tickets, and fails
if notarization does not succeed. Build metadata records whether notarization was
requested. Signing credentials are
never copied into the application payload.

## Release workflow

`.github/workflows/release-installer.yml` builds on the Apple silicon
`macos-15` runner when a release is published, or when dispatched manually for an
existing tag. It verifies the archives and saves the DMG, ZIP, and checksums as
workflow artifacts. Publishing attaches the complete set to the GitHub release.

Already uploaded installer assets are retained, which lets a maintainer publish
a locally verified or notarized build without a later CI run replacing it. A
partial installer asset set must be removed before retrying automated publishing,
so the checksum file cannot silently describe a different build.

The default workflow build is ad-hoc signed. Notarized releases use the optional
Developer ID build path above and are uploaded by their maintainer.
