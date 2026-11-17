#!/usr/bin/env python3
"""Local, seek-aware Live Translation worker feeding IINA a real SRT track.

Unlike the upstream live-audio overlay, this adapter decodes the local media file
ahead of playback. Media timestamps therefore survive pauses, seeks and speed
changes. Only the authenticated IINA plugin can select a file for processing.
"""

from __future__ import annotations

import argparse
import gc
import hmac
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from live_translation.deterministic import (
    DeterministicTranslator,
    descriptor_hash,
    local_model_identities,
    profile_descriptor,
    runtime_versions,
    seed_asr,
    whisper_decode_options,
)
from live_translation.media_cache import (
    fingerprint_file,
    key_for,
    source_signature,
    validate_chunks,
)
from live_translation.sessions import export_subtitles
from live_translation.text_pipeline import strip_hallucinations

SUPPORT = Path.home() / "Library/Application Support/LiveTranslate"
CHUNK_SECONDS = 30.0
CONTEXT_SECONDS = 1.0
CACHE_VERSION = 4
PAGE_LAYOUT_VERSION = 2
HEARTBEAT_SECONDS = 12.0
LOOKAHEAD_SECONDS = 180.0
MODEL_IDLE_SECONDS = 10.0
MLX_CACHE_BYTES = 128 * 1024 * 1024
ASR_BATCH_SIZE = 4
ASR_BATCH_BUFFER_SECONDS = 60.0
PORT = 18743
MAX_BODY_BYTES = 16_384
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
TRANSLATION_MODEL = "gemma4:12b"


def resolve_executable(name):
    """Use a configured binary or PATH; support non-Homebrew installations."""
    requested = os.environ.get("LT_" + name.upper()) or name
    resolved = shutil.which(str(Path(requested).expanduser()))
    if not resolved:
        raise RuntimeError(f"Cannot find {name}. Install FFmpeg or set LT_{name.upper()}.")
    return resolved


