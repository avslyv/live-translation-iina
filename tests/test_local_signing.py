"""Exercise local-signing persistence and failure recovery without real keychains."""

import os
import shlex
import signal
import subprocess
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import local_signing as signing

SHA1 = "12" * 20
SHA256 = "34" * 32
PASSWORD = "ab" * 32
REQUIREMENT = f'identifier "test.live-translation.installer" and certificate leaf = H"{SHA1.lower()}"'


def private_file(path: Path, content: str = "fixture") -> None:
    path.write_text(content)
    path.chmod(0o600)


def existing_identity(directory: Path) -> Path:
    directory.mkdir(mode=0o700)
    for name, content in (
        ("identity.keychain-db", "private fixture keychain"),
        ("password", PASSWORD + "\n"),
        ("certificate.pem", "public fixture certificate"),
    ):
        private_file(directory / name, content)
    return directory


class NativeModel:
    """A command allowlist with real fixture files and a mutable search list."""

    def __init__(self):
        self.original = ["/fixture/login with spaces.keychain-db", "/fixture/other.keychain-db"]
        self.search_list = self.original.copy()
        self.calls = []
        self.fail = None
        self.ignore_restore = False
        self.fingerprint = SHA1
        self.details = f"Authority={signing.COMMON_NAME}\nCodeDirectory flags=0x10000(runtime)\n"
        self.requirement = f"designated => {REQUIREMENT}\n"
        self.temporary_artifacts = []

    def __call__(self, *args, **kwargs):
        args = tuple(map(str, args))
        self.calls.append((args, kwargs))
        tool, verb, *rest = args
        if self.fail is not None and self.fail(args):
            raise RuntimeError("injected native failure")
        if tool == "/usr/bin/security":
            if verb == "list-keychains":
                if "-s" in rest:
                    updated = list(rest[rest.index("-s") + 1 :])
                    if not (self.ignore_restore and updated == self.original):
                        self.search_list = updated
                    return ""
                assert rest == ["-d", "user"]
                return "\n".join(shlex.quote(path) for path in self.search_list)
            if verb == "create-keychain":
                private_file(Path(rest[-1]), "new fixture keychain")
                self.search_list.append(rest[-1])
                return ""
            if verb == "delete-keychain":
                Path(rest[-1]).unlink(missing_ok=True)
                self.search_list = [path for path in self.search_list if path != rest[-1]]
                return ""
            if verb in {"unlock-keychain", "import", "set-key-partition-list"}:
                return ""
        if tool == "/usr/bin/openssl":
            if verb == "req":
                private_key = Path(rest[rest.index("-keyout") + 1])
                certificate = Path(rest[rest.index("-out") + 1])
                config = Path(rest[rest.index("-config") + 1]).read_text()
                assert f"CN = {signing.COMMON_NAME}" in config
                assert "CA:false" in config and "extendedKeyUsage = critical,codeSigning" in config
                private_file(private_key, "fixture private key")
                private_file(certificate, "fixture certificate")
                self.temporary_artifacts.append(private_key)
                return ""
            if verb == "pkcs12":
                archive = Path(rest[rest.index("-out") + 1])
                private_file(archive, "fixture private archive")
                self.temporary_artifacts.append(archive)
                return ""
            if verb == "x509":
                if "-checkend" in rest:
                    return "Certificate will not expire\n"
                value = self.fingerprint if "-sha1" in rest else SHA256
                return "Fingerprint=" + ":".join(value[i : i + 2] for i in range(0, len(value), 2)) + "\n"
        if tool == "/usr/bin/codesign":
            if verb == "--force" or verb == "--verify":
                return ""
            if verb == "--display":
                assert kwargs.get("include_stderr") is True
                return self.requirement if "-r-" in rest else self.details
        raise AssertionError(f"Unexpected native operation: {tool} {verb}")


