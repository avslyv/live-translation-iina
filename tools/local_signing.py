"""Persistent local certificate signing, without changing macOS trust settings.

This is the same local-build identity pattern used by codex-switcher. The private
key stays in a dedicated keychain; only its public certificate enters an app's
signature. Developer ID signing and Apple notarization remain separate modes.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

COMMON_NAME = "Live Translation Local Build"
LOCK_ROOT = Path("/tmp") / f"live-translation-signing-{os.getuid()}"


def _run(*args, secrets_to_hide=(), include_stderr=False):
    result = subprocess.run(args, text=True, capture_output=True, umask=0o077)
    if result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        for secret in secrets_to_hide:
            if secret:
                detail = detail.replace(secret, "[redacted]")
        raise RuntimeError(f"{Path(args[0]).name} {args[1]} failed: {detail}")
    return result.stdout + (result.stderr if include_stderr else "")


def _private(path, *, directory=False):
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    private_mode = (
        stat.S_IMODE(info.st_mode) == 0o700 if directory else not stat.S_IMODE(info.st_mode) & 0o077
    )
    if not correct_type or info.st_uid != os.getuid() or not private_mode:
        raise RuntimeError(
            f"Signing {'directory' if directory else 'file'} must be private and owned by you: {path}"
        )


@contextmanager
def _serialized():
    LOCK_ROOT.mkdir(mode=0o700, exist_ok=True)
    _private(LOCK_ROOT, directory=True)
    descriptor = os.open(LOCK_ROOT / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        info = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise RuntimeError("The local signing lock is not private")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def _restore_search_list():
    original = shlex.split(_run("/usr/bin/security", "list-keychains", "-d", "user"))
    handlers = {}
    if threading.current_thread() is threading.main_thread():

        def interrupted(signum, _frame):
            raise SystemExit(128 + signum)

        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, interrupted)
    try:
        yield original
    finally:
        try:
            _run("/usr/bin/security", "list-keychains", "-d", "user", "-s", *original)
            restored = shlex.split(_run("/usr/bin/security", "list-keychains", "-d", "user"))
            if restored != original:
                raise RuntimeError("Could not restore the original keychain search list")
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


@dataclass(frozen=True)
class LocalIdentity:
    fingerprint: str
    certificate_sha256: str
    keychain: Path


def _create(directory):
    directory.mkdir(mode=0o700)
    password = secrets.token_hex(32)
    password_file = directory / "password"
    keychain, certificate = directory / "identity.keychain-db", directory / "certificate.pem"
    try:
        descriptor = os.open(
            password_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(password + "\n")
        with tempfile.TemporaryDirectory(prefix="identity-", dir=directory) as temporary:
            temporary = Path(temporary)
            config = temporary / "openssl.cnf"
            config.write_text(f"""[req]