def ollama_version():
    """Fingerprint the running local engine, including CLI/Homebrew installs."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=5) as response:
            body = json.loads(response.read())
        version = body.get("version") if isinstance(body, dict) else None
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Missing Ollama version")
        return version.strip()
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Cannot reach local Ollama. Start Ollama before opening a video."
        ) from error


def boolean(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false", "1", "0"}:
        return value.lower() in {"true", "1"}
    raise ValueError("Boolean fields must be true or false")


def atomic_write(path, text):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def recognized_cues(result, decode_start, core_start, core_end):
    """Own words by midpoint so overlapping decode windows cannot repeat them."""
    words = []
    for segment in result.get("segments", []):
        if float(segment.get("no_speech_prob", 0)) >= 0.7:
            continue
        if float(segment.get("compression_ratio", 0)) > 2.4:
            continue
        for word in segment.get("words", []) or []:
            text = str(word.get("word", "")).strip()
            start = decode_start + float(word.get("start", 0))
            end = decode_start + float(word.get("end", 0))
            if text and core_start <= (start + end) / 2 < core_end:
                words.append((max(core_start, start), min(core_end, end), text))
        # Word alignment occasionally fails on otherwise valid short utterances.
        if not segment.get("words"):
            start = decode_start + float(segment.get("start", 0))
            end = decode_start + float(segment.get("end", 0))
            text = str(segment.get("text", "")).strip()
            if text and core_start <= (start + end) / 2 < core_end:
                words.append((max(core_start, start), min(core_end, end), text))
    # Whisper's segment boundaries are decoder windows, not semantic units.
    # Flatten them before grouping so terms such as "F sharp minor" survive a
    # boundary and are translated together with the sentence that defines them.
    words.sort(key=lambda word: (word[0], word[1]))
    cues = []
    current = []

    def flush():
        if not current:
            return
        source = strip_hallucinations(" ".join(word[2] for word in current)).strip()
        if source:
            start = max(core_start, current[0][0])
            end = min(core_end, max(start + 0.12, current[-1][1]))
            if end > start:
                cues.append({"start": round(start, 3), "end": round(end, 3), "source": source})
        current.clear()

    for word in words:
        if current and (
            word[0] - current[-1][1] >= 0.9
            or word[1] - current[0][0] > 10.0
            or len(" ".join(item[2] for item in current)) + len(word[2]) > 180
        ):
            flush()
        current.append(word)
        if re.search(r"[.!?]$", word[2]):
            flush()
    flush()
    cues.sort(key=lambda item: (item["start"], item["end"]))
    return cues


def translated_pages(cue, translated):
    """Keep a native subtitle readable instead of displaying a whole paragraph."""
    translated = re.sub(r"\s+", " ", translated).strip()
    if not translated:
        raise RuntimeError("The local model returned an empty translation")
    lines = textwrap.wrap(translated, width=46, break_long_words=False, break_on_hyphens=False)
    pages = ["\n".join(lines[i : i + 2]) for i in range(0, len(lines), 2)]
    duration = cue["end"] - cue["start"]
    # Avoid flashing many pages for unexpectedly verbose model output.
    if len(pages) > 1 and duration / len(pages) < 1.0:
        pages = ["\n".join(textwrap.wrap(translated, width=54, break_long_words=False))]
    weights = [len(page) for page in pages]
    total = sum(weights)
    minimum_hold = min(1.2, duration / len(pages))
    distributable = max(0.0, duration - minimum_hold * len(pages))
    result = []
    cursor = cue["start"]
    for index, page in enumerate(pages):
        end = (
            cue["end"]
            if index == len(pages) - 1
            else cursor + minimum_hold + distributable * weights[index] / total
        )
        result.append({**cue, "start": round(cursor, 3), "end": round(end, 3), "translated": page})
        cursor = end
    return result


def reflow_cached_pages(cues):
    """Retimestamp old display pages without repeating recognition/translation."""
    result = []
    group = []

    def flush():
        if not group:
            return
        combined = {**group[0], "end": group[-1]["end"]}
        translated = " ".join(str(page.get("translated", "")) for page in group)
        result.extend(translated_pages(combined, translated))
        group.clear()

    for cue in cues:
        if group and (
            cue.get("source") != group[-1].get("source")
            or abs(float(cue["start"]) - float(group[-1]["end"])) > 0.003
        ):
            flush()
        group.append(cue)
    flush()
    return result


@dataclass
class Media:
    source: Path
    key: str
    duration: float
    json_path: Path
    srt_path: Path
    content_sha256: str = ""
    signature: tuple = ()
    recognized: dict = field(default_factory=dict)
    asr_key: str = ""
    asr_path: Path | None = None
    asr_profile: dict = field(default_factory=dict)
    chunks: dict = field(default_factory=dict)
    revision: int = 0
    error: str | None = None
    retry_at: float = 0.0

    @property
    def chunk_count(self):
        return max(1, math.ceil(self.duration / CHUNK_SECONDS))

    def phrases(self):
        return sorted(
            [cue for values in self.chunks.values() for cue in values],
            key=lambda cue: (cue["start"], cue["end"]),
        )


class Bridge:
    def __init__(self, cache_dir=None, *, start_worker=True, profile=None):
        self.cache_dir = Path(cache_dir) if cache_dir else SUPPORT / "subtitles"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.whisper_model = os.environ.get("LT_WHISPER_MODEL", WHISPER_MODEL)
        self.translation_model = os.environ.get("LT_TRANSLATION_MODEL", TRANSLATION_MODEL)
        self.profile = json.loads(json.dumps(profile)) if profile is not None else None
        self.profile_hash = None
        self._pinned_models = None
        self._fingerprints = {}
        self.condition = threading.Condition(threading.RLock())
        self.sessions = {}
        self.active = None
        self.position = 0.0
        self.enabled = False
        self.paused = False
        self.last_heartbeat = 0.0
        self.generation = 0
        self.processing = False
        self.phase = "idle"
        self.client_diagnostics = {}
        self.closed = False
        self._translator = None
        self._whisper_path = None
        self._whisper_resident = False
        self._translation_resident = False
        self._idle_since = None
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(
                target=self._worker, name="IINA local models", daemon=True
            )
            self.worker.start()

    def _probe(self, path):
        probe = (
            resolve_executable("ffprobe")
            if os.environ.get("LT_FFPROBE")
            else shutil.which("ffprobe")
        )
        if probe:
            process = subprocess.run(
                [
                    probe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                check=True,
                timeout=20,
            )
            duration = float(json.loads(process.stdout)["format"]["duration"])
        else:
            # The verified portable FFmpeg wheel includes the decoder executable
            # but not ffprobe. FFmpeg reports container duration before exiting
            # with its normal "no output file" status, so that status is expected.
            process = subprocess.run(
                [resolve_executable("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path)],
                capture_output=True,
                check=False,
                timeout=20,
            )
            detail = process.stderr.decode(errors="replace")
            match = re.search(r"\bDuration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", detail)
            if not match:
                raise ValueError("The video has no usable duration")
            hours, minutes, seconds = (float(value) for value in match.groups())
            duration = hours * 3600 + minutes * 60 + seconds
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("The video has no usable duration")
        return duration

    def _media(self, value):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Only absolute local media paths are supported")
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("Select a local video file")
        if self.profile is None:
            self._pinned_models = local_model_identities(self.whisper_model, self.translation_model)
            engines = runtime_versions()
            engines["ollama"] = ollama_version()
            engines["ffmpeg"] = subprocess.check_output(
                [resolve_executable("ffmpeg"), "-version"],
                text=True,
                timeout=10,
            ).splitlines()[0]
            self.profile = profile_descriptor(
                self.whisper_model,
                self.translation_model,
                engine_versions=engines,
                **self._pinned_models,
            )
            self.profile["segmentation"] = {
                "version": CACHE_VERSION,
                "chunk_seconds": CHUNK_SECONDS,
                "context_seconds": CONTEXT_SECONDS,
                "page_layout_version": PAGE_LAYOUT_VERSION,
                "cue_gap_seconds": 0.9,
                "cue_max_seconds": 10.0,
                "cue_max_chars": 180,
                "audio_track": "0:a:0",
                "sample_rate": 16000,
                "channels": 1,
            }
        if self.profile_hash is None:
            self.profile_hash = descriptor_hash(self.profile)
        content_sha256, signature = fingerprint_file(path, self._fingerprints)
        key = key_for(content_sha256, self.profile_hash)
        if key in self.sessions:
            media = self.sessions[key]
            media.source = path
            media.signature = signature
            return media
        subtitle_dir = self.cache_dir / key
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        media = Media(
            path,
            key,
            self._probe(path),
            self.cache_dir / f"{key}.json",
            subtitle_dir / "Русский — Live Translation.srt",
            content_sha256=content_sha256,
            signature=signature,
        )
        media.asr_profile = self._recognition_profile()
        media.asr_key = key_for(content_sha256, descriptor_hash(media.asr_profile))
        recognition_dir = self.cache_dir / "recognition"
        recognition_dir.mkdir(parents=True, exist_ok=True)
        media.asr_path = recognition_dir / f"{media.asr_key}.json"
        if media.asr_path.is_file():
            try:
                saved_asr = json.loads(media.asr_path.read_text(encoding="utf-8"))
                if (
                    isinstance(saved_asr, dict)
                    and saved_asr.get("version") == 1
                    and saved_asr.get("key") == media.asr_key
                    and saved_asr.get("source_sha256") == content_sha256
                    and saved_asr.get("profile_sha256") == descriptor_hash(media.asr_profile)
                    and descriptor_hash(saved_asr.get("profile"))
                    == descriptor_hash(media.asr_profile)
                ):
                    media.recognized = validate_chunks(
                        saved_asr.get("chunks"),
                        media.duration,
                        CHUNK_SECONDS,
                        required_fields=("source",),
                    )
            except (ValueError, TypeError, KeyError, OSError):
                media.recognized = {}
        if media.json_path.is_file():
            repair_subtitles = False
            try:
                saved = json.loads(media.json_path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("The video cache must be a JSON object")
                if (
                    saved.get("key") == key
                    and saved.get("version") == CACHE_VERSION
                    and saved.get("source_sha256") == content_sha256
                    and saved.get("profile_sha256") == self.profile_hash
                    and descriptor_hash(saved.get("profile")) == self.profile_hash
                ):
                    media.chunks = validate_chunks(
                        saved.get("chunks"), media.duration, CHUNK_SECONDS
                    )
                    media.revision = int(saved.get("revision", 0))
                    migrate_pages = int(saved.get("page_layout_version", 0)) < PAGE_LAYOUT_VERSION
                    if migrate_pages:
                        reflowed = {
                            index: reflow_cached_pages(cues) for index, cues in media.chunks.items()
                        }
                        if reflowed != media.chunks:
                            media.chunks = reflowed
                            media.revision += 1
                    # JSON is the committed source of truth. Repair SRT after
                    # crashes, manual edits, or a missing subtitle file without
                    # loading either model or changing committed translations.
                    repair_subtitles = True
            except (ValueError, TypeError, KeyError, OSError):
                media.chunks = {}
                media.revision = 0
            if repair_subtitles:
                # A failed repair must surface as an I/O error, never turn
                # valid frozen translations into missing work to regenerate.
                self._save(media)
        self.sessions[key] = media
        return media

    def _recognition_profile(self):
        """Speech work survives translation-only profile and model changes."""
        assert self.profile is not None
        segmentation = dict(self.profile.get("segmentation", {}))
        segmentation.pop("page_layout_version", None)
        if not segmentation:
            segmentation = {
                "version": CACHE_VERSION,
                "chunk_seconds": CHUNK_SECONDS,
                "context_seconds": CONTEXT_SECONDS,
                "cue_gap_seconds": 0.9,
                "cue_max_seconds": 10.0,
                "cue_max_chars": 180,
                "audio_track": "0:a:0",
                "sample_rate": 16000,
                "channels": 1,
            }
        engines = self.profile.get("engine_versions", {})
        return {
            "version": 1,
            "seed": self.profile.get("seed", 42),
            "whisper": self.profile.get(
                "whisper",
                {
                    "model": self.whisper_model,
                    "options": whisper_decode_options(),
                },
            ),
            "segmentation": segmentation,
            "engine_versions": {key: value for key, value in engines.items() if key != "ollama"},
        }

    def update(self, data):
        enabled = boolean(data.get("enabled"), True)
        paused = boolean(data.get("paused"), False)
        position = float(data.get("position", 0))
        if not math.isfinite(position) or position < 0:
            raise ValueError("position must be a finite nonnegative number")
        client_diagnostics: dict[str, bool | int] = {
            name: boolean(data[name])
            for name in ("native_russian_selected", "waiting_for_translation")
            if name in data
        }
        if "seek_generation" in data:
            client_diagnostics["seek_generation"] = int(data["seek_generation"])
        with self.condition:
            if not enabled:
                # A delayed end-file request from the previous video must not
                # stop a newer video selected by another request/window.
                stopped_path = data.get("path")
                if stopped_path and self.active:
                    if not isinstance(stopped_path, str) or not Path(stopped_path).is_absolute():
                        raise ValueError("Only absolute local media paths are supported")
                    if Path(stopped_path).resolve() != self.active.source:
                        return self._response()
                if self.enabled:
                    self.generation += 1
                self.enabled = False
                self.last_heartbeat = time.monotonic()
                self.condition.notify_all()
                return self._response()
            value = data.get("path")
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError("A local media path is required")
            media = self._media(value)
            if self.active is not media or not self.enabled:
                self.generation += 1
            elif abs(position - self.position) > CHUNK_SECONDS or (
                "seek_generation" in client_diagnostics
                and "seek_generation" in self.client_diagnostics
                and client_diagnostics["seek_generation"]
                != self.client_diagnostics["seek_generation"]
            ):
                # Explicit seeks also preempt a batch when the jump is short
                # but crosses into a different, still missing subtitle chunk.
                self.generation += 1
                media.error = None
                media.retry_at = 0
            self.active = media
            self.position = min(position, media.duration)
            self.paused = paused
            self.enabled = True
            self.last_heartbeat = time.monotonic()
            self.client_diagnostics = client_diagnostics
            self.condition.notify_all()
            return self._response()

    def _response(self):
        media = self.active
        if media is None or not self.enabled:
            return {
                "status": "disabled",
                "path": None,
                "revision": 0,
                "buffered_until": 0,
                "done": False,
                "processing": False,
            }
        index = min(int(self.position // CHUNK_SECONDS), media.chunk_count - 1)
        buffered_until = self.position
        while str(index) in media.chunks and index < media.chunk_count:
            buffered_until = min(media.duration, (index + 1) * CHUNK_SECONDS)
            index += 1
        has_cues = media.srt_path.is_file() and bool(media.phrases())
        current_index = min(int(self.position // CHUNK_SECONDS), media.chunk_count - 1)
        current_ready = str(current_index) in media.chunks
        prepared_seconds = sum(
            min(CHUNK_SECONDS, media.duration - int(chunk) * CHUNK_SECONDS)
            for chunk in media.chunks
        )
        prepared_until = 0.0
        while (
            str(int(prepared_until // CHUNK_SECONDS)) in media.chunks
            and prepared_until < media.duration
        ):
            prepared_until = min(media.duration, prepared_until + CHUNK_SECONDS)
        result = {
            "status": "error" if media.error else "ready" if current_ready else "processing",
            "path": str(media.srt_path) if has_cues else None,
            "revision": media.revision,
            "buffered_until": round(buffered_until, 3),
            "done": len(media.chunks) == media.chunk_count,
            "processing": self.processing,
            "phase": self.phase,
            "current_ready": current_ready,
            "completed_chunks": len(media.chunks),
            "total_chunks": media.chunk_count,
            "prepared_percent": round(100 * prepared_seconds / media.duration, 1),
            "prepared_until": round(prepared_until, 3),
            "cache_key": media.key,
        }
        if media.error:
            result["error"] = media.error
        return result

    def _next_chunk(self, media):
        return next(iter(self._missing_chunks(media)), None)

    def _missing_chunks(self, media):
        first = min(int(self.position // CHUNK_SECONDS), media.chunk_count - 1)
        last = min(media.chunk_count, first + math.ceil(LOOKAHEAD_SECONDS / CHUNK_SECONDS))
        nearby = [index for index in range(first, last) if str(index) not in media.chunks]
        # Once the playhead has a useful buffer, fill every other gap in a
        # stable order. Pausing still prepares the entire video for later seeks.
        remaining = [
            index
            for index in range(media.chunk_count)
            if str(index) not in media.chunks and not first <= index < last
        ]
        return nearby + remaining

    def _batch_indices(self, media):
        missing = self._missing_chunks(media)
        first = min(int(self.position // CHUNK_SECONDS), media.chunk_count - 1)
        index = first
        while index < media.chunk_count and str(index) in media.chunks:
            index += 1
        buffered_until = min(media.duration, index * CHUNK_SECONDS)
        ready = str(first) in media.chunks
        limit = (
            ASR_BATCH_SIZE
            if ready and buffered_until - self.position >= ASR_BATCH_BUFFER_SECONDS
            else 1
        )
        return missing[:limit]

    def _decode_audio(self, path, start, seconds):
        import numpy as np

        process = subprocess.run(
            [
                resolve_executable("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-ss",
                str(start),
                "-i",
                str(path),
                "-t",
                str(seconds),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "f32le",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return np.frombuffer(process.stdout, dtype=np.float32).copy()

    def _transcribe(self, audio):
        # File-based operation must not discover/download models during playback.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        if self._whisper_path is None:
            from huggingface_hub import hf_hub_download

            try:
                candidate = Path(self.whisper_model).expanduser()
                if not candidate.is_dir():
                    # Resolving one known cached file avoids requiring README
                    # and repository metadata in a deliberately lean snapshot.
                    candidate = Path(
                        hf_hub_download(
                            repo_id=self.whisper_model,
                            filename="config.json",
                            local_files_only=True,
                        )
                    ).parent
                if not (candidate / "config.json").is_file() or not any(
                    (candidate / name).is_file()
                    for name in ("weights.safetensors", "model.safetensors", "weights.npz")
                ):
                    raise FileNotFoundError("Missing Whisper weights or configuration")
                self._whisper_path = str(candidate)
            except Exception as error:
                raise RuntimeError(
                    "Не найдена локальная модель Whisper. Нужно завершить загрузку модели "
                    f"{self.whisper_model}; интернет во время просмотра не используется."
                ) from error
        import mlx.core as mx
        import mlx_whisper

        # The default MLX allocator may retain most of unified memory after
        # inference. Keep its reusable cache small before loading Whisper.
        mx.set_cache_limit(MLX_CACHE_BYTES)
        seed_asr()
        self._whisper_resident = True
        return mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._whisper_path,
            **whisper_decode_options(),
        )

    def _translate(self, source, history):
        if self._translator is None:
            self._translator = DeterministicTranslator(
                model=self.translation_model,
                target="ru",
                source="en",
                url="http://127.0.0.1:11434",
                max_tokens=256,
                temperature=0.0,
                reasoning=False,
                num_ctx=4096,
            )
        try:
            self._translation_resident = True
            return self._translator.translate(
                source,
                max_tokens=max(256, min(1024, int(len(source) * 1.2) + 96)),
                history=history,
            )
        except RuntimeError as error:
            raise RuntimeError(
                "Локальный переводчик Ollama недоступен или модель не загружена: "
                f"{self.translation_model}. Проверьте, что Ollama запущен и модель установлена."
            ) from error

    def _still_active(self, media, generation):
        with self.condition:
            return (
                not self.closed
                and self.enabled
                and self.active is media
                and generation == self.generation
                and time.monotonic() - self.last_heartbeat <= HEARTBEAT_SECONDS
            )

    def _save(self, media):
        phrases = media.phrases()
        # Save SRT first; the JSON commit is the marker that a revision is durable.
        if phrases or media.srt_path.exists():
            atomic_write(media.srt_path, export_subtitles({"phrases": phrases}) if phrases else "")
        atomic_write(
            media.json_path,
            json.dumps(
                {
                    "version": CACHE_VERSION,
                    "page_layout_version": PAGE_LAYOUT_VERSION,
                    "key": media.key,
                    "source": str(media.source),
                    "duration": media.duration,
                    "revision": media.revision,
                    "chunks": media.chunks,
                    "whisper_model": self.whisper_model,
                    "translation_model": self.translation_model,
                    "source_sha256": media.content_sha256,
                    "profile_sha256": self.profile_hash,
                    "profile": self.profile,
                    "complete": len(media.chunks) == media.chunk_count,
                    "completed_chunks": len(media.chunks),
                    "total_chunks": media.chunk_count,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
        )

    def _check_inputs(self, media):
        if source_signature(media.source) != media.signature:
            raise RuntimeError("Видеофайл изменился во время обработки; результат не сохранён")
        if (
            self._pinned_models is not None
            and local_model_identities(self.whisper_model, self.translation_model)
            != self._pinned_models
        ):
            raise RuntimeError("Модель изменилась во время обработки; результат не сохранён")

    def _save_recognition(self, media):
        atomic_write(
            media.asr_path,
            json.dumps(
                {
                    "version": 1,
                    "key": media.asr_key,
                    "source_sha256": media.content_sha256,
                    "profile_sha256": descriptor_hash(media.asr_profile),
                    "profile": media.asr_profile,
                    "duration": media.duration,
                    "chunks": media.recognized,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
        )

    def _prepare_speech(self, media, index, generation, *, keep_loaded=False):
        """Recognize a fixed chunk once, preserving valid work across seeks."""
        if str(index) in media.recognized:
            return media.recognized[str(index)]
        if not self._still_active(media, generation):
            return None
        self._check_inputs(media)
        core_start = index * CHUNK_SECONDS
        core_end = min(media.duration, core_start + CHUNK_SECONDS)
        decode_start = max(0.0, core_start - CONTEXT_SECONDS)
        decode_end = min(media.duration, core_end + CONTEXT_SECONDS)
        self.phase = "recognizing"
        audio = self._decode_audio(media.source, decode_start, decode_end - decode_start)
        if not self._still_active(media, generation):
            return None
        if len(audio):
            if not self._whisper_resident:
                self.phase = "releasing translation model"
                self._release_translation(force=True)
            try:
                self.phase = "recognizing"
                result = self._transcribe(audio)
            finally:
                if not keep_loaded:
                    self._release_whisper()
        else:
            result = {"segments": []}
        cues = recognized_cues(result, decode_start, core_start, core_end)
        # A seek can finish during ASR. Keep this valid, deterministic speech
        # result, but do not start translating stale work after cancellation.
        with self.condition:
            self._check_inputs(media)
            media.recognized[str(index)] = cues
            try:
                self._save_recognition(media)
            except Exception:
                media.recognized.pop(str(index), None)
                raise
        return cues if self._still_active(media, generation) else None

    def _translate_speech(self, media, index, cues, generation):
        if str(index) in media.chunks or not self._still_active(media, generation):
            return False
        # Keep requests byte-identical to the serial algorithm: same cue
        # order, empty initial history, and only this chunk's previous turns.
        history = []
        translated = []
        self.phase = "translating"
        for cue in cues:
            if not self._still_active(media, generation):
                return False
            text = self._translate(cue["source"], history)
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("The local model returned an empty translation")
            translated.extend(translated_pages(cue, text))
            history = (history + [(cue["source"], text)])[-3:]
        with self.condition:
            if not self._still_active(media, generation):
                return False
            self._check_inputs(media)
            previous_revision = media.revision
            media.chunks[str(index)] = translated
            media.revision += 1
            try:
                self._save(media)
            except Exception:
                media.chunks.pop(str(index), None)
                media.revision = previous_revision
                raise
            media.error = None
            media.retry_at = 0
            self.phase = "ready"
        return True

    def _process(self, media, index, generation):
        """Immediate path: one chunk uses the same request/output contract."""
        self._process_batch(media, [index], generation)

    def _process_batch(self, media, indices, generation):
        started = time.monotonic()
        indices = list(dict.fromkeys(indices))
        pending = [index for index in indices if str(index) not in media.chunks]
        if not pending or not self._still_active(media, generation):
            return
        self._check_inputs(media)
        prepared = []
        try:
            for index in pending:
                if not self._still_active(media, generation):
                    return
                cues = self._prepare_speech(media, index, generation, keep_loaded=True)
                if cues is None:
                    return
                prepared.append((index, cues))
        finally:
            # This handoff is unconditional on success, seek, or ASR failure.
            # Only compact cue dictionaries survive; audio is never queued.
            if self._whisper_resident:
                self._release_whisper()
        completed = []
        for index, cues in prepared:
            if not self._still_active(media, generation):
                break
            if self._translate_speech(media, index, cues, generation):
                completed.append(index)
        if completed:
            elapsed = time.monotonic() - started
            if len(pending) == 1:
                index = completed[0]
                start = index * CHUNK_SECONDS
                end = min(media.duration, start + CHUNK_SECONDS)
                print(
                    f"[iina] {start:.0f}-{end:.0f}s: {len(media.chunks[str(index)])} Russian cues in {elapsed:.2f}s",
                    flush=True,
                )
            else:
                print(
                    f"[iina] phased ASR/translation batch {completed}: {len(completed)} chunks in {elapsed:.2f}s",
                    flush=True,
                )

    def _release_whisper(self):
        if not self._whisper_resident:
            return
        try:
            import mlx.core as mx
            from mlx_whisper.transcribe import ModelHolder

            mx.synchronize()
            ModelHolder.model = None
            ModelHolder.model_path = None
            gc.collect()
            mx.synchronize()
            mx.clear_cache()
            self._whisper_resident = False
        except Exception as error:
            # Fail the handoff instead of loading a second model on top of
            # memory that could not be released.
            raise RuntimeError("Не удалось освободить память модели Whisper") from error

    def _release_translation(self, force=False):
        if not force and not self._translation_resident:
            return
        payload = json.dumps(
            {
                "model": self.translation_model,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
            if body.get("done_reason") not in (None, "unload"):
                raise RuntimeError("Ollama did not confirm model unload")
        except Exception as error:
            raise RuntimeError(
                "Не удалось освободить память локального переводчика. Проверьте, что Ollama запущен."
            ) from error
        self._translation_resident = False

    def _unload(self):
        for release in (self._release_whisper, self._release_translation):
            try:
                release()
            except Exception as error:
                print(f"[iina] model release: {error}", file=sys.stderr, flush=True)

    def _idle_unload_due(self, now, inactive):
        if self._idle_since is None:
            self._idle_since = now
        return inactive or now - self._idle_since >= MODEL_IDLE_SECONDS

    def _worker(self):
        while True:
            with self.condition:
                if self.closed:
                    break
                stale = time.monotonic() - self.last_heartbeat > HEARTBEAT_SECONDS
                media = self.active if self.enabled and not stale else None
                indices = self._batch_indices(media) if media else []
                generation = self.generation
                unload_idle = False
                if media is None or not indices or time.monotonic() < media.retry_at:
                    self.processing = False
                    self.phase = "idle" if media is None else "ready"
                    unload_idle = self._idle_unload_due(time.monotonic(), media is None)
                    idle = True
                else:
                    self.processing = True
                    self._idle_since = None
                    idle = False
            if idle:
                if unload_idle:
                    self._unload()
                with self.condition:
                    self.condition.wait(timeout=1.0)
                continue
            assert media is not None
            try:
                self._process_batch(media, indices, generation)
            except Exception as error:
                with self.condition:
                    if self._still_active(media, generation):
                        if isinstance(error, subprocess.CalledProcessError):
                            detail = (error.stderr or b"").decode(errors="replace")[-700:].strip()
                        else:
                            detail = str(error)
                        media.error = detail or type(error).__name__
                        media.retry_at = time.monotonic() + 20.0
                        self.phase = "error"
                print(f"[iina] generation failed: {error}", file=sys.stderr, flush=True)
            finally:
                with self.condition:
                    self.processing = False
                    self.condition.notify_all()
        self._unload()

    def close(self):
        with self.condition:
            self.closed = True
            self.enabled = False
            self.generation += 1
            self.condition.notify_all()
        if self.worker:
            self.worker.join(timeout=2.0)
        else:
            self._unload()


def server_for(bridge, token, port=PORT):
    if not token or len(token) < 24:
        raise ValueError("An authentication token of at least 24 characters is required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def reply(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                self.reply(
                    200, {"status": "ok", "service": "iina-local-translation", "protocol": 1}
                )
            elif self.path == "/status":
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + token
                ):
                    self.reply(401, {"error": "Authentication required"})
                    return
                with bridge.condition:
                    state = bridge._response()
                    state.update(
                        {
                            "position": bridge.position,
                            "paused": bridge.paused,
                            "source": str(bridge.active.source) if bridge.active else None,
                            "heartbeat_age": round(time.monotonic() - bridge.last_heartbeat, 2),
                            "generation": bridge.generation,
                            "client": dict(bridge.client_diagnostics),
                        }
                    )
                self.reply(200, state)
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/state":
                self.reply(404, {"error": "Not found"})
                return
            origin = self.headers.get("Origin")
            allowed = {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }
            if origin and origin not in allowed:
                self.reply(403, {"error": "Cross-origin requests are not allowed"})
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self.reply(401, {"error": "Authentication required"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > MAX_BODY_BYTES:
                    self.reply(413, {"error": "Invalid request size"})
                    return
                raw = self.rfile.read(size).decode("utf-8")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type == "application/json":
                    data = json.loads(raw)
                elif content_type == "application/x-www-form-urlencoded":
                    data = {key: values[-1] for key, values in parse_qs(raw).items()}
                else:
                    self.reply(415, {"error": "Use JSON or form data"})
                    return
                if not isinstance(data, dict):
                    raise ValueError("The request must be an object")
                self.reply(200, bridge.update(data))
            except (
                ValueError,
                TypeError,
                KeyError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                self.reply(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", type=Path, help="Generate the 30-second chunk at --position and exit"
    )
    parser.add_argument("--position", type=float, default=0.0)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    bridge = Bridge(start_worker=not bool(args.once))
    if args.once:
        try:
            bridge.update(
                {"path": str(args.once.resolve()), "position": args.position, "enabled": True}
            )
            # The command itself is the lease; it has no polling plugin attached.
            bridge.last_heartbeat = math.inf
            index = bridge._next_chunk(bridge.active)
            if index is not None:
                bridge._process(bridge.active, index, bridge.generation)
            print(json.dumps(bridge._response(), ensure_ascii=False))
        except Exception as error:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
            return 1
        finally:
            bridge.close()
        return 0
    try:
        token = (SUPPORT / "iina-token").read_text(encoding="utf-8").strip()
        server = server_for(bridge, token, args.port)
    except (ValueError, OSError) as error:
        bridge.close()
        print(f"[iina] startup failed: {error}", file=sys.stderr)
        return 1

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"[iina] local subtitle bridge listening on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        bridge.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
