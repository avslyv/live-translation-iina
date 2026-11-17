"""Native IINA source, settings and cache lifecycle; heavy work stays off HTTP threads."""

from __future__ import annotations

import copy
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path

from .deterministic import descriptor_hash, local_translation_identity
from .local_http import local_urlopen
from .media_cache import fingerprint_file, validate_chunks

LANGUAGES = {
    "en": "English",
    "ru": "Русский",
    "es": "Español",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
    "pt": "Português",
    "ja": "日本語",
    "ko": "한국어",
    "zh": "中文",
    "uk": "Українська",
}
KEY = re.compile(r"[a-f0-9]{32}\Z")
GENERATION_SETTINGS = (
    "translation_model",
    "source_language",
    "target_language",
    "vad_mode",
    "batch_size",
    "glossary",
)


def json_object(value, name):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def defaults(model):
    return {
        "translation_model": model,
        "source_language": "en",
        "target_language": "ru",
        "source_mode": "auto",
        "bilingual": False,
        "wait_for_translation": True,
        "vad_mode": "off",
        "batch_size": 4,
        "glossary": [],
    }


def checked_settings(values, previous):
    from .translation_quality import validate_glossary

    if not isinstance(values, dict) or set(values) - set(previous):
        raise ValueError("Неизвестные настройки перевода")
    result = {**previous, **values}
    for name in ("source_language", "target_language"):
        if result[name] not in LANGUAGES:
            raise ValueError("Выберите поддерживаемый язык")
    model = result["translation_model"]
    if (
        not isinstance(model, str)
        or len(model) > 200
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", model)
    ):
        raise ValueError("Некорректное имя локальной модели")
    if result["source_mode"] not in {"auto", "audio", "subtitles"}:
        raise ValueError("Выберите источник перевода")
    if result["vad_mode"] not in {"off", "conservative"}:
        raise ValueError("Некорректный режим определения тишины")
    if type(result["batch_size"]) is not int or result["batch_size"] not in {1, 4}:
        raise ValueError("Размер пакета должен быть 1 или 4")
    for name in ("bilingual", "wait_for_translation"):
        if type(result[name]) is not bool:
            raise ValueError("Переключатель должен быть включён или выключен")
    result["glossary"] = validate_glossary(result["glossary"])
    return result


