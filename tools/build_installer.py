#!/usr/bin/env python3
"""Build a native Apple silicon installer app and downloadable release archives.

The app contains source and a small native frontend. Its bootstrap downloads
verified prerequisites and the large Whisper/Gemma weights at installation time.
No private models, virtualenv, token, cache, signing credential, or source checkout
is bundled. A pinned public Silero VAD asset and its MIT license are included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
APP_NAME = "Live Translation Installer.app"
EXECUTABLE = "LiveTranslationInstaller"
ARTIFACT = "Live-Translation-Installer-arm64"
PLUGIN_ARTIFACT = "Live-Translation-IINA.iinaplgz"
VERIFICATION_ARTIFACT = ARTIFACT + ".verification.json"
REQUIRED_PAYLOAD = (
    "iina_bridge.py",
    "requirements-iina.txt",
    "requirements-iina.lock.txt",
    "LICENSE",
    "tools/install_iina.py",
    "iina-plugin/main.js",
    "iina-plugin/Info.json",
    "iina-plugin/sidebar.html",
    "iina-plugin/sidebar.css",
    "iina-plugin/sidebar.js",
    "live_translation/assets/silero_vad.jit",
    "live_translation/assets/silero.json",
    "live_translation/assets/SILERO_LICENSE",
    "installer/bootstrap.py",
    "installer/dependencies.py",
    "installer/dependencies.lock.json",
    "installer/python.lock.json",
)


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def payload_files(source: Path) -> list[Path]:
    """Use a source-only allowlist instead of recursively copying a checkout."""
    source = source.resolve()
    paths = [source / relative for relative in REQUIRED_PAYLOAD]
    paths.extend(sorted((source / "live_translation").glob("*.py")))
    icon = source / "LiveTranslate.app/Contents/Resources/AppIcon.icns"
    if icon.is_file():
        paths.append(icon)
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f"Missing installer payload source: {path.relative_to(source)}")
        if not path.resolve().is_relative_to(source):
            raise RuntimeError(f"Payload source escapes the checkout: {path.relative_to(source)}")
    plugin = (source / "iina-plugin/main.js").read_text(encoding="utf-8")
    if 'let token = "";' not in plugin:
        raise RuntimeError("Refusing to bundle an installed plugin or changed token template")
    return paths


def copy_payload(source: Path, destination: Path) -> dict[str, str]:
    hashes = {}
    source = source.resolve()
    for path in payload_files(source):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o644)
        hashes[relative.as_posix()] = file_hash(target)
    return hashes


def signing_arguments(
    identity: str, keychain: Path | None = None, *, hardened: bool = True
) -> list[str]:
    if identity != "-" and not (
        identity.startswith("Developer ID Application: ")
        or re.fullmatch(r"[A-Fa-f0-9]{40}", identity)
    ):
        raise RuntimeError("Select a Developer ID Application identity or '-' for an ad-hoc build")
    arguments = ["/usr/bin/codesign", "--force", "--sign", identity]
    if hardened:
        arguments.extend(("--options", "runtime"))
    arguments.append("--timestamp=none" if identity == "-" else "--timestamp")
    if keychain is not None:
        arguments.extend(("--keychain", str(keychain)))
    return arguments


def sign_app(app: Path, identity: str, keychain: Path | None = None) -> None:
    arguments = signing_arguments(identity, keychain)
    run(*arguments, str(app))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    if identity != "-":
        details = run(
            "/usr/bin/codesign",
            "--display",
            "--verbose=4",
            str(app),
            capture_output=True,
            text=True,
        )
        observed = (details.stdout or "") + (details.stderr or "")
        if (
            "Authority=Developer ID Application:" not in observed
            or not re.search(r"TeamIdentifier=[A-Z0-9]{10}(?:\n|$)", observed)
            or not re.search(r"flags=0x[0-9a-fA-F]+\([^\n]*runtime", observed)
            or not re.search(r"^Timestamp=(?!none\s*$).+", observed, re.MULTILINE)
        ):
            raise RuntimeError(
                "The app lacks a timestamped Developer ID hardened-runtime signature"
            )


def zip_app(app: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    run("/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(destination))


def verify_app_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        required = {
            APP_NAME + "/Contents/Info.plist",
            APP_NAME + "/Contents/MacOS/" + EXECUTABLE,
            APP_NAME + "/Contents/Resources/installer-build.json",
        }
        if (
            archive.testzip() is not None
            or not required.issubset(names)
            or any(Path(name).is_absolute() or ".." in Path(name).parts for name in names)
        ):
            raise RuntimeError("The installer ZIP failed its content/integrity check")


def notarize(path: Path, profile: str, keychain: Path | None = None) -> dict:
    """A successful CLI exit alone is not proof that Apple accepted an upload."""
    authentication = ["--keychain-profile", profile]
    if keychain is not None:
        authentication.extend(("--keychain", str(keychain)))
    failure = None
    try:
        result = run(
            "/usr/bin/xcrun",
            "notarytool",
            "submit",
            str(path),
            *authentication,
            "--wait",
            "--timeout",
            "30m",
            "--output-format",
            "json",
            capture_output=True,
            text=True,
            timeout=1900,
        )
        output = result.stdout
    except subprocess.CalledProcessError as error:
        output, failure = error.stdout, error
    try:
        body = json.loads(output or "")
        if not isinstance(body, dict):
            raise ValueError("Expected a notarization object")
        submission_id = str(uuid.UUID(body.get("id", "")))
        status = body.get("status")
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeError("Apple notarization did not return a valid submission status") from (
            failure or error
        )
    log = run(
        "/usr/bin/xcrun",
        "notarytool",
        "log",
        submission_id,
        *authentication,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        log_body = json.loads(log.stdout)
        issues = log_body.get("issues") or []
        if not isinstance(log_body, dict) or not isinstance(issues, list):
            raise ValueError("Invalid notary log")
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeError("Apple notarization log could not be validated") from error
    if failure is not None or status != "Accepted":
        raise RuntimeError(
            f"Apple did not accept {path.name}: {status!r}; submission {submission_id}. "
            "Inspect its notarytool log before retrying."
        ) from failure
    if log_body.get("jobId") != submission_id or log_body.get("status") != "Accepted":
        raise RuntimeError("Apple's notary log does not match the accepted submission")
    if any(not isinstance(issue, dict) or issue.get("severity") == "error" for issue in issues):
        raise RuntimeError(
            "Apple's accepted submission still reports an error; review its notary log"
        )
    return {
        "submission_id": submission_id,
        "status": status,
        "warnings": sum(issue.get("severity") == "warning" for issue in issues),
        "log_sha256": hashlib.sha256(log.stdout.encode()).hexdigest(),
    }


def verify_notarized_app(app: Path) -> None:
    run("/usr/bin/xcrun", "stapler", "validate", str(app))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    run("/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=4", str(app))
    if Path("/usr/bin/syspolicy_check").is_file():
        run("/usr/bin/syspolicy_check", "distribution", str(app))


def pack_plugin(source: Path, destination: Path) -> None:
    """IINA's official .iinaplgz format is ZIP with plugin files at its root."""
    source = source.resolve()
    # Reuse the installer allowlist and its installed-token/symlink guards.
    payload_files(source)
    info_path = source / "iina-plugin/Info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if not isinstance(info, dict) or info.get("entry") != "main.js":
        raise RuntimeError("The standalone plugin entry must be main.js")
    files = {
        "Info.json": info_path,
        "main.js": source / "iina-plugin/main.js",
        "LICENSE": source / "LICENSE",
        **{
            name: source / "iina-plugin" / name
            for name in ("sidebar.html", "sidebar.css", "sidebar.js")
        },
    }
    # Optional package-relative UI resources must be declared in Info.json.
    for field in ("preferencesPage", "helpPage"):
        if relative := info.get(field):
            if (
                not isinstance(relative, str)
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
            ):
                raise RuntimeError("The plugin contains an unsafe UI resource path")
            resource = source / "iina-plugin" / relative
            if not resource.is_file() or not resource.resolve().is_relative_to(
                source / "iina-plugin"
            ):
                raise RuntimeError("The plugin UI resource is missing or escapes its package")
            files[relative] = resource
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != set(files):
            raise RuntimeError("The standalone IINA plugin archive failed verification")


