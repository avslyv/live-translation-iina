"""Verified prerequisite handling without network, installation or real media."""

import errno
import hashlib
import io
import json
import os
import plistlib
import subprocess
import tarfile
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from installer import dependencies


def artifact(data: bytes) -> dict:
    return {
        "filename": "dependency.tar.gz",
        "url": "https://github.com/vendor/repo/releases/download/v1/dependency.tar.gz",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_pinned_sources_have_hashes_and_arm64_wheel():
    lock = dependencies.load_lock()
    assert lock["artifacts"]["ffmpeg"]["binary"].startswith("imageio_ffmpeg/binaries/")
    assert "arm64.whl" in lock["artifacts"]["ffmpeg"]["url"]
    assert lock["artifacts"]["iina"]["version"] == "1.5.1"


def test_cached_download_is_verified_before_reuse(tmp_path):
    data = b"verified bytes"
    pinned = artifact(data)
    (tmp_path / pinned["filename"]).write_bytes(data)
    with mock.patch.object(dependencies.subprocess, "run") as run:
        assert dependencies.download_artifact(pinned, tmp_path).read_bytes() == data
    run.assert_not_called()


def test_corrupt_download_is_not_promoted_and_retry_starts_fresh(tmp_path):
    pinned = artifact(b"correct bytes")

    def download(command, **kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(b"corrupt bytes")
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        mock.patch.object(dependencies.subprocess, "run", side_effect=download),
        pytest.raises(RuntimeError, match="SHA-256"),
    ):
        dependencies.download_artifact(pinned, tmp_path)
    assert not (tmp_path / pinned["filename"]).exists()
    assert not (tmp_path / (pinned["filename"] + ".partial")).exists()


def test_interrupted_download_resumes_and_verifies_final_bytes(tmp_path):
    data = b"complete download"
    pinned = artifact(data)
    partial = tmp_path / (pinned["filename"] + ".partial")
    partial.write_bytes(data[:5])

    def download(command, **kwargs):
        assert "--continue-at" in command
        assert "--proto" in command and "=https" in command
        partial.write_bytes(data)
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(dependencies.subprocess, "run", side_effect=download):
        assert dependencies.download_artifact(pinned, tmp_path).read_bytes() == data
    assert not partial.exists()


def test_symlink_partial_cannot_write_outside_download_cache(tmp_path):
    outside = tmp_path / "existing-user-data"
    outside.write_bytes(b"private")
    pinned = artifact(b"download")
    (tmp_path / (pinned["filename"] + ".partial")).symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlink"):
        dependencies.download_artifact(pinned, tmp_path)
    assert outside.read_bytes() == b"private"


def test_complete_partial_is_verified_and_promoted_without_an_http_416_retry(tmp_path):
    data = b"completed before connection closed"
    pinned = artifact(data)
    partial = tmp_path / (pinned["filename"] + ".partial")
    partial.write_bytes(data)
    with mock.patch.object(dependencies.subprocess, "run") as run:
        assert dependencies.download_artifact(pinned, tmp_path).read_bytes() == data
    run.assert_not_called()


def test_corrupt_full_sized_partial_restarts_instead_of_appending(tmp_path):
    data = b"correct bytes"
    pinned = artifact(data)
    partial = tmp_path / (pinned["filename"] + ".partial")
    partial.write_bytes(b"x" * len(data))

    def download(command, **kwargs):
        assert "--continue-at" not in command
        partial.write_bytes(data)
        return subprocess.CompletedProcess(command, 0, "", "")

    with mock.patch.object(dependencies.subprocess, "run", side_effect=download):
        assert dependencies.download_artifact(pinned, tmp_path).read_bytes() == data


def make_tar(path: Path, name: str, *, link: str | None = None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        if link:
            info.type = tarfile.SYMTYPE
            info.linkname = link
            archive.addfile(info)
        else:
            info.size = 4
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(b"data"))


@pytest.mark.parametrize(
    "name,link",
    [
        ("../escape", None),
        ("/absolute", None),
        ("dir/link", "../../escape"),
    ],
)
def test_archive_traversal_and_escaping_links_are_rejected(tmp_path, name, link):
    archive = tmp_path / "bad.tar.gz"
    make_tar(archive, name, link=link)
    destination = tmp_path / "runtime"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="Unsafe"):
        dependencies.safe_extract_tar(archive, destination)
    assert not list(destination.iterdir())


def test_safe_ollama_archive_preserves_binary_mode(tmp_path):
    archive = tmp_path / "good.tar.gz"
    make_tar(archive, "ollama")
    destination = tmp_path / "runtime"
    destination.mkdir()
    dependencies.safe_extract_tar(archive, destination)
    assert (destination / "ollama").read_bytes() == b"data"
    assert os.access(destination / "ollama", os.X_OK)