distinguished_name = subject
prompt = no
x509_extensions = codesign
[subject]
CN = {COMMON_NAME}
[codesign]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
""")
            private_key, archive = temporary / "key.pem", temporary / "identity.p12"
            _run(
                "/usr/bin/openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-x509",
                "-nodes",
                "-days",
                "3650",
                "-keyout",
                str(private_key),
                "-out",
                str(certificate),
                "-config",
                str(config),
            )
            certificate.chmod(0o600)
            _run(
                "/usr/bin/openssl",
                "pkcs12",
                "-export",
                "-inkey",
                str(private_key),
                "-in",
                str(certificate),
                "-out",
                str(archive),
                "-passout",
                "pass:" + password,
                secrets_to_hide=(password,),
            )
            _run(
                "/usr/bin/security",
                "create-keychain",
                "-p",
                password,
                str(keychain),
                secrets_to_hide=(password,),
            )
            _run(
                "/usr/bin/security",
                "unlock-keychain",
                "-p",
                password,
                str(keychain),
                secrets_to_hide=(password,),
            )
            _run(
                "/usr/bin/security",
                "import",
                str(archive),
                "-k",
                str(keychain),
                "-P",
                password,
                "-x",
                "-T",
                "/usr/bin/codesign",
                secrets_to_hide=(password,),
            )
            _run(
                "/usr/bin/security",
                "set-key-partition-list",
                "-S",
                "apple-tool:,apple:,codesign:",
                "-s",
                "-k",
                password,
                str(keychain),
                secrets_to_hide=(password,),
            )
    except BaseException:
        if keychain.exists():
            with suppress(RuntimeError):
                _run(
                    "/usr/bin/security",
                    "delete-keychain",
                    str(keychain),
                    secrets_to_hide=(password,),
                )
        shutil.rmtree(directory)
        raise


def _fingerprint(certificate, algorithm, digits):
    value = _run(
        "/usr/bin/openssl", "x509", "-in", str(certificate), "-noout", "-fingerprint", algorithm
    )
    value = value.strip().split("=")[-1].replace(":", "").upper()
    if not re.fullmatch(rf"[A-F0-9]{{{digits}}}", value):
        raise RuntimeError("Could not identify the local signing certificate")
    return value


@contextmanager
def local_identity(directory=None):
    if sys.platform != "darwin":
        raise RuntimeError("Local certificate signing requires macOS")
    directory = (
        Path(
            directory
            or os.environ.get("LT_SIGNING_DIR")
            or Path.home() / "Library/Application Support/LiveTranslate/build-signing"
        )
        .expanduser()
        .absolute()
    )
    with _serialized(), _restore_search_list() as original:
        directory.parent.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise RuntimeError("The local signing directory cannot be a symlink")
        if not directory.exists():
            _create(directory)
        _private(directory, directory=True)
        keychain, password_file, certificate = (
            directory / name for name in ("identity.keychain-db", "password", "certificate.pem")
        )
        if not all(path.exists() for path in (keychain, password_file, certificate)):
            raise RuntimeError(
                "Local signing identity is incomplete; restore its original keychain, password and certificate before rebuilding"
            )
        for path in (keychain, password_file, certificate):
            _private(path)
        password = password_file.read_text().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", password):
            raise RuntimeError("The local signing keychain password file is invalid")
        _run("/usr/bin/openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "0")
        fingerprint = _fingerprint(certificate, "-sha1", 40)
        sha256 = _fingerprint(certificate, "-sha256", 64)
        _run(
            "/usr/bin/security",
            "unlock-keychain",
            "-p",
            password,
            str(keychain),
            secrets_to_hide=(password,),
        )
        # No trust settings are installed. This temporary search entry is needed by codesign.
        _run("/usr/bin/security", "list-keychains", "-d", "user", "-s", *original, str(keychain))
        yield LocalIdentity(fingerprint, sha256, keychain)


def sign_local(bundle, directory=None, *, hardened=True):
    bundle = Path(bundle).resolve(strict=True)
    if not (
        (bundle.is_dir() and bundle.suffix == ".app")
        or (bundle.is_file() and bundle.suffix == ".dmg")
    ):
        raise ValueError("Select an application bundle or disk image")
    with local_identity(directory) as identity:
        arguments = [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            identity.fingerprint,
            "--keychain",
            str(identity.keychain),
            "--timestamp=none",
        ]
        if hardened:
            arguments.extend(("--options", "runtime"))
        _run(*arguments, str(bundle))
        _run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle))
        details = _run(
            "/usr/bin/codesign", "--display", "--verbose=4", str(bundle), include_stderr=True
        )
        if f"Authority={COMMON_NAME}\n" not in details or (
            hardened and not re.search(r"flags=0x[0-9a-fA-F]+\([^\n]*runtime", details)
        ):
            raise RuntimeError("The application lacks the expected local certificate signature")
        requirement = _run(
            "/usr/bin/codesign", "--display", "-r-", str(bundle), include_stderr=True
        )
        match = re.search(r"^designated => (.+)$", requirement, re.MULTILINE)
        if bundle.suffix == ".app" and not match:
            raise RuntimeError("The signed app has no designated requirement")
        return {
            "mode": "local-certificate",
            "authority": COMMON_NAME,
            "certificate_sha1": identity.fingerprint,
            "certificate_sha256": identity.certificate_sha256,
            "designated_requirement": match.group(1) if match else None,
        }


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--directory", type=Path, help="persistent private local signing directory")
    args = parser.parse_args(argv)
    try:
        result = sign_local(args.bundle, args.directory, hardened=args.bundle.suffix == ".app")
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Local signing failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