def build(args: argparse.Namespace) -> dict[str, Path]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Build the installer on an Apple silicon Mac")
    if args.notary_profile and args.sign_identity == "-":
        raise RuntimeError("Notarization requires a Developer ID Application signing identity")
    signing_arguments(args.sign_identity, args.keychain)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", args.version):
        raise RuntimeError("Use a semantic release version such as 1.3.0")
    source = SOURCE.resolve()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Validate the complete source set before replacing any prior build output.
    payload_files(source)
    for relative in ("installer/Installer.swift", "installer/bootstrap.sh"):
        if not (source / relative).is_file():
            raise RuntimeError(f"Missing installer source: {relative}")
    with tempfile.TemporaryDirectory(prefix="live-translation-installer-build-") as temporary:
        work = Path(temporary)
        app = work / APP_NAME
        contents = app / "Contents"
        resources = contents / "Resources"
        binary = contents / "MacOS" / EXECUTABLE
        binary.parent.mkdir(parents=True)
        resources.mkdir(parents=True)
        hashes = copy_payload(source, resources / "payload")
        bootstrap = resources / "bootstrap.sh"
        shutil.copyfile(source / "installer/bootstrap.sh", bootstrap)
        bootstrap.chmod(0o755)
        icon = source / "LiveTranslate.app/Contents/Resources/AppIcon.icns"
        if icon.is_file():
            shutil.copyfile(icon, resources / "AppIcon.icns")
        info = {
            "CFBundleName": "Live Translation Installer",
            "CFBundleDisplayName": "Live Translation Installer",
            "CFBundleIdentifier": "io.github.avslyv.live-translation-installer",
            "CFBundleVersion": args.version,
            "CFBundleShortVersionString": args.version,
            "CFBundleExecutable": EXECUTABLE,
            "CFBundlePackageType": "APPL",
            "CFBundleIconFile": "AppIcon",
            "LSMinimumSystemVersion": "14.0",
            "LSUIElement": False,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "MIT · avslyv and contributors; based on KazKozDev/live-translation",
        }
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(info, stream)
        metadata = {
            "version": args.version,
            "architecture": "arm64",
            "minimum_macos": "14.0",
            "signing": "ad-hoc" if args.sign_identity == "-" else "Developer ID",
            "notarization_requested": bool(args.notary_profile),
            "payload_sha256": hashes,
        }
        (resources / "installer-build.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        sdk = subprocess.check_output(
            ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True
        ).strip()
        run(
            "/usr/bin/xcrun",
            "swiftc",
            "-O",
            "-whole-module-optimization",
            "-target",
            "arm64-apple-macosx14.0",
            "-file-prefix-map",
            str(source) + "=/Source",
            "-debug-prefix-map",
            str(source) + "=/Source",
            "-sdk",
            sdk,
            "-framework",
            "Cocoa",
            str(source / "installer/Installer.swift"),
            "-o",
            str(binary),
        )
        sign_app(app, args.sign_identity, args.keychain)
        run(str(binary), "--self-test")
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
        archive = work / (ARTIFACT + ".zip")
        zip_app(app, archive)
        verification = {
            "schema": 1,
            "version": args.version,
            "signing": "ad-hoc" if args.sign_identity == "-" else "Developer ID Application",
            "notarized": False,
            "gatekeeper_assessed": False,
            "notarization": {},
        }
        if args.notary_profile:
            verification["notarization"]["app"] = notarize(
                archive, args.notary_profile, args.keychain
            )
            run("/usr/bin/xcrun", "stapler", "staple", str(app))
            verify_notarized_app(app)
            zip_app(app, archive)
        verify_app_archive(archive)
        volume = work / "volume"
        volume.mkdir()
        shutil.copytree(app, volume / APP_NAME, symlinks=True)
        (volume / "Start here.txt").write_text(
            "Live Translation for IINA\n\n"
            "1. Open Live Translation Installer.app.\n"
            "2. Click Install and keep this disk image mounted until setup finishes.\n"
            "3. Quit and reopen IINA, then open a local English video.\n\n"
            "The installer downloads local prerequisites and model weights. "
            "No Terminal commands or Homebrew are required. Existing model downloads "
            "and subtitle caches are retained.\n\n"
            "Requires Apple silicon and macOS 14 or newer.\n"
            "Documentation: https://github.com/avslyv/live-translation-iina\n",
            encoding="utf-8",
        )
        image = work / (ARTIFACT + ".dmg")
        run(
            "/usr/bin/hdiutil",
            "create",
            "-volname",
            "Live Translation Installer",
            "-srcfolder",
            str(volume),
            "-format",
            "UDZO",
            "-ov",
            str(image),
        )
        if args.notary_profile:
            run(*signing_arguments(args.sign_identity, args.keychain, hardened=False), str(image))
            verification["notarization"]["dmg"] = notarize(
                image, args.notary_profile, args.keychain
            )
            run("/usr/bin/xcrun", "stapler", "staple", str(image))
            run("/usr/bin/xcrun", "stapler", "validate", str(image))
            run(
                "/usr/sbin/spctl",
                "--assess",
                "--type",
                "open",
                "--context",
                "context:primary-signature",
                "--verbose=4",
                str(image),
            )
            verification["notarized"] = True
            verification["gatekeeper_assessed"] = True
        run("/usr/bin/hdiutil", "verify", str(image))
        plugin = work / PLUGIN_ARTIFACT
        pack_plugin(source, plugin)
        report = work / VERIFICATION_ARTIFACT
        verification["artifacts"] = {
            path.name: file_hash(path) for path in (image, archive, plugin)
        }
        report.write_text(
            json.dumps(verification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        app_output = output / APP_NAME
        if app_output.exists():
            shutil.rmtree(app_output)
        shutil.copytree(app, app_output, symlinks=True)
        checksum = work / "SHA256SUMS"
        checksum.write_text(
            "".join(
                f"{file_hash(path)}  {path.name}\n" for path in (image, archive, plugin, report)
            ),
            encoding="utf-8",
        )
        # Only expose release archives after the complete signing/notarization
        # and archive verification path has succeeded.
        artifacts = {
            "dmg": image,
            "zip": archive,
            "plugin": plugin,
            "verification": report,
            "checksums": checksum,
        }
        for kind, path in artifacts.items():
            destination = output / path.name
            shutil.copyfile(path, destination)
            artifacts[kind] = destination
    return {"app": app_output, **artifacts}


def main(argv: list[str] | None = None) -> int:
    with (SOURCE / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", default=version, help="release version (defaults to pyproject.toml)"
    )
    parser.add_argument("--output-dir", type=Path, default=SOURCE / "dist")
    parser.add_argument(
        "--sign-identity",
        default=os.environ.get("MACOS_SIGNING_IDENTITY", "-"),
        help="codesign identity; '-' creates an ad-hoc signature",
    )
    parser.add_argument(
        "--notary-profile",
        default=os.environ.get("MACOS_NOTARY_PROFILE"),
        help="optional existing notarytool keychain profile; requires Developer ID",
    )
    parser.add_argument(
        "--keychain",
        type=Path,
        default=os.environ.get("MACOS_SIGNING_KEYCHAIN"),
        help="optional temporary keychain containing the signing identity and notary profile",
    )
    args = parser.parse_args(argv)
    try:
        for kind, path in build(args).items():
            print(f"{kind}: {path}")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Installer build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