def test_ffmpeg_extracts_only_verified_binary_and_license_not_python_code(tmp_path):
    wheel = tmp_path / "ffmpeg.whl"
    with zipfile.ZipFile(wheel, "w") as package:
        package.writestr("imageio_ffmpeg/binaries/ffmpeg-arm64", b"binary")
        package.writestr("imageio_ffmpeg/some_python.py", b"raise RuntimeError('do not execute')")
        package.writestr("imageio_ffmpeg-1.dist-info/licenses/LICENSE", b"license")
    lock = {
        "artifacts": {
            "ffmpeg": {
                "version": "test-version",
                "binary": "imageio_ffmpeg/binaries/ffmpeg-arm64",
            }
        }
    }
    tools = tmp_path / "tools"
    with (
        mock.patch.object(dependencies, "load_lock", return_value=lock),
        mock.patch.object(dependencies, "find_executable", return_value=None),
        mock.patch.object(dependencies, "download_artifact", return_value=wheel),
        mock.patch.object(dependencies, "_working_binary", side_effect=lambda p, *_: p.is_file()),
    ):
        executable = dependencies.ensure_ffmpeg(tmp_path, tools)
    assert executable.read_bytes() == b"binary"
    assert (executable.parent / "licenses/LICENSE").read_bytes() == b"license"
    assert not list(tools.rglob("*.py"))


def test_ffmpeg_reuses_working_install_without_network(tmp_path):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"existing")
    with (
        mock.patch.object(dependencies, "find_executable", return_value=executable),
        mock.patch.object(dependencies, "_working_binary", return_value=True),
        mock.patch.object(dependencies, "download_artifact") as download,
    ):
        assert dependencies.ensure_ffmpeg(tmp_path, tmp_path) == executable
    download.assert_not_called()


@pytest.mark.parametrize("probe", ["stat", "access", "execute"])
def test_unreadable_existing_binary_is_unavailable_at_every_probe_stage(tmp_path, probe):
    executable = tmp_path / "ollama"
    executable.write_bytes(b"existing")
    executable.chmod(0o755)
    denied = PermissionError(errno.EPERM, "Operation not permitted", str(executable))
    targets = {
        "stat": (Path, "is_file"),
        "access": (dependencies.os, "access"),
        "execute": (dependencies.subprocess, "run"),
    }
    target, attribute = targets[probe]
    with mock.patch.object(target, attribute, side_effect=denied):
        assert dependencies._working_binary(executable, "--version", "ollama") is False


def test_unreadable_executable_search_is_treated_as_missing():
    with mock.patch.object(
        dependencies.shutil, "which", side_effect=PermissionError(errno.EPERM, "Host path denied")
    ):
        assert dependencies.find_executable("ollama") is None


def test_ollama_provisions_private_runtime_when_host_app_stat_is_denied(tmp_path, monkeypatch):
    blocked = Path("/Applications/Ollama.app/Contents/Resources/ollama")
    archive = tmp_path / "ollama.tar.gz"
    make_tar(archive, "ollama")
    tools = tmp_path / "private-tools"
    real_is_file = Path.is_file

    def is_file(path):
        if path == blocked:
            raise PermissionError(errno.EPERM, "Host app denied", str(path))
        return real_is_file(path)

    def version(command, **kwargs):
        assert command[0] != str(blocked)
        return subprocess.CompletedProcess(command, 0, "ollama version fixture", "")

    with (
        mock.patch.object(dependencies, "find_executable", return_value=blocked),
        mock.patch.object(dependencies, "download_artifact", return_value=archive) as download,
        mock.patch.object(dependencies.subprocess, "run", side_effect=version),
        mock.patch.object(Path, "is_file", is_file),
    ):
        result = dependencies.ensure_ollama(tmp_path / "cache", tools)
    assert result.is_relative_to(tools)
    assert result.read_bytes() == b"data"
    download.assert_called_once()


def test_unreadable_existing_iina_metadata_is_unavailable():
    with mock.patch.object(
        Path, "open", side_effect=PermissionError(errno.EPERM, "Host app denied")
    ):
        assert dependencies._working_iina(Path("/Applications/IINA.app")) is False


def test_iina_reuses_compatible_app_without_replacing_it(tmp_path):
    app = tmp_path / "IINA.app"
    executable = app / "Contents/MacOS/IINA"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"existing signed app")
    executable.chmod(0o755)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "1.3.5"})
    )
    with mock.patch.object(dependencies, "download_artifact") as download:
        assert dependencies.ensure_iina(tmp_path / "cache", tmp_path) == app
    assert executable.read_bytes() == b"existing signed app"
    download.assert_not_called()


def test_manifest_rejects_untrusted_download_host(tmp_path, monkeypatch):
    value = dependencies.load_lock()
    value["artifacts"]["iina"]["url"] = "https://unrelated.invalid/iina.dmg"
    manifest = tmp_path / "lock.json"
    manifest.write_text(json.dumps(value))
    monkeypatch.setattr(dependencies, "LOCK_PATH", manifest)
    with pytest.raises(RuntimeError, match="approved HTTPS"):
        dependencies.load_lock()


def test_cancel_during_hdiutil_attach_still_detaches_a_mounted_image(tmp_path):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "attach" in command:
            raise InterruptedError("Installer cancelled")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    with (
        mock.patch.object(dependencies, "_working_iina", return_value=False),
        mock.patch.object(dependencies, "download_artifact", return_value=tmp_path / "image.dmg"),
        mock.patch.object(dependencies.subprocess, "run", side_effect=run),
        mock.patch.object(dependencies.os.path, "ismount", return_value=True),
        pytest.raises(InterruptedError, match="cancelled"),
    ):
        dependencies.ensure_iina(tmp_path, tmp_path / "apps")
    assert any("detach" in command for command in commands)
