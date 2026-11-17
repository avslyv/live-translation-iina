#!/usr/bin/env python3
"""Build a native Apple silicon installer app and downloadable release archives.

The app contains source and a small native frontend. Its bootstrap downloads
verified prerequisites and model weights at installation time. No local models,
virtualenv, token, cache, signing credential, or source checkout is bundled.
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
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
APP_NAME = "Live Translation Installer.app"
EXECUTABLE = "LiveTranslationInstaller"
ARTIFACT = "Live-Translation-Installer-arm64"
REQUIRED_PAYLOAD = (
    "iina_bridge.py",
    "requirements-iina.txt",
    "requirements-iina.lock.txt",
    "LICENSE",
    "tools/install_iina.py",
    "iina-plugin/main.js",
    "iina-plugin/Info.json",
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


def sign_app(app: Path, identity: str) -> None:
    arguments = ["/usr/bin/codesign", "--force", "--options", "runtime", "--sign", identity]
    arguments.append("--timestamp=none" if identity == "-" else "--timestamp")
    run(*arguments, str(app))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))


def zip_app(app: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    run("/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(destination))


def notarize(path: Path, profile: str) -> None:
    run(
        "/usr/bin/xcrun", "notarytool", "submit", str(path), "--keychain-profile", profile, "--wait"
    )


def build(args: argparse.Namespace) -> dict[str, Path]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Build the installer on an Apple silicon Mac")
    if args.notary_profile and args.sign_identity == "-":
        raise RuntimeError("Notarization requires a Developer ID Application signing identity")
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
        sign_app(app, args.sign_identity)
        run(str(binary), "--self-test")
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
        archive = work / (ARTIFACT + ".zip")
        zip_app(app, archive)
        if args.notary_profile:
            notarize(archive, args.notary_profile)
            run("/usr/bin/xcrun", "stapler", "staple", str(app))
            zip_app(app, archive)
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
            run(
                "/usr/bin/codesign",
                "--force",
                "--sign",
                args.sign_identity,
                "--timestamp",
                str(image),
            )
            notarize(image, args.notary_profile)
            run("/usr/bin/xcrun", "stapler", "staple", str(image))
        app_output = output / APP_NAME
        if app_output.exists():
            shutil.rmtree(app_output)
        shutil.copytree(app, app_output, symlinks=True)
        checksum = work / "SHA256SUMS"
        checksum.write_text(
            "".join(f"{file_hash(path)}  {path.name}\n" for path in (image, archive)),
            encoding="utf-8",
        )
        # Only expose release archives after the complete signing/notarization
        # and archive verification path has succeeded.
        artifacts = {"dmg": image, "zip": archive, "checksums": checksum}
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
