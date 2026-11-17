"""Content identity and validation for durable, per-video subtitle caches."""

from __future__ import annotations

import hashlib
import json
import math
import stat
from pathlib import Path

HASH_BLOCK_BYTES = 8 * 1024 * 1024
TIMESTAMP_EPSILON = 0.003


class SourceChangedError(RuntimeError):
    """The source changed while its content identity was being calculated."""


def source_signature(path):
    """Cheap mutation signature; content identity itself never includes the path."""
    metadata = Path(path).stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Select a regular local media file")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(HASH_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint_file(path, memo):
    """Return ``(content_sha256, signature)`` without re-reading unchanged files.

    ``memo`` belongs to the caller and maps stat-signature tuples to full
    SHA-256 values. A rename/copy can be hashed again, but identical bytes still
    select exactly the same video cache.
    """
    before = source_signature(path)
    if before in memo:
        return memo[before], before
    digest = _sha256_file(path)
    after = source_signature(path)
    if after != before:
        raise SourceChangedError(
            "Видеофайл изменился во время вычисления хеша; повторите открытие."
        )
    memo[before] = digest
    return digest, before


def key_for(content_sha256, profile_hash):
    """Stable cache-directory key for video content and generation profile."""
    if not isinstance(content_sha256, str) or not content_sha256:
        raise ValueError("A video content hash is required")
    if not isinstance(profile_hash, str) or not profile_hash:
        raise ValueError("A generation profile hash is required")
    identity = json.dumps(
        {"content_sha256": content_sha256, "profile_hash": profile_hash},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("A timestamp must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("A timestamp must be finite")
    return value


def validate_chunks(
    raw,
    duration,
    chunk_seconds,
    required_fields=("source", "translated"),
    *,
    allow_cross_boundary=False,
):
    """Keep only complete, valid chunks; invalid chunks can be regenerated.

    Empty lists are completed silence. One malformed cue invalidates its whole
    chunk, so a damaged cache never silently drops speech. Valid overlapping
    cues are retained; their canonical ordering uses media timestamps.
    """
    duration, chunk_seconds = _timestamp(duration), _timestamp(chunk_seconds)
    if duration <= 0 or chunk_seconds <= 0:
        raise ValueError("Video and chunk durations must be positive")
    if not isinstance(raw, dict):
        return {}
    count = math.ceil(duration / chunk_seconds)
    valid = {}
    for key, cues in raw.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdigit()
            or len(key) > len(str(count - 1))
        ):
            continue
        # Reject aliases such as 00, +0 and whitespace, as well as out-of-range
        # IDs, instead of allowing multiple IDs for one completed interval.
        index = int(key)
        if str(index) != key or not 0 <= index < count or not isinstance(cues, list):
            continue
        core_start = index * chunk_seconds
        core_end = min(duration, core_start + chunk_seconds)
        checked = []
        try:
            for cue in cues:
                if not isinstance(cue, dict):
                    raise ValueError("A subtitle cue must be an object")
                start, end = _timestamp(cue.get("start")), _timestamp(cue.get("end"))
                valid_time = (
                    core_start - TIMESTAMP_EPSILON <= start < end <= core_end + TIMESTAMP_EPSILON
                )
                if allow_cross_boundary:
                    owner = _timestamp(cue.get("owner_start", start))
                    valid_time = (
                        core_start - TIMESTAMP_EPSILON <= owner < core_end
                        and owner <= start < end <= duration + TIMESTAMP_EPSILON
                    )
                if not valid_time:
                    raise ValueError("A subtitle cue lies outside its chunk")
                if any(
                    not isinstance(cue.get(field), str) or not cue[field].strip()
                    for field in required_fields
                ):
                    raise ValueError("Required subtitle text is missing")
                start, end = (
                    max(core_start, start),
                    min(duration if allow_cross_boundary else core_end, end),
                )
                if end <= start:
                    raise ValueError("A subtitle cue has no duration")
                checked.append({**cue, "start": start, "end": end})
        except (TypeError, ValueError):
            continue
        valid[key] = sorted(checked, key=lambda cue: (cue["start"], cue["end"]))
    return valid
