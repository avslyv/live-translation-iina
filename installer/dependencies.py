"""Provision verified, per-user prerequisites for the graphical installer.

No Homebrew, administrator privileges or global PATH changes are required.
Artifacts are pinned by URL, size and SHA-256. Existing working installations
are reused; an existing app is never overwritten. Model downloads and Ollama's
LaunchAgent are managed by the parent installation workflow.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

Emitter = Callable[[dict[str, Any]], None] | None
LOCK_PATH = Path(__file__).with_name("dependencies.lock.json")
MINIMUM_IINA = (1, 3, 5)


def load_lock() -> dict[str, Any]:
    value = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("platform") != "macos-arm64":
        raise RuntimeError("Unsupported dependency manifest")
    for artifact in value["artifacts"].values():
        parsed = urlparse(artifact["url"])
        if parsed.scheme != "https" or parsed.hostname not in {
            "github.com",
            "files.pythonhosted.org",
        }:
            raise RuntimeError("A dependency URL is not an approved HTTPS source")
        if not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"]) or artifact["size"] <= 0:
            raise RuntimeError("A dependency is missing its pinned identity")
        if Path(artifact["filename"]).name != artifact["filename"]:
            raise RuntimeError("Invalid dependency filename")
    return value


def _emit(emit: Emitter, label: str, detail: str = "") -> None:
    if emit:
        emit({"type": "progress", "stage": "dependencies", "label": label, "detail": detail})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_artifact(path: Path, artifact: dict[str, Any]) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == artifact["size"]
        and sha256_file(path) == artifact["sha256"]
    )


def download_artifact(artifact: dict[str, Any], cache_dir: Path, emit: Emitter = None) -> Path:
    """Resume HTTPS downloads, but never expose unverified bytes as an artifact."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / artifact["filename"]
    if verify_artifact(target, artifact):
        _emit(emit, "Reusing downloaded prerequisite", artifact["filename"])
        return target
    if target.exists() or target.is_symlink():
        target.unlink()
    partial = target.with_name(target.name + ".partial")
    if partial.is_symlink():
        raise RuntimeError("Refusing a symlink in the dependency download cache")
    if verify_artifact(partial, artifact):
        partial.chmod(0o600)
        partial.replace(target)
        return target
    if partial.exists() and partial.stat().st_size >= artifact["size"]:
        partial.unlink()
    _emit(emit, "Downloading prerequisite", artifact["filename"])
    base = [
        "/usr/bin/curl",
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--retry",
        "3",
        "--retry-all-errors",
        "--connect-timeout",
        "30",
        "--max-time",
        "1800",
        "--speed-limit",
        "100",
        "--speed-time",
        "90",
        "--max-filesize",
        str(artifact["size"]),
    ]
    for attempt in range(2):
        resume = partial.exists() and partial.stat().st_size > 0
        command = base + (["--continue-at", "-"] if resume else [])
        result = subprocess.run(
            command + ["--output", str(partial), artifact["url"]],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            break
        if resume and attempt == 0 and result.returncode in {33, 36}:
            partial.unlink(missing_ok=True)
            continue
        raise RuntimeError("Prerequisite download failed: " + result.stderr.strip()[-1500:])
    if not verify_artifact(partial, artifact):
        partial.unlink(missing_ok=True)
        raise RuntimeError("The prerequisite download failed its SHA-256 or size check")
    partial.chmod(0o600)
    partial.replace(target)
    return target


def find_executable(name: str) -> Path | None:
    search = os.pathsep.join((os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"))
    try:
        value = shutil.which(name, path=search)
    except OSError:
        # A protected host location is unavailable to this installation, just
        # like a missing prerequisite. Provision our own verified copy instead.
        return None
    return Path(value) if value else None


def _working_binary(path: Path, option: str, marker: str) -> bool:
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        result = subprocess.run(
            [str(path), option], capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and marker.lower() in (result.stdout + result.stderr).lower()


def safe_extract_tar(archive_path: Path, destination: Path) -> None:
    """Extract trusted archives defensively, rejecting traversal and special files."""
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise RuntimeError("Unsafe path in prerequisite archive")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise RuntimeError("Unsupported special file in prerequisite archive")
            if member.issym() or member.islnk():
                base = str(path.parent) if member.issym() else "."
                resolved = PurePosixPath(posixpath.normpath(posixpath.join(base, member.linkname)))
                if resolved.is_absolute() or ".." in resolved.parts or "\\" in member.linkname:
                    raise RuntimeError("Unsafe link in prerequisite archive")
        archive.extractall(destination, filter="data")


def _version(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    return (int(match[1]), int(match[2]), int(match[3] or 0)) if match else None


def _working_iina(path: Path) -> bool:
    try:
        with (path / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        version = _version(str(info.get("CFBundleShortVersionString", "")))
        executable = path / "Contents/MacOS/IINA"
        return bool(
            version
            and version >= MINIMUM_IINA
            and executable.is_file()
            and os.access(executable, os.X_OK)
        )
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def ensure_iina(
    cache_dir: Path, applications_dir: Path | None = None, emit: Emitter = None
) -> Path:
    applications_dir = applications_dir or Path.home() / "Applications"
    for candidate in (applications_dir / "IINA.app", Path("/Applications/IINA.app")):
        if _working_iina(candidate):
            _emit(emit, "Using installed IINA", str(candidate))
            return candidate
    artifact = load_lock()["artifacts"]["iina"]
    destination = applications_dir / "IINA.app"
    if destination.exists():
        destination = applications_dir / f"IINA {artifact['version']}.app"
    if _working_iina(destination):
        return destination
    if destination.exists():
        raise RuntimeError(f"An existing application occupies {destination}; it was not changed")
    archive = download_artifact(artifact, cache_dir, emit)
    _emit(emit, "Installing IINA", "The existing player and its settings are preserved")
    scratch = Path(tempfile.mkdtemp(prefix="iina-mount-", dir=cache_dir))
    mount = scratch / "volume"
    mount.mkdir()
    mounted = False
    staging = None
    try:
        subprocess.run(
            [
                "/usr/bin/hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-plist",
                "-mountpoint",
                str(mount),
                str(archive),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        mounted = True
        source = mount / "IINA.app"
        if not _working_iina(source):
            raise RuntimeError("The official IINA image does not contain a compatible application")
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(source)],
            check=True,
            capture_output=True,
        )
        applications_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".live-translation-iina-", dir=applications_dir))
        copied_app = staging / "IINA.app"
        subprocess.run(["/usr/bin/ditto", str(source), str(copied_app)], check=True)
        # Rename only into an absent path; never replace a user's existing app.
        if destination.exists():
            raise RuntimeError(
                "The IINA destination changed during installation; no app was replaced"
            )
        copied_app.rename(destination)
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        if mounted or os.path.ismount(mount):
            result = subprocess.run(
                ["/usr/bin/hdiutil", "detach", str(mount)], check=False, capture_output=True
            )
            mounted = result.returncode != 0
        if not mounted:
            shutil.rmtree(scratch, ignore_errors=True)
    return destination


def ensure_ollama(cache_dir: Path, tools_dir: Path, emit: Emitter = None) -> Path:
    candidates = [find_executable("ollama")]
    candidates.extend(
        directory / "Ollama.app/Contents/Resources/ollama"
        for directory in (Path("/Applications"), Path.home() / "Applications")
    )
    for candidate in candidates:
        if candidate and _working_binary(candidate, "--version", "ollama"):
            _emit(emit, "Using installed Ollama", str(candidate))
            return candidate
    artifact = load_lock()["artifacts"]["ollama"]
    destination = tools_dir / ("ollama-" + artifact["version"])
    binary = destination / "ollama"
    if _working_binary(binary, "--version", "ollama"):
        return binary
    if destination.exists():
        raise RuntimeError(
            "An incomplete private Ollama installation exists; retry after reviewing "
            + str(destination)
        )
    archive = download_artifact(artifact, cache_dir, emit)
    _emit(emit, "Installing local Ollama runtime")
    tools_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ollama-", dir=tools_dir) as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir()
        safe_extract_tar(archive, staging)
        candidate = staging / "ollama"
        if not _working_binary(candidate, "--version", "ollama"):
            raise RuntimeError("The downloaded Ollama runtime cannot run on this Mac")
        staging.rename(destination)
    return binary


def ensure_ffmpeg(cache_dir: Path, tools_dir: Path, emit: Emitter = None) -> Path:
    candidate = find_executable("ffmpeg")
    if candidate and _working_binary(candidate, "-version", "ffmpeg version"):
        _emit(emit, "Using installed FFmpeg", str(candidate))
        return candidate
    artifact = load_lock()["artifacts"]["ffmpeg"]
    destination = tools_dir / artifact["version"]
    binary = destination / "ffmpeg"
    if _working_binary(binary, "-version", "ffmpeg version"):
        return binary
    if destination.exists():
        raise RuntimeError(
            "An incomplete private FFmpeg installation exists; retry after reviewing "
            + str(destination)
        )
    wheel = download_artifact(artifact, cache_dir, emit)
    _emit(
        emit, "Installing bundled FFmpeg", "Verified Apple silicon executable from imageio-ffmpeg"
    )
    tools_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ffmpeg-", dir=tools_dir) as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir()
        with zipfile.ZipFile(wheel) as package:
            # Extract only the pinned executable and license notices, never code.
            with (
                package.open(artifact["binary"]) as source,
                (staging / "ffmpeg").open("wb") as output,
            ):
                shutil.copyfileobj(source, output)
            for member in package.infolist():
                if ".dist-info/licenses/" in member.filename and not member.is_dir():
                    relative = PurePosixPath(member.filename.split(".dist-info/licenses/", 1)[1])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise RuntimeError("Unsafe license path in FFmpeg wheel")
                    target = staging / "licenses" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(package.read(member))
        (staging / "ffmpeg").chmod(0o755)
        if not _working_binary(staging / "ffmpeg", "-version", "ffmpeg version"):
            raise RuntimeError("The downloaded FFmpeg executable cannot run on this Mac")
        staging.rename(destination)
    return binary
