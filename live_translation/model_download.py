"""Verified official-registry model downloads for restricted DNS environments.

This fallback accepts model names, never caller-supplied URLs. Every HTTP hop
must use HTTPS and a pinned official registry/CDN hostname. TLS verification is
unchanged. Artifacts become model blobs only after exact size and SHA-256 checks;
the manifest is committed last. Signed CDN URLs never enter progress or errors.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REGISTRY = "registry.ollama.ai"
OFFICIAL_HOSTS = frozenset({REGISTRY, "dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com"})
CHUNK_BYTES = 32 * 1024 * 1024
WORKERS = 4
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 6
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
Emitter = Callable[[dict], None]


def _model_parts(model: str) -> tuple[str, str, str]:
    if not isinstance(model, str) or "://" in model:
        raise ValueError("Use an official Ollama model name, not a URL")
    if model.startswith(REGISTRY + "/"):
        model = model[len(REGISTRY) + 1 :]
    parts = model.split("/")
    if len(parts) == 1:
        parts.insert(0, "library")
    if len(parts) != 2:
        raise ValueError("Use a model name or namespace/model name")
    namespace, tagged = parts
    name, separator, tag = tagged.partition(":")
    tag = tag if separator else "latest"
    if any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", part)
        for part in (namespace, name, tag)
    ):
        raise ValueError("Invalid official model name")
    return namespace, name, tag


def _validate_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise RuntimeError("Invalid official download URL") from error
    if (
        any(ord(character) <= 32 or ord(character) == 127 for character in url)
        or parsed.scheme != "https"
        or parsed.hostname not in OFFICIAL_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError("The model registry returned an unapproved download destination")
    return url


def _curl() -> list[str]:
    # Deliberately no --location/-L: redirects are checked before the next call.
    return [
        "/usr/bin/curl",
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--connect-timeout",
        "25",
    ]


def _headers(raw: str) -> tuple[int, dict[str, str]]:
    blocks = re.split(r"(?m)(?=^HTTP/)", raw.replace("\r\n", "\n"))
    final = next((block for block in reversed(blocks) if block.startswith("HTTP/")), "")
    status = re.match(r"HTTP/\S+\s+(\d+)", final)
    values = {}
    for line in final.splitlines()[1:]:
        name, separator, value = line.partition(":")
        if separator:
            values[name.lower().strip()] = value.strip()
    return int(status[1]) if status else 0, values


def _resolve_url(url: str, *, head: bool = False) -> str:
    seen = set()
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(url)
        if urlsplit(url).hostname != REGISTRY:
            # Registry signatures can authorize GET but not HEAD. The CDN is
            # already allowlisted; data requests never follow further redirects.
            return url
        if url in seen:
            raise RuntimeError("The official model download has a redirect loop")
        seen.add(url)
        command = _curl() + [
            "--range",
            "0-0",
            "--max-filesize",
            "1",
            "--dump-header",
            "-",
            "--output",
            "/dev/null",
            "--max-time",
            "45",
            url,
        ]
        if head:
            command = _curl() + ["--head", "--max-time", "45", url]
        result = None
        for attempt in range(3):
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode in (0, 63):
                break
            if attempt < 2:
                time.sleep(1)
        assert result is not None
        status, headers = _headers(result.stdout)
        # The registry can answer HEAD with 200 while GET redirects, and an R2
        # signature can be method-specific. Probe GET headers with a one-byte
        # body cap; curl 63 means that cap stopped an otherwise valid response.
        if result.returncode not in (0, 63):
            raise RuntimeError(f"Official model request failed (curl {result.returncode})")
        if status in {301, 302, 303, 307, 308}:
            location = headers.get("location")
            if not location:
                raise RuntimeError("The official model redirect is missing its destination")
            url = _validate_url(urljoin(url, location))
            continue
        if 200 <= status < 300:
            return url
        raise RuntimeError(f"The official model registry returned HTTP {status}")
    raise RuntimeError("Too many official model download redirects")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic(path: Path, data: bytes):
    temporary = path.with_name(path.name + ".live-translation-tmp")
    if temporary.is_symlink():
        raise RuntimeError("Refusing a symlink in model download state")
    with os.fdopen(
        os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "wb"
    ) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _manifest(url: str) -> tuple[bytes, list[dict]]:
    resolved = _resolve_url(url, head=True)
    result = subprocess.run(
        _curl()
        + [
            "--retry",
            "3",
            "--retry-all-errors",
            "--retry-delay",
            "1",
            "--max-time",
            "60",
            "--max-filesize",
            str(MAX_MANIFEST_BYTES),
            "--header",
            "Accept: application/vnd.docker.distribution.manifest.v2+json",
            resolved,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Official model manifest request failed (curl {result.returncode})")
    raw = result.stdout
    if not 0 < len(raw) <= MAX_MANIFEST_BYTES:
        raise RuntimeError("The official model manifest has an invalid size")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("layers"), list):
            raise ValueError("Missing layers")
        artifacts = body["layers"] + [body["config"]]
        if not 1 <= len(artifacts) <= 128:
            raise ValueError("Invalid artifact count")
        unique = {}
        for item in artifacts:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("digest"), str)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", item["digest"])
                or isinstance(item.get("size"), bool)
                or not isinstance(item.get("size"), int)
                or not 0 <= item["size"] <= 64 * 1024**3
            ):
                raise ValueError("Invalid artifact")
            if item["digest"] in unique and unique[item["digest"]]["size"] != item["size"]:
                raise ValueError("Inconsistent artifact size")
            unique[item["digest"]] = {"digest": item["digest"], "size": item["size"]}
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("The official model manifest is invalid") from error
    return raw, list(unique.values())


def _valid_range(raw: str, start: int, end: int, total: int) -> bool:
    status, values = _headers(raw)
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", values.get("content-range", ""))
    return bool(status == 206 and match and tuple(map(int, match.groups())) == (start, end, total))


def _download_range(
    url: str, start: int, end: int, total: int, work: Path, cancelled: threading.Event
) -> Path:
    part = work / f"range-{start}-{end}.part"
    tail = part.with_suffix(".tail")
    headers = part.with_suffix(".headers")
    for path in (part, tail, headers):
        if path.is_symlink():
            raise RuntimeError("Refusing a symlink in model download state")
    length = end - start + 1
    if part.exists() and part.stat().st_size > length:
        part.unlink()
    for attempt in range(MAX_ATTEMPTS):
        if cancelled.is_set():
            raise InterruptedError("Model download cancelled")
        received = part.stat().st_size if part.exists() else 0
        if received == length:
            return part
        first = start + received
        remaining = end - first + 1
        for path in (tail, headers):
            with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "wb"):
                pass
        result = subprocess.run(
            _curl()
            + [
                "--max-time",
                "180",
                "--speed-limit",
                "1024",
                "--speed-time",
                "30",
                "--range",
                f"{first}-{end}",
                "--max-filesize",
                str(remaining),
                "--dump-header",
                str(headers),
                "--output",
                str(tail),
                _validate_url(url),
            ],
            capture_output=True,
            check=False,
        )
        count = tail.stat().st_size if tail.exists() else 0
        raw_headers = headers.read_text(errors="replace") if headers.exists() else ""
        status, _ = _headers(raw_headers)
        complete_body = status == 200 and first == 0 and end == total - 1 and count == total
        if (
            _valid_range(raw_headers, first, end, total) or complete_body
        ) and 0 < count <= remaining:
            with part.open("ab") as output, tail.open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
            part.chmod(0o600)
        tail.unlink(missing_ok=True)
        headers.unlink(missing_ok=True)
        if part.exists() and part.stat().st_size == length:
            return part
        if cancelled.wait(min(1 + attempt, 5)):
            raise InterruptedError("Model download cancelled")
        # Curl errors and ignored/wrong ranges are retried without trusting data.
        _ = result.returncode
    raise RuntimeError("Official model range download failed; retry to resume verified progress")


def _fetch_blob(
    base: str,
    item: dict,
    blobs: Path,
    work_root: Path,
    notify: Callable[[int], None],
    cancelled: threading.Event,
):
    expected = item["digest"].split(":", 1)[1]
    size = item["size"]
    target = blobs / item["digest"].replace(":", "-")
    if target.is_file() and target.stat().st_size == size and _sha256(target) == expected:
        notify(size)
        return
    partial = target.with_name(target.name + ".live-translate-partial")
    if partial.is_symlink():
        raise RuntimeError("Refusing a symlink in model download state")
    work = work_root / expected
    if work.is_symlink():
        raise RuntimeError("Refusing a symlink in model download state")
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o700)
    state = work / "progress.json"
    descriptor = os.open(partial, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state.exists():
            saved = json.loads(state.read_text())
            if (
                saved.get("digest") != expected
                or saved.get("size") != size
                or saved.get("chunk_bytes") != CHUNK_BYTES
            ):
                raise RuntimeError("Model download resume state does not match the artifact")
        else:
            prefix = os.fstat(descriptor).st_size
            if prefix > size:
                os.ftruncate(descriptor, 0)
                prefix = 0
            saved = {
                "digest": expected,
                "size": size,
                "chunk_bytes": CHUNK_BYTES,
                "prefix": prefix,
                "completed": [],
            }
        prefix = saved["prefix"]
        if isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 <= prefix <= size:
            raise RuntimeError("Invalid model download resume prefix")
        ranges = [
            (start, min(start + CHUNK_BYTES, size) - 1)
            for start in range(prefix, size, CHUNK_BYTES)
        ]
        if not isinstance(saved.get("completed"), list):
            raise RuntimeError("Invalid model download resume state")
        completed = set(saved["completed"])
        if any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(ranges)
            or os.fstat(descriptor).st_size <= ranges[index][1]
            for index in completed
        ):
            raise RuntimeError("Invalid model download completed ranges")
        progress_lock = threading.Lock()

        def save():
            saved["completed"] = sorted(completed)
            _atomic(state, json.dumps(saved, sort_keys=True).encode())

        save()
        done = prefix + sum(ranges[index][1] - ranges[index][0] + 1 for index in completed)
        notify(done)
        url = _resolve_url(base + "/blobs/" + item["digest"]) if ranges else ""

        def worker(index: int):
            first, last = ranges[index]
            part = _download_range(url, first, last, size, work, cancelled)
            offset = first
            with part.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    view = memoryview(block)
                    while view:
                        written = os.pwrite(descriptor, view, offset)
                        if written <= 0:
                            raise RuntimeError("Cannot write local model download")
                        offset += written
                        view = view[written:]
            with progress_lock:
                os.fsync(descriptor)
                completed.add(index)
                save()
                notify(prefix + sum(ranges[i][1] - ranges[i][0] + 1 for i in completed))
            part.unlink(missing_ok=True)

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
        try:
            futures = [pool.submit(worker, i) for i in range(len(ranges)) if i not in completed]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        except BaseException:
            cancelled.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if partial.stat().st_size != size or _sha256(partial) != expected:
        partial.unlink(missing_ok=True)
        shutil.rmtree(work)
        raise RuntimeError("Model artifact checksum did not match; retry to fetch a clean copy")
    partial.replace(target)
    shutil.rmtree(work)


def download_ollama_model(model: str, root: Path, emit: Emitter) -> Path:
    """Download an official model into an Ollama store, committing the manifest last."""
    namespace, name, tag = _model_parts(model)
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise ValueError("The Ollama model store must be an absolute path")
    base = f"https://{REGISTRY}/v2/{namespace}/{name}"
    raw, artifacts = _manifest(base + "/manifests/" + tag)
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    work_root = root / ".live-translation-downloads"
    if work_root.is_symlink():
        raise RuntimeError("Refusing a symlink in model download state")
    work_root.mkdir(parents=True, exist_ok=True)
    work_root.chmod(0o700)
    total = sum(item["size"] for item in artifacts)
    progress = 0
    callback_lock = threading.Lock()
    cancelled = threading.Event()
    for item in artifacts:
        last_done = 0

        def notify(done: int):
            nonlocal progress, last_done
            with callback_lock:
                progress += done - last_done
                last_done = done
                emit(
                    {
                        "type": "progress",
                        "stage": "translation-model",
                        "label": "Downloading verified local model",
                        "detail": f"{progress / 1e9:.2f} / {total / 1e9:.2f} GB",
                        "progress": 0.5 + 0.2 * progress / max(total, 1),
                    }
                )

        _fetch_blob(base, item, blobs, work_root, notify, cancelled)
    manifest = root / "manifests" / REGISTRY / namespace / name / tag
    manifest.parent.mkdir(parents=True, exist_ok=True)
    _atomic(manifest, raw)
    emit({"type": "log", "message": "Official local model artifacts and manifest verified."})
    return manifest
