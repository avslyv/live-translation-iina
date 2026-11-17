"""Detect completed runtime updates without mixing old and new worker code."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REVISION_MARKER = "runtime-revision.json"
_REVISION = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class _Snapshot:
    revision: str
    signature: tuple


@dataclass(frozen=True)
class _Marker:
    state: str
    revision: str | None
    signature: tuple


def _stat_signature(path: Path) -> tuple:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _source_files(root: Path) -> list[Path]:
    required = (
        root / "iina_bridge.py",
        root / "live_translation/__init__.py",
        root / "requirements-iina.txt",
    )
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f"Incomplete runtime: {path.relative_to(root)}")
    paths = {root / "iina_bridge.py", *root.glob("requirements-iina*.txt")}
    paths.update(
        path
        for path in (root / "live_translation").rglob("*.py")
        if not any(
            part in {"__pycache__", ".venv", ".git"} for part in path.relative_to(root).parts
        )
    )
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _source_signature(root: Path, paths: list[Path]) -> tuple:
    return tuple((path.relative_to(root).as_posix(), *_stat_signature(path)) for path in paths)


def _snapshot(root: Path, *, validate: bool = False) -> _Snapshot:
    paths = _source_files(root)
    before = _source_signature(root, paths)
    digest = hashlib.sha256(b"live-translation-runtime-v1\0")
    for path in paths:
        name = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if validate and path.suffix == ".py":
            compile(content, name, "exec", dont_inherit=True)
        for value in (name.encode("utf-8"), content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    if before != _source_signature(root, _source_files(root)):
        raise OSError("Runtime files changed during inspection")
    return _Snapshot(digest.hexdigest(), before)


def runtime_revision(root: Path) -> str:
    """Hash worker/library/dependency contents, independent of paths and mtimes."""
    return _snapshot(Path(root)).revision


def _write_marker(root: Path, payload: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-revision-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / REVISION_MARKER)
    finally:
        Path(temporary).unlink(missing_ok=True)


def begin_runtime_update(root: Path) -> None:
    """Suspend automatic restarts before an installer replaces runtime files."""
    _write_marker(Path(root), {"schema": 1, "state": "updating"})


def finish_runtime_update(root: Path) -> str:
    """Publish a complete, syntax-valid runtime after the installer's final copy."""
    root = Path(root)
    revision = _snapshot(root, validate=True).revision
    _write_marker(root, {"schema": 1, "state": "ready", "revision": revision})
    return revision


def _read_marker(root: Path) -> _Marker | None:
    path = root / REVISION_MARKER
    try:
        before = _stat_signature(path)
    except FileNotFoundError:
        return None
    if before[2] > 4096:
        raise ValueError("Invalid runtime revision marker")
    raw = path.read_bytes()
    after = _stat_signature(path)
    if before != after:
        raise OSError("Runtime revision marker changed during inspection")
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data.get("schema") != 1:
        raise ValueError("Invalid runtime revision marker")
    state = data.get("state")
    revision = data.get("revision")
    if not isinstance(state, str) or state not in {"updating", "ready"}:
        raise ValueError("Invalid runtime revision state")
    if state == "ready" and (not isinstance(revision, str) or not _REVISION.fullmatch(revision)):
        raise ValueError("Invalid runtime revision identity")
    return _Marker(
        state, revision if state == "ready" else None, (*after, hashlib.sha256(raw).digest())
    )


def runtime_update_pending(root: Path) -> bool:
    """Keep recovery clients from restarting an unfinished installer transaction."""
    try:
        marker = _read_marker(Path(root))
    except (OSError, ValueError, UnicodeError):
        return True
    return marker is not None and marker.state == "updating"


class RuntimeWatcher:
    """Notify once after a complete changed tree remains stable for the debounce.

    Installers publish ``updating`` before copying and a matching ``ready`` marker
    last. An unchanged ready marker permits direct source/git updates as well.
    The caller shuts down its HTTP server and exits; launchd KeepAlive starts a
    fresh interpreter. This class neither reloads Python modules nor starts jobs.
    """

    def __init__(
        self,
        root: Path,
        on_change: Callable[[str], None],
        loaded_revision: str | None = None,
        poll_interval: float = 0.5,
        settle_seconds: float = 2,
    ):
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive and finite")
        if not math.isfinite(settle_seconds) or settle_seconds < 0:
            raise ValueError("settle_seconds must be nonnegative and finite")
        self.root = Path(root)
        self.on_change = on_change
        self.loaded_revision = (
            runtime_revision(self.root) if loaded_revision is None else loaded_revision
        )
        if not _REVISION.fullmatch(self.loaded_revision):
            raise ValueError("Invalid loaded runtime revision")
        self.poll_interval = poll_interval
        self.settle_seconds = settle_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notified = False
        self._candidate: tuple | None = None
        self._candidate_since = 0.0
        self._cached: _Snapshot | None = None
        try:
            self._baseline_marker = _read_marker(self.root)
        except (OSError, ValueError, UnicodeError):
            self._baseline_marker = None

    def poll(self, now: float | None = None) -> bool:
        """Inspect once; return True only when the one-shot callback is invoked."""
        if self._notified or self._stop.is_set():
            return False
        try:
            marker = _read_marker(self.root)
            if marker is not None and marker.state == "updating":
                self._candidate = None
                return False
            paths = _source_files(self.root)
            signature = _source_signature(self.root, paths)
            if self._cached is None or self._cached.signature != signature:
                self._cached = _snapshot(self.root, validate=True)
            revision = self._cached.revision
            if revision == self.loaded_revision:
                if marker is not None and marker.revision == revision:
                    self._baseline_marker = marker
                self._candidate = None
                return False
            if (
                marker is not None
                and marker != self._baseline_marker
                and marker.revision != revision
            ):
                self._candidate = None
                return False
            # Stat changes to identical bytes also restart the settling period.
            candidate = (revision, self._cached.signature, marker)
            current_time = time.monotonic() if now is None else now
            if candidate != self._candidate:
                self._candidate = candidate
                self._candidate_since = current_time
            if current_time - self._candidate_since < self.settle_seconds:
                return False
        except (OSError, ValueError, UnicodeError, SyntaxError):
            self._candidate = None
            return False
        self._notified = True
        self._stop.set()
        self.on_change(revision)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.poll():
                return
            self._stop.wait(self.poll_interval)

    def start(self) -> RuntimeWatcher:
        if self._thread is not None:
            return self
        if self._stop.is_set():
            raise RuntimeError("Runtime watcher is already closed")
        self._thread = threading.Thread(target=self._run, name="runtime-update-watch", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.poll_interval + 1)