class NativeFeatures:
    """Mixed into Bridge. One model thread owns initialization and inference."""

    def _features_init(self, asynchronous):
        self._asynchronous = asynchronous
        self._pending_request = None
        self._request_signature = None
        self._selection = {}
        self._initialization_error = None
        self._source_revision = 0
        self._settings_path = self.cache_dir.parent / "iina-settings.json"
        self.settings = defaults(self.translation_model)
        try:
            saved = json.loads(self._settings_path.read_text())
            self.settings = checked_settings(saved, self.settings)
        except (OSError, ValueError, TypeError, ImportError):
            pass
        self._base_profile = copy.deepcopy(self.profile)
        self._index_path = self.cache_dir / "index.json"
        try:
            index = json.loads(self._index_path.read_text())
            self._cache_index = index if isinstance(index, dict) else {}
        except (OSError, ValueError):
            self._cache_index = {}
        self._feature_session = False
        self._client_id = None
        self._current_path = None
        self._loading_cache_key = None
        self._preferred_cache_key = None
        self.metrics = {"requests": 0, "initialization_seconds": 0.0, "first_caption_seconds": None}
        self._request_started = None

    def _write_private(self, path, value):
        # Imported lazily to avoid a module cycle during worker startup.
        from iina_bridge import atomic_write

        atomic_write(
            path, json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        )

    def _decoder_version(self):
        from iina_bridge import resolve_executable

        executable = Path(resolve_executable("ffmpeg"))
        stat = executable.stat()
        signature = (str(executable), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        previous = getattr(self, "_decoder_identity", None)
        if previous is None or previous[0] != signature:
            version = subprocess.check_output(
                [str(executable), "-version"], text=True, timeout=10
            ).splitlines()[0]
            self._decoder_identity = (signature, version)
        return self._decoder_identity[1]

    def _selection_request(self, data):
        selection = json_object(data.get("selection", {}), "selection")
        # Older clients have no track metadata; keep the documented first-audio fallback.
        selection = copy.deepcopy(selection)
        if isinstance(selection.get("tracks"), list):
            fields = {
                "id",
                "type",
                "lang",
                "title",
                "codec",
                "ff-index",
                "src-id",
                "external",
                "external-filename",
                "forced",
            }
            selection["tracks"] = [
                {
                    key: value
                    for key, value in track.items()
                    if key in fields
                    or (
                        key == "selected"
                        and track.get("type") == "audio"
                        and selection.get("audio_track_id") in {None, "auto"}
                    )
                }
                for track in selection["tracks"]
                if isinstance(track, dict)
            ]
        selection["mode"] = (
            self.settings["source_mode"] if "mode" not in selection else selection["mode"]
        )
        return selection

    def _feature_update(self, data):
        """Return None for legacy synchronous tests; queue production initialization."""
        if not (self._asynchronous or "selection" in data or self._feature_session):
            return None
        from iina_bridge import boolean

        enabled = boolean(data.get("enabled"), True)
        if not enabled:
            with self.condition:
                value = data.get("path")
                current = (
                    self._pending_request["path"]
                    if self._pending_request
                    else str(self.active.source)
                    if self.active
                    else None
                )
                if value and current and str(Path(value).resolve()) != current:
                    return self._feature_response()
                self.enabled = False
                self.generation += 1
                self._pending_request = None
                if self.active is None:
                    self._request_signature = None
                self.condition.notify_all()
                return self._feature_response()
        value = data.get("path")
        if not isinstance(value, str) or len(value) > 4096 or not Path(value).is_absolute():
            raise ValueError("A local media path is required")
        position = float(data.get("position", 0))
        if not math.isfinite(position) or position < 0:
            raise ValueError("position must be a finite nonnegative number")
        selection = self._selection_request(data)
        identity = {
            "path": str(Path(value).resolve()),
            "selection": selection,
            "settings": {name: self.settings[name] for name in GENERATION_SETTINGS},
        }
        if "cache_key" in data and not data["cache_key"]:
            self._preferred_cache_key = None
        preferred = data.get("cache_key") if "cache_key" in data else self._preferred_cache_key
        preferred = preferred or None
        if preferred is not None:
            if not isinstance(preferred, str) or not KEY.fullmatch(preferred):
                raise ValueError("Некорректный ключ сохранённого перевода")
            identity["cache_key"] = preferred
        signature = descriptor_hash(identity)
        with self.condition:
            self._feature_session = True
            self.position = position
            self.paused = boolean(data.get("paused"), False)
            self.last_heartbeat = time.monotonic()
            self.enabled = True
            self._client_id = str(data.get("client_id", "legacy"))[:128]
            diagnostics: dict[str, bool | int] = {
                name: boolean(data[name])
                for name in ("native_russian_selected", "waiting_for_translation")
                if name in data
            }
            if "seek_generation" in data:
                diagnostics["seek_generation"] = int(data["seek_generation"])
            if signature != self._request_signature:
                self._queue_request(identity, signature)
            elif self.active is not None and diagnostics.get(
                "seek_generation"
            ) != self.client_diagnostics.get("seek_generation"):
                self.generation += 1
                if self.active:
                    self.active.error = None
                    self.active.retry_at = 0
            self.client_diagnostics = diagnostics
            self.condition.notify_all()
        if not self._asynchronous:
            self._initialize_pending()
        with self.condition:
            return self._feature_response()

    def _queue_request(self, request, signature):
        self.generation += 1
        self._request_signature = signature
        self._pending_request = {**request, "generation": self.generation}
        self._pending_request["settings_snapshot"] = copy.deepcopy(self.settings)
        self._current_path = request["path"]
        self._selection = request["selection"]
        self.active = None
        self._initialization_error = None
        self._source_revision += 1
        self._request_started = time.monotonic()
        self.metrics["first_caption_seconds"] = None
        self.phase = "initializing"

    def _initialize_pending(self):
        with self.condition:
            request = self._pending_request
            if request is None or self.closed:
                return False
            self._pending_request = None
            generation = request["generation"]
            self._loading_cache_key = None
            self.processing = True
        started = time.monotonic()
        try:
            # This runs on the worker, never under the HTTP/condition lock.
            self._unload()
            media = self._feature_media(request)
            with self.condition:
                if generation == self.generation and self.enabled:
                    self.active = media
                    self.position = min(self.position, media.duration)
                    self.phase = "ready"
        except Exception as error:
            with self.condition:
                if generation == self.generation:
                    self._initialization_error = str(error)
                    self.phase = "error"
        finally:
            with self.condition:
                self.metrics["initialization_seconds"] = round(time.monotonic() - started, 4)
                self.processing = False
                self._loading_cache_key = None
                self.condition.notify_all()
        return True

    def _feature_media(self, request):
        from iina_bridge import (
            CACHE_VERSION,
            CHUNK_SECONDS,
            CONTEXT_SECONDS,
            Media,
            ollama_version,
            resolve_executable,
        )

        from .deterministic import local_model_identities, profile_descriptor, runtime_versions
        from .media_cache import key_for
        from .speech_vad import vad_descriptor
        from .subtitle_sources import resolve_source
        from .translation_quality import quality_descriptor

        path = Path(request["path"]).resolve(strict=True)
        content, signature = fingerprint_file(path, self._fingerprints)
        duration = self._probe(path)
        settings = copy.deepcopy(request.get("settings_snapshot", self.settings))
        spec = resolve_source(
            path,
            request["selection"],
            ffmpeg=resolve_executable("ffmpeg"),
            duration=duration,
            source_language=settings["source_language"],
            media_sha256=content,
        )
        request_profile = {
            "source": spec.identity,
            "settings": {k: settings[k] for k in GENERATION_SETTINGS},
            "quality": quality_descriptor(settings["glossary"]),
            "vad": vad_descriptor() if settings["vad_mode"] == "conservative" else {"mode": "off"},
        }
        request_hash = descriptor_hash(request_profile)
        lookup = descriptor_hash({"content": content, "request": request_hash})
        entry = self._cache_index.get(lookup, {})
        # Cached output is an artifact, readable even with Ollama stopped or weights removed.
        cached = self._read_cache(request.get("cache_key") or entry.get("key"))
        self._loading_cache_key = cached.get("key") if cached else None
        if cached and "request_profile" not in cached:
            if not request.get("cache_key") or cached.get("source_sha256") != content:
                raise ValueError("Сохранённый перевод относится к другому видео")
            self._check_legacy_audio(request["selection"])
            return self._legacy_media(cached, path, duration, signature, spec, settings)
        if (
            cached
            and request.get("cache_key")
            and cached.get("source_sha256") == content
            and cached.get("request_profile", {}).get("source") == spec.identity
            and cached.get("request_profile") != request_profile
        ):
            # An explicitly selected older prompt/engine contract is an immutable artifact.
            return self._legacy_media(cached, path, duration, signature, spec, settings)
        profile = None
        if (
            cached
            and cached.get("source_sha256") == content
            and cached.get("request_profile") == request_profile
        ):
            profile = cached["profile"]
            try:
                current_digest = local_translation_identity(settings["translation_model"])
            except RuntimeError:
                current_digest = None
            if (
                not request.get("cache_key")
                and current_digest
                and current_digest != profile.get("translation", {}).get("digest")
            ):
                cached = None
            if cached and not request.get("cache_key"):
                try:
                    if spec.kind == "audio":
                        current_models = local_model_identities(
                            self.whisper_model, settings["translation_model"]
                        )
                        if current_models["whisper_revision"] != profile.get("whisper", {}).get(
                            "revision"
                        ):
                            cached = None
                    complete_chunks = (
                        validate_chunks(
                            cached.get("chunks"), duration, CHUNK_SECONDS, allow_cross_boundary=True
                        )
                        if cached
                        else {}
                    )
                    if cached and len(complete_chunks) < math.ceil(duration / CHUNK_SECONDS):
                        # Never append new-engine results to a partially prepared old profile.
                        engines_now = {**runtime_versions(), "ollama": ollama_version()}
                        if spec.kind == "audio":
                            engines_now["ffmpeg"] = self._decoder_version()
                        if any(
                            profile.get("engine_versions", {}).get(name) != value
                            for name, value in engines_now.items()
                        ):
                            cached = None
                except RuntimeError:
                    # The durable part remains usable while its generation dependencies are offline.
                    pass
        elif cached:
            raise ValueError("Сохранённый перевод относится к другому источнику или настройкам")
        if not cached:
            from .translation_quality import OllamaBatchTranslator

            identities = (
                local_model_identities(self.whisper_model, settings["translation_model"])
                if spec.kind == "audio"
                else {
                    "whisper_revision": None,
                    "translation_digest": local_translation_identity(settings["translation_model"]),
                }
            )
            engines = runtime_versions()
            engines["ollama"] = ollama_version()
            if spec.kind == "audio":
                engines["ffmpeg"] = self._decoder_version()
            profile = profile_descriptor(
                self.whisper_model,
                settings["translation_model"],
                engine_versions=engines,
                source_language=settings["source_language"],
                target_language=settings["target_language"],
                **identities,
            )
            if spec.kind == "text":
                profile.pop("whisper", None)
            contract = OllamaBatchTranslator(
                settings["translation_model"],
                settings["source_language"],
                settings["target_language"],
                self.runtime.ollama_url,
            ).descriptor(settings["glossary"])
            profile["translation"]["options"] = contract["options"]
            profile["translation"]["batch_contract"] = contract
            profile["translation"]["history_scope"] = (
                "fixed neighboring source cues across chunk boundaries"
            )
            profile["translation"]["token_budget"] = {"maximum": 4096}
            profile["native"] = request_profile
            profile["segmentation"] = {
                "version": CACHE_VERSION,
                "chunk_seconds": CHUNK_SECONDS,
                "context_seconds": CONTEXT_SECONDS,
                "audio_track": spec.audio_map,
            }
        assert profile is not None
        profile_hash = descriptor_hash(profile)
        key = key_for(content, profile_hash)
        directory = self.cache_dir / key
        directory.mkdir(parents=True, exist_ok=True)
        target_label = (
            "Русский"
            if settings["target_language"] == "ru"
            else LANGUAGES[settings["target_language"]]
        )
        media = Media(
            path,
            key,
            duration,
            self.cache_dir / f"{key}.json",
            directory / f"{target_label} — Live Translation.srt",
            content_sha256=content,
            signature=signature,
        )
        media.spec, media.settings = spec, settings
        media.profile, media.profile_hash = profile, profile_hash
        media.request_profile, media.lookup = request_profile, lookup
        media.original_path = directory / "Original — Live Translation.srt"
        media.partial_chunks = {}
        media.cache_only = bool(cached)
        media.contexts = {}
        if cached:
            media.chunks = validate_chunks(
                cached.get("chunks"), duration, CHUNK_SECONDS, allow_cross_boundary=True
            )
            media.revision = int(cached.get("revision", 0))
        self.profile, self.profile_hash = profile, profile_hash
        self.translation_model = settings["translation_model"]
        self._translator = None
        self._pinned_models = None
        media.asr_profile = {
            "source": spec.identity,
            "whisper": profile.get("whisper"),
            "vad_mode": settings["vad_mode"],
            "recognition_schema": 2,
            "engines": {
                name: value
                for name, value in profile.get("engine_versions", {}).items()
                if name != "ollama"
            },
            "vad": request_profile["vad"],
        }
        media.asr_key = key_for(content, descriptor_hash(media.asr_profile))
        asr_path = self.cache_dir / "recognition" / f"{media.asr_key}.json"
        media.asr_path = asr_path
        asr_path.parent.mkdir(exist_ok=True)
        try:
            recognition = json.loads(asr_path.read_text())
            if recognition.get("profile") == media.asr_profile:
                media.recognized = validate_chunks(
                    recognition.get("chunks"),
                    duration,
                    CHUNK_SECONDS,
                    required_fields=("source",),
                    allow_cross_boundary=True,
                )
        except (OSError, ValueError, TypeError):
            pass
        with self.condition:
            if request.get("generation", self.generation) == self.generation and self.enabled:
                previous_entry = self._cache_index.get(lookup)
                if previous_entry and previous_entry.get("key") != key:
                    self._cache_index["saved:" + previous_entry["key"]] = previous_entry
                self._cache_index[lookup] = {
                    "key": key,
                    "label": path.name,
                    "profile": profile,
                    "request_profile": request_profile,
                    "source_sha256": content,
                }
                self._write_private(self._index_path, self._cache_index)
                if cached:
                    self._save(media)
        return media

    def _display_source_cues(self, media):
        delay = media.spec.subtitle_delay
        result = []
        for cue in media.spec.cues:
            start, end = max(0.0, cue["start"] + delay), min(media.duration, cue["end"] + delay)
            if end > start:
                result.append({**cue, "start": round(start, 3), "end": round(end, 3)})
        return result

    def _check_legacy_audio(self, selection):
        audio = [
            track
            for track in selection.get("tracks", [])
            if track.get("type") == "audio" and not track.get("external")
        ]
        requested = selection.get("audio_track_id")
        if audio and requested not in {None, "auto", audio[0].get("id"), str(audio[0].get("id"))}:
            raise ValueError(
                "Этот сохранённый перевод создан для первой аудиодорожки. Выберите её в IINA."
            )

    def _legacy_media(self, saved, path, duration, signature, spec, settings):
        from iina_bridge import CHUNK_SECONDS, Media, atomic_write

        from .sessions import export_subtitles

        key = saved["key"]
        directory = self.cache_dir / key
        directory.mkdir(exist_ok=True)
        target = saved["profile"].get("target_language", "ru")
        label = "Русский" if target == "ru" else LANGUAGES.get(target, target)
        media = Media(
            path,
            key,
            duration,
            self.cache_dir / f"{key}.json",
            directory / f"{label} — Live Translation.srt",
            content_sha256=saved["source_sha256"],
            signature=signature,
        )
        media.spec, media.settings = spec, settings
        media.profile, media.profile_hash = saved["profile"], saved["profile_sha256"]
        modern = "request_profile" in saved
        media.chunks = validate_chunks(
            saved.get("chunks"), duration, CHUNK_SECONDS, allow_cross_boundary=modern
        )
        media.revision = int(saved.get("revision", 0))
        media.original_path = directory / "Original — Live Translation.srt"
        media.partial_chunks, media.contexts = {}, {}
        media.cache_only = media.read_only = True
        phrases = media.phrases()

        # Legacy caches remain immutable artifacts; never append current-model inference to them.
        def render(values):
            shift = spec.subtitle_delay if modern else 0
            output = [
                {
                    **cue,
                    "start": max(0.0, cue["start"] - shift),
                    "end": max(0.0, cue["end"] - shift),
                }
                for cue in values
                if cue["end"] > max(shift, cue["start"])
            ]
            return export_subtitles({"phrases": output}) if output else ""

        if not media.srt_path.is_file():
            atomic_write(media.srt_path, render(phrases))
        originals = []
        for cue in phrases:
            if (
                originals
                and originals[-1]["source"] == cue["source"]
                and abs(originals[-1]["end"] - cue["start"]) < 0.004
            ):
                originals[-1]["end"] = cue["end"]
            else:
                originals.append({**cue, "translated": cue["source"]})
        if originals and not media.original_path.is_file():
            atomic_write(media.original_path, render(originals))
        return media

    def _prepare_feature_speech(self, media, index, generation, *, keep_loaded=False):
        from iina_bridge import CHUNK_SECONDS, CONTEXT_SECONDS, recognized_cues

        from .translation_quality import conservative_audio_gate

        if str(index) in media.recognized:
            return media.recognized[str(index)]
        if not self._still_active(media, generation):
            return None
        self._check_inputs(media)
        start, end = index * CHUNK_SECONDS, min(media.duration, (index + 1) * CHUNK_SECONDS)
        if media.spec.kind == "text":
            cues = [cue for cue in self._display_source_cues(media) if start <= cue["start"] < end]
        else:
            delay = media.spec.audio_delay + media.spec.subtitle_delay
            decode_start = max(0.0, start - delay - CONTEXT_SECONDS)
            decode_end = min(media.duration, end - delay + CONTEXT_SECONDS)
            self.phase = "recognizing"
            if decode_end <= decode_start:
                cues = []
            else:
                audio = self._decode_audio(media.source, decode_start, decode_end - decode_start)
                if not self._still_active(media, generation):
                    return None
                if media.settings["vad_mode"] == "conservative":
                    from .speech_vad import SileroSpeechGate

                    if not hasattr(self, "_speech_gate"):
                        self._speech_gate = SileroSpeechGate()
                    gate = self._speech_gate.analyze(audio)
                else:
                    gate = conservative_audio_gate(audio, mode="off")
                if len(audio) and gate.should_transcribe:
                    if not self._whisper_resident:
                        self._release_translation(force=True)
                    try:
                        self._asr_language = media.settings["source_language"]
                        result = self._transcribe(audio)
                    finally:
                        if not keep_loaded:
                            self._release_whisper()
                    cues = recognized_cues(result, decode_start + delay, start, end)
                else:
                    cues = []
        with self.condition:
            self._check_inputs(media)
            media.recognized[str(index)] = cues
            self._save_recognition(media)
        return cues if self._still_active(media, generation) else None

    def _prepare_feature_context(self, media, index, generation):
        """A fixed neighboring-source window, independent of viewing/translation order."""
        from .translation_quality import build_timeline_context, stable_cue_id

        if media.spec.kind == "text":
            timeline = self._display_source_cues(media)
        else:
            for neighbor in (index - 1, index + 1):
                if 0 <= neighbor < media.chunk_count and (
                    self._prepare_feature_speech(media, neighbor, generation, keep_loaded=True)
                    is None
                ):
                    return
            timeline = sorted(
                [
                    cue
                    for neighbor in range(max(0, index - 1), min(media.chunk_count, index + 2))
                    for cue in media.recognized.get(str(neighbor), [])
                ],
                key=lambda c: (c["start"], c["end"]),
            )
        ids = [stable_cue_id(c) for c in media.recognized.get(str(index), [])]
        media.contexts[index] = build_timeline_context(timeline, ids)

    def _verify_feature_models(self, media):
        from iina_bridge import ollama_version

        self._verify_feature_artifacts(media)
        if ollama_version() != media.profile.get("engine_versions", {}).get("ollama"):
            raise RuntimeError(
                "Движок перевода изменился. Нажмите «Повторить» для нового перевода."
            )
        media.models_verified = True
        media.cache_only = False

    def _verify_feature_artifacts(self, media):
        from .deterministic import local_model_identities

        if media.spec.kind == "text":
            digest = local_translation_identity(media.settings["translation_model"])
        else:
            if self._decoder_version() != media.profile.get("engine_versions", {}).get("ffmpeg"):
                raise RuntimeError(
                    "Декодер аудио изменился. Нажмите «Повторить» для нового перевода."
                )
            identities = local_model_identities(
                self.whisper_model, media.settings["translation_model"]
            )
            digest = identities["translation_digest"]
            if identities["whisper_revision"] != media.profile.get("whisper", {}).get("revision"):
                raise RuntimeError(
                    "Модель распознавания изменилась. Нажмите «Повторить» для нового перевода."
                )
        if digest != media.profile.get("translation", {}).get("digest"):
            raise RuntimeError(
                "Модель или движок перевода изменились. Нажмите «Повторить» для нового перевода."
            )

    def _translate_feature_speech(self, media, index, cues, generation):
        from iina_bridge import translated_pages

        from .translation_quality import OllamaBatchTranslator, TranslationCancelled, stable_cue_id

        if str(index) in media.chunks or not self._still_active(media, generation):
            return False
        # Silence/empty subtitle sections need no model or Ollama server.
        if cues:
            self._verify_feature_models(media)
        self.phase = "translating"
        translated = {}
        by_id = {stable_cue_id(cue): cue for cue in cues}

        def publish(cue_id, text):
            with self.condition:
                if not self._still_active(media, generation):
                    raise TranslationCancelled()
                self._check_inputs(media)
                translated[cue_id] = [
                    {**page, "owner_start": by_id[cue_id]["start"]}
                    for page in translated_pages(by_id[cue_id], text)
                ]
                media.partial_chunks[str(index)] = [
                    page for key in by_id for page in translated.get(key, [])
                ]
                media.revision += 1
                self._save(media)
                if self.metrics["first_caption_seconds"] is None and self._request_started:
                    self.metrics["first_caption_seconds"] = round(
                        time.monotonic() - self._request_started, 4
                    )

        if cues:
            translator = OllamaBatchTranslator(
                model=media.settings["translation_model"],
                source=media.settings["source_language"],
                target=media.settings["target_language"],
                base_url=self.runtime.ollama_url,
            )
            self._translation_resident = True
            translator.translate(
                cues,
                context=media.contexts.get(index, ()),
                glossary=media.settings["glossary"],
                batch_size=media.settings["batch_size"],
                on_cue=publish,
                cancelled=lambda: not self._still_active(media, generation),
            )
        with self.condition:
            if not self._still_active(media, generation):
                return False
            media.chunks[str(index)] = [page for key in by_id for page in translated.get(key, [])]
            media.partial_chunks.pop(str(index), None)
            media.revision += 1
            self._save(media)
            media.error, media.retry_at = None, 0
        return True

    def _save_feature(self, media):
        from iina_bridge import CACHE_VERSION, PAGE_LAYOUT_VERSION, atomic_write

        from .sessions import export_subtitles

        chunks = {**media.chunks, **media.partial_chunks}
        phrases = sorted(
            [cue for values in chunks.values() for cue in values],
            key=lambda c: (c["start"], c["end"]),
        )
        delay = media.spec.subtitle_delay

        def render(values, original=False):
            output = []
            for cue in values:
                start, end = max(0.0, cue["start"] - delay), max(0.0, cue["end"] - delay)
                if end > start:
                    output.append(
                        {
                            **cue,
                            "start": start,
                            "end": end,
                            "translated": cue["source"] if original else cue["translated"],
                        }
                    )
            return export_subtitles({"phrases": output}) if output else ""

        if phrases or media.srt_path.exists():
            atomic_write(media.srt_path, render(phrases))
        originals = sorted(
            [c for values in media.recognized.values() for c in values],
            key=lambda c: (c["start"], c["end"]),
        )
        if media.spec.kind == "text":
            originals = self._display_source_cues(media)
        elif not originals:
            # The committed translation JSON retains source text even if the ASR artifact was removed.
            grouped = {}
            for cue in media.phrases():
                anchor = cue.get("owner_start", cue["start"])
                key = (anchor, cue["source"])
                if key in grouped:
                    grouped[key]["end"] = max(grouped[key]["end"], cue["end"])
                else:
                    grouped[key] = {**cue, "start": anchor}
            originals = sorted(grouped.values(), key=lambda c: (c["start"], c["end"]))
        if originals or media.original_path.exists():
            atomic_write(media.original_path, render(originals, True))
        # Only complete chunks are durable. Partial output is an incremental display artifact.
        payload = {
            "version": CACHE_VERSION,
            "page_layout_version": PAGE_LAYOUT_VERSION,
            "key": media.key,
            "source": str(media.source),
            "duration": media.duration,
            "revision": media.revision,
            "chunks": media.chunks,
            "source_sha256": media.content_sha256,
            "profile_sha256": media.profile_hash,
            "profile": media.profile,
            "request_profile": media.request_profile,
            "complete": len(media.chunks) == media.chunk_count,
            "completed_chunks": len(media.chunks),
            "total_chunks": media.chunk_count,
        }
        self._write_private(media.json_path, payload)

    def _read_cache(self, key):
        if not isinstance(key, str) or not KEY.fullmatch(key):
            return None
        path = self.cache_dir / f"{key}.json"
        if path.is_symlink():
            return None
        try:
            data = json.loads(path.read_text())
            if (
                not isinstance(data, dict)
                or data.get("key") != key
                or not isinstance(data.get("profile"), dict)
                or descriptor_hash(data["profile"]) != data.get("profile_sha256")
            ):
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def _feature_response(self):
        state = self._response()
        state.update(
            {
                "settings": copy.deepcopy(self.settings),
                "source_revision": self._source_revision,
                "translation_model": self.settings["translation_model"],
                "source_language": self.settings["source_language"],
                "target_language": self.settings["target_language"],
                "metrics": dict(self.metrics),
            }
        )
        if self.enabled and self.active is None:
            state.update(
                status="error" if self._initialization_error else "initializing",
                initializing=True,
                current_ready=False,
                processing=self.processing or self._pending_request is not None,
                source_path=self._pending_request["path"] if self._pending_request else None,
            )
            if self._initialization_error:
                state["error"] = self._initialization_error
        media = self.active
        if media is not None and hasattr(media, "spec"):
            state.update(
                source={
                    "kind": media.spec.kind,
                    "label": media.spec.label,
                    "language": media.settings["source_language"],
                    "audio_label": media.spec.audio_map,
                },
                source_path=str(media.source),
                original_path=str(media.original_path) if media.original_path.is_file() else None,
                coverage=[
                    [int(i) * 30, min(media.duration, (int(i) + 1) * 30)]
                    for i in sorted(media.chunks, key=int)
                ],
                cache_only=media.cache_only,
            )
            if media.partial_chunks and media.srt_path.is_file():
                state["path"] = str(media.srt_path)
            if getattr(media, "read_only", False):
                state["read_only"] = True
                state["source"] = {"kind": "saved", "label": "Сохранённый перевод"}
                state["status"] = "cached"
        return state

    def describe(self):
        models, model_error = [], None
        try:
            with local_urlopen(self.runtime.ollama_url + "/api/tags", timeout=2) as response:
                data = json.load(response)
            models = [
                {"name": model["name"], "size": model.get("size", 0)}
                for model in data.get("models", [])
                if isinstance(model.get("name"), str)
            ]
        except (OSError, ValueError, TypeError, KeyError):
            model_error = "Ollama недоступен. Сохранённые переводы можно открыть без него."
        # Import old committed caches as read-only versions, without requiring their models.
        with self.condition:
            known = {entry.get("key") for entry in self._cache_index.values()}
            for path in self.cache_dir.glob("*.json"):
                if KEY.fullmatch(path.stem) and path.stem not in known:
                    value = self._read_cache(path.stem)
                    if value:
                        self._cache_index["saved:" + path.stem] = {
                            "key": path.stem,
                            "label": Path(value.get("source", "Видео")).name,
                        }
        cache, seen_keys = [], set()
        for entry in list(self._cache_index.values()):
            value = self._read_cache(entry.get("key"))
            if value is None:
                continue
            key = value["key"]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            directory = self.cache_dir / key
            size = (self.cache_dir / f"{key}.json").stat().st_size
            if directory.is_dir() and not directory.is_symlink():
                size += sum(
                    p.stat().st_size
                    for p in directory.iterdir()
                    if p.is_file() and not p.is_symlink()
                )
            cache.append(
                {
                    "key": key,
                    "label": entry.get("label", "Видео"),
                    "complete": value.get("complete", False),
                    "bytes": size,
                    "profile": value.get("profile", {}),
                    "current": bool(self.active and self.active.key == key),
                    "legacy": "request_profile" not in value,
                }
            )
        with self.condition:
            result = {
                "settings": copy.deepcopy(self.settings),
                "models": models,
                "cache": cache,
                "languages": [{"code": code, "name": name} for code, name in LANGUAGES.items()],
                "state": self._feature_response(),
            }
        if model_error:
            result["model_error"] = model_error
        return result

    def control(self, action, data):
        if not isinstance(data, dict):
            raise ValueError("Control data must be an object")
        if action == "describe":
            return self.describe()
        if action == "settings":
            with self.condition:
                previous = self.settings
                changed = checked_settings(data, previous)
                self._write_private(self._settings_path, changed)
                self.settings = changed
                generation_changed = (
                    any(changed[k] != previous[k] for k in GENERATION_SETTINGS)
                    or changed["source_mode"] != previous["source_mode"]
                )
                if generation_changed:
                    self._preferred_cache_key = None
                    self._pending_request = None
                    self._request_signature = None
                    self.generation += 1
                    self.active = None
                    self._source_revision += 1
                self.condition.notify_all()
            return self.describe()
        if action == "retry":
            with self.condition:
                self._request_signature = None
                self._initialization_error = None
                if self.active:
                    self.active.error, self.active.retry_at = None, 0
                self._preferred_cache_key = None
            return self.describe()
        if action in {"cache_delete", "cache_use"}:
            key = data.get("key")
            value = self._read_cache(key)
            if value is None:
                raise ValueError("Сохранённый перевод не найден")
            assert isinstance(key, str)
            if action == "cache_delete":
                with self.condition:
                    deleting_current = (
                        bool(self.active and self.active.key == key)
                        or self._loading_cache_key == key
                    )
                    if deleting_current:
                        self.generation += 1
                        self.active = None
                        self._request_signature = None
                        self._pending_request = None
                        self._source_revision += 1
                    if self._preferred_cache_key == key:
                        self._preferred_cache_key = None
                    directory = self.cache_dir / key
                    if directory.is_symlink():
                        raise ValueError("Некорректная папка кэша")
                    if directory.exists():
                        shutil.rmtree(directory)
                    (self.cache_dir / f"{key}.json").unlink()
                    self._cache_index = {
                        k: v for k, v in self._cache_index.items() if v.get("key") != key
                    }
                    self._write_private(self._index_path, self._cache_index)
                    self.condition.notify_all()
            else:
                with self.condition:
                    media = self.active
                    current_path = self._current_path
                    current_selection = copy.deepcopy(self._selection)
                    generation = self.generation
                if media is None:
                    if not current_path:
                        raise ValueError("Сначала откройте видео")
                    from iina_bridge import resolve_executable

                    from .subtitle_sources import resolve_source

                    digest, _ = fingerprint_file(Path(current_path), self._fingerprints)
                    source = resolve_source(
                        Path(current_path),
                        current_selection,
                        ffmpeg=resolve_executable("ffmpeg"),
                        duration=float(value["duration"]),
                        source_language=value["profile"].get("source_language", "en"),
                        media_sha256=digest,
                    )
                    source_identity, label = source.identity, Path(current_path).name
                else:
                    digest, source_identity, label = (
                        media.content_sha256,
                        media.spec.identity,
                        media.source.name,
                    )
                legacy = "request_profile" not in value
                if legacy:
                    self._check_legacy_audio(current_selection)
                if value.get("source_sha256") != digest or (
                    not legacy and value.get("request_profile", {}).get("source") != source_identity
                ):
                    raise ValueError("Откройте видео с той же исходной дорожкой")
                with self.condition:
                    if generation != self.generation:
                        raise ValueError("Видео или источник изменились. Выберите перевод ещё раз.")
                    saved_settings = value.get("request_profile", {}).get("settings", {})
                    if legacy:
                        saved_settings = {
                            "translation_model": value["profile"]
                            .get("translation", {})
                            .get("model", self.translation_model),
                            "source_language": value["profile"].get("source_language", "en"),
                            "target_language": value["profile"].get("target_language", "ru"),
                        }
                    settings = checked_settings(saved_settings, self.settings)
                    self._write_private(self._settings_path, settings)
                    self.settings = settings
                    lookup = (
                        "saved:" + key
                        if legacy
                        else descriptor_hash(
                            {
                                "content": digest,
                                "request": descriptor_hash(value["request_profile"]),
                            }
                        )
                    )
                    previous_entry = self._cache_index.get(lookup)
                    if previous_entry and previous_entry.get("key") != key:
                        self._cache_index["saved:" + previous_entry["key"]] = previous_entry
                    self._cache_index[lookup] = {"key": key, "label": label}
                    self._write_private(self._index_path, self._cache_index)
                    self._preferred_cache_key = key
                    self._request_signature = None
                    self.generation += 1
                    self._pending_request = None
                    self.active = None
                    self._initialization_error = None
                    self._source_revision += 1
            return self.describe()
        raise ValueError("Неизвестное действие")