@pytest.fixture(autouse=True)
def isolated_signing_environment(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Local-signing unit tests must never run native tools")

    monkeypatch.setattr(signing.subprocess, "run", forbidden)
    monkeypatch.setattr(signing, "LOCK_ROOT", tmp_path / "uid-lock")
    monkeypatch.setattr(signing, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LT_SIGNING_DIR", raising=False)


@pytest.fixture
def native(monkeypatch):
    model = NativeModel()
    monkeypatch.setattr(signing, "_run", model)
    return model


def test_first_creation_is_private_persistent_and_search_list_is_temporary(tmp_path, native):
    directory = tmp_path / "identity"
    with signing.local_identity(directory) as identity:
        assert identity == signing.LocalIdentity(SHA1, SHA256, directory / "identity.keychain-db")
        assert native.search_list == [*native.original, str(identity.keychain)]
        assert directory.stat().st_mode & 0o777 == 0o700
        for name in ("identity.keychain-db", "password", "certificate.pem"):
            assert (directory / name).stat().st_mode & 0o777 == 0o600
        with pytest.raises(FrozenInstanceError):
            identity.fingerprint = "changed"
    assert native.search_list == native.original
    assert all(not path.exists() for path in native.temporary_artifacts)
    first_password = (directory / "password").read_bytes()
    native.calls.clear()
    with signing.local_identity(directory) as second:
        assert second == identity
    assert (directory / "password").read_bytes() == first_password
    assert not any(args[1] in {"req", "pkcs12", "create-keychain", "import"} for args, _ in native.calls)


@pytest.mark.parametrize("source", ["environment", "default", "explicit"])
def test_identity_directory_selection_is_isolated(tmp_path, monkeypatch, native, source):
    home = Path(os.environ["HOME"])
    expected = home / "Library/Application Support/LiveTranslate/build-signing"
    selected = None
    if source == "environment":
        expected = tmp_path / "environment"
        monkeypatch.setenv("LT_SIGNING_DIR", str(expected))
    elif source == "explicit":
        expected = tmp_path / "explicit"
        monkeypatch.setenv("LT_SIGNING_DIR", str(tmp_path / "ignored"))
        selected = expected
    with signing.local_identity(selected) as identity:
        assert identity.keychain.parent == expected


@pytest.mark.parametrize("missing", ["identity.keychain-db", "password", "certificate.pem"])
def test_incomplete_identity_fails_closed_without_regeneration(tmp_path, native, missing):
    directory = existing_identity(tmp_path / "identity")
    (directory / missing).unlink()
    remaining = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(RuntimeError, match="incomplete"), signing.local_identity(directory):
        pytest.fail("Incomplete identity was accepted")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == remaining
    assert native.search_list == native.original
    assert not any(args[1] in {"req", "create-keychain", "import"} for args, _ in native.calls)


@pytest.mark.parametrize("name,mode", [(None, 0o750), ("password", 0o640), ("certificate.pem", 0o644), ("identity.keychain-db", 0o660)])
def test_public_permissions_are_rejected(tmp_path, native, name, mode):
    directory = existing_identity(tmp_path / "identity")
    (directory if name is None else directory / name).chmod(mode)
    with pytest.raises(RuntimeError, match="private"), signing.local_identity(directory):
        pytest.fail("Public signing material was accepted")
    assert native.search_list == native.original
    assert not any(args[1] == "unlock-keychain" for args, _ in native.calls)


@pytest.mark.parametrize("name", [None, "password", "certificate.pem", "identity.keychain-db"])
def test_symlink_directory_or_identity_file_is_rejected(tmp_path, native, name):
    directory = existing_identity(tmp_path / "identity")
    if name is None:
        linked = tmp_path / "linked-identity"
        linked.symlink_to(directory, target_is_directory=True)
        selected = linked
    else:
        original = directory / name
        target = tmp_path / (name + ".original")
        original.rename(target)
        original.symlink_to(target)
        selected = directory
    with pytest.raises(RuntimeError, match="symlink|private"), signing.local_identity(selected):
        pytest.fail("Symlink signing material was accepted")
    assert native.search_list == native.original


def test_wrong_file_owner_is_rejected_before_unlock(tmp_path, monkeypatch, native):
    directory = existing_identity(tmp_path / "identity")
    real_lstat = Path.lstat

    def wrong_owner(path):
        result = real_lstat(path)
        if path == directory / "password":
            return SimpleNamespace(st_mode=result.st_mode, st_uid=os.getuid() + 1)
        return result

    monkeypatch.setattr(Path, "lstat", wrong_owner)
    with pytest.raises(RuntimeError, match="owned by you"), signing.local_identity(directory):
        pytest.fail("Another user's signing material was accepted")
    assert not any(args[1] == "unlock-keychain" for args, _ in native.calls)


@pytest.mark.parametrize("password", ["", "short", "AB" * 32, "cd" * 31, "ef" * 33])
def test_malformed_password_is_rejected_without_native_unlock(tmp_path, native, password):
    directory = existing_identity(tmp_path / "identity")
    (directory / "password").write_text(password)
    with (
        pytest.raises(RuntimeError, match="password file is invalid"),
        signing.local_identity(directory),
    ):
        pytest.fail("Invalid password was accepted")
    assert not any(args[1] == "unlock-keychain" for args, _ in native.calls)


@pytest.mark.parametrize("fingerprint", ["", "not-a-fingerprint", "12" * 19, "12" * 21, "GG" * 20])
def test_invalid_fingerprint_cannot_select_another_identity(tmp_path, native, fingerprint):
    directory = existing_identity(tmp_path / "identity")
    native.fingerprint = fingerprint
    with pytest.raises(RuntimeError, match="identify"), signing.local_identity(directory):
        pytest.fail("Invalid fingerprint was accepted")
    assert native.search_list == native.original


@pytest.mark.parametrize("verb", ["req", "pkcs12", "create-keychain", "unlock-keychain", "import", "set-key-partition-list"])
def test_failed_creation_cleans_new_material_and_restores_search_list(tmp_path, native, verb):
    directory = tmp_path / "identity"
    native.fail = lambda args: args[1] == verb
    with pytest.raises(RuntimeError, match="injected"), signing.local_identity(directory):
        pytest.fail("Failed creation yielded an identity")
    assert not directory.exists()
    assert native.search_list == native.original


def test_body_failure_restores_search_list_and_signal_handlers(tmp_path, native):
    directory = existing_identity(tmp_path / "identity")
    before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    with pytest.raises(LookupError, match="body failed"), signing.local_identity(directory):
        assert native.search_list != native.original
        raise LookupError("body failed")
    assert native.search_list == native.original
    assert {number: signal.getsignal(number) for number in before} == before
    assert directory.exists()


def test_search_list_restore_failure_is_reported(tmp_path, native):
    directory = existing_identity(tmp_path / "identity")
    native.ignore_restore = True
    with (
        pytest.raises(RuntimeError, match="restore the original keychain search list"),
        signing.local_identity(directory),
    ):
        pass


def test_expired_certificate_preserves_existing_identity_and_restores_list(tmp_path, native):
    directory = existing_identity(tmp_path / "identity")
    native.fail = lambda args: "-checkend" in args
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(RuntimeError, match="injected"), signing.local_identity(directory):
        pytest.fail("Expired certificate was accepted")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert native.search_list == native.original


def test_lock_symlink_is_rejected_without_following_target(tmp_path, native):
    signing.LOCK_ROOT.mkdir(mode=0o700)
    target = tmp_path / "must-not-truncate"
    private_file(target, "unchanged")
    (signing.LOCK_ROOT / "lock").symlink_to(target)
    with pytest.raises(OSError), signing.local_identity(tmp_path / "identity"):
        pytest.fail("Symlink lock was accepted")
    assert target.read_text() == "unchanged"
    assert native.calls == []


def test_regular_file_cannot_be_used_as_identity_directory(tmp_path, native):
    directory = tmp_path / "identity"
    private_file(directory, "must remain a file")
    with pytest.raises(RuntimeError, match="private"), signing.local_identity(directory):
        pytest.fail("Regular file was accepted as a directory")
    assert directory.read_text() == "must remain a file"
    assert native.search_list == native.original


def test_public_lock_directory_is_rejected_before_native_operations(tmp_path, native):
    signing.LOCK_ROOT.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="private"), signing.local_identity(tmp_path / "identity"):
        pytest.fail("Public lock directory was accepted")
    assert native.calls == []


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_graceful_interruption_restores_search_list_and_handlers(tmp_path, native, number):
    directory = existing_identity(tmp_path / "identity")
    original_handler = signal.getsignal(number)
    with pytest.raises(SystemExit) as caught, signing.local_identity(directory):
        handler = signal.getsignal(number)
        assert callable(handler)
        handler(number, None)
    assert caught.value.code == 128 + number
    assert native.search_list == native.original
    assert signal.getsignal(number) == original_handler
    assert directory.exists()


def test_uid_lock_serializes_independent_threads(tmp_path, native):
    directory = existing_identity(tmp_path / "identity")
    first_entered = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    failures = []

    def first():
        try:
            with signing.local_identity(directory):
                first_entered.set()
                assert release_first.wait(timeout=5)
        except BaseException as error:
            failures.append(error)

    def second():
        try:
            second_started.set()
            with signing.local_identity(directory):
                second_entered.set()
        except BaseException as error:
            failures.append(error)

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    try:
        one.start()
        assert first_entered.wait(timeout=5)
        two.start()
        assert second_started.wait(timeout=5)
        assert not second_entered.wait(timeout=0.1)
    finally:
        release_first.set()
        one.join(timeout=5)
        if two.ident is not None:
            two.join(timeout=5)
    assert not one.is_alive() and not two.is_alive()
    assert failures == []
    assert second_entered.is_set()
    assert native.search_list == native.original


@pytest.mark.parametrize("hardened", [True, False])
@pytest.mark.parametrize("kind", ["app", "dmg"])
def test_signing_uses_local_identity_and_validates_result(tmp_path, native, hardened, kind):
    directory = existing_identity(tmp_path / "identity")
    bundle = tmp_path / ("Installer." + kind)
    bundle.mkdir() if kind == "app" else bundle.write_bytes(b"fixture disk image")
    result = signing.sign_local(bundle, directory, hardened=hardened)
    assert result == {
        "mode": "local-certificate",
        "authority": signing.COMMON_NAME,
        "certificate_sha1": SHA1,
        "certificate_sha256": SHA256,
        "designated_requirement": REQUIREMENT,
    }
    calls = [args for args, _ in native.calls if args[0] == "/usr/bin/codesign"]
    assert len(calls) == 4
    sign = calls[0]
    assert sign[sign.index("--sign") + 1] == SHA1
    assert sign[sign.index("--keychain") + 1] == str(directory / "identity.keychain-db")
    assert "--timestamp=none" in sign and sign[-1] == str(bundle)
    assert ("--options" in sign) == hardened
    if hardened:
        assert sign[sign.index("--options") + 1] == "runtime"
    assert calls[1] == ("/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle))
    assert native.search_list == native.original


def test_updated_app_reuses_certificate_and_designated_requirement(tmp_path, native):
    directory = existing_identity(tmp_path / "identity")
    bundle = tmp_path / "Installer.app"
    bundle.mkdir()
    executable = bundle / "fixture-binary"
    executable.write_bytes(b"version one")
    first = signing.sign_local(bundle, directory)
    executable.write_bytes(b"different version two")
    second = signing.sign_local(bundle, directory)
    assert first == second
    assert not any(args[1] in {"req", "create-keychain", "import"} for args, _ in native.calls)


@pytest.mark.parametrize("failure", ["sign", "verify", "authority", "runtime", "requirement"])
def test_invalid_or_failed_signature_never_reports_success(tmp_path, native, failure):
    directory = existing_identity(tmp_path / "identity")
    bundle = tmp_path / "Installer.app"
    bundle.mkdir()
    if failure in {"sign", "verify"}:
        verb = "--force" if failure == "sign" else "--verify"
        native.fail = lambda args: args[0] == "/usr/bin/codesign" and args[1] == verb
    elif failure == "authority":
        native.details = "Authority=Unrelated Local Build\nCodeDirectory flags=0x10000(runtime)\n"
    elif failure == "runtime":
        native.details = f"Authority={signing.COMMON_NAME}\nCodeDirectory flags=0x0(none)\n"
    else:
        native.requirement = "no designated requirement\n"
    with pytest.raises(RuntimeError):
        signing.sign_local(bundle, directory)
    assert native.search_list == native.original
    assert directory.exists()


def test_non_hardened_mode_does_not_require_runtime_flag(tmp_path, native):
    directory = existing_identity(tmp_path / "identity")
    bundle = tmp_path / "Installer.app"
    bundle.mkdir()
    native.details = f"Authority={signing.COMMON_NAME}\nCodeDirectory flags=0x0(none)\n"
    assert signing.sign_local(bundle, directory, hardened=False)["mode"] == "local-certificate"


@pytest.mark.parametrize("kind", ["file", "directory", "missing"])
def test_invalid_signing_target_is_rejected_before_keychain_access(tmp_path, native, kind):
    bundle = tmp_path / "unrecognized"
    if kind == "file":
        bundle.write_text("fixture")
    elif kind == "directory":
        bundle.mkdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        signing.sign_local(bundle)
    assert native.calls == []


def test_local_signing_never_changes_trust_or_system_security(tmp_path, native):
    bundle = tmp_path / "Installer.app"
    bundle.mkdir()
    signing.sign_local(bundle, tmp_path / "identity")
    allowed_security_verbs = {
        "list-keychains", "create-keychain", "unlock-keychain", "import",
        "set-key-partition-list", "delete-keychain",
    }
    for args, kwargs in native.calls:
        assert args[0] in {"/usr/bin/security", "/usr/bin/openssl", "/usr/bin/codesign"}
        if args[0] == "/usr/bin/security":
            assert args[1] in allowed_security_verbs
        if args[1] in {"create-keychain", "unlock-keychain", "import", "set-key-partition-list", "pkcs12"}:
            assert kwargs.get("secrets_to_hide"), "Password-bearing native calls must redact errors"


def test_native_failure_redacts_secrets_and_preserves_useful_error(monkeypatch):
    secret = "unit-test-private-password"

    def failure(args, **kwargs):
        assert kwargs == {"text": True, "capture_output": True, "umask": 0o077}
        return subprocess.CompletedProcess(args, 1, "", f"cannot unlock with {secret}")

    monkeypatch.setattr(signing.subprocess, "run", failure)
    with pytest.raises(RuntimeError) as caught:
        signing._run("/usr/bin/security", "unlock-keychain", "-p", secret, secrets_to_hide=(secret,))
    assert secret not in str(caught.value)
    assert "[redacted]" in str(caught.value)
    assert "security unlock-keychain failed" in str(caught.value)


def test_native_display_can_read_stderr_without_losing_stdout(monkeypatch):
    monkeypatch.setattr(
        signing.subprocess, "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "stdout\n", "signature on stderr\n"),
    )
    assert signing._run("/usr/bin/codesign", "--display") == "stdout\n"
    assert signing._run("/usr/bin/codesign", "--display", include_stderr=True) == "stdout\nsignature on stderr\n"


def test_other_platform_does_not_attempt_native_operations(tmp_path, monkeypatch, native):
    monkeypatch.setattr(signing, "sys", SimpleNamespace(platform="linux"))
    with (
        pytest.raises(RuntimeError, match="requires macOS"),
        signing.local_identity(tmp_path / "identity"),
    ):
        pytest.fail("Local signing was accepted off macOS")
    assert native.calls == []
