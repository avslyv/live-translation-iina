#!/usr/bin/env python3
"""Reproducible native-pipeline measurements on synthetic local media only.

Run each variant in a fresh interpreter with an empty private cache. This tool
never installs models or updates a service. Inference uses the already running
local Ollama; reserve its GPU exclusively before running ``measure``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT = (
    (0.5, "This is a local translation benchmark. The blue signal means the system is ready."),
    (
        28.5,
        "At the boundary, the speaker says F sharp minor, then explains why the chord sounds tense.",
    ),
    (
        61.0,
        "The second example costs twenty five dollars. Keep the number and the name Atlas unchanged.",
    ),
    (70.0, "At the end, say goodbye."),
)
ALTERNATE = (
    "Ceci est une autre piste audio. Cette piste doit rester différente de la piste anglaise."
)


def words(text):
    return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)


def word_error_rate(reference: str, hypothesis: str) -> dict:
    """Levenshtein word error rate; diagnostic, not translation-quality scoring."""
    expected, actual = words(reference), words(hypothesis)
    previous = list(range(len(actual) + 1))
    for row, word in enumerate(expected, 1):
        current = [row]
        for column, other in enumerate(actual, 1):
            current.append(
                min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (word != other))
            )
        previous = current
    return {
        "edits": previous[-1],
        "reference_words": len(expected),
        "wer": previous[-1] / len(expected) if expected else None,
    }


def srt_clock(value):
    milliseconds = round(value * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def fixture(directory: Path, duration: int, ffmpeg: str, ffprobe: str) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Fixture destination must be empty; never overwrite existing media")
    cues = []
    clips = []
    for index, (start, text) in enumerate(SCRIPT):
        if start >= duration:
            continue
        clip = directory / f"speech-{index}.aiff"
        subprocess.run(
            ["/usr/bin/say", "-v", "Samantha", "-r", "190", "-o", str(clip), text],
            check=True,
            timeout=30,
        )
        metadata = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(clip)],
            timeout=10,
        )
        seconds = float(json.loads(metadata)["format"]["duration"])
        if start + seconds >= duration:
            raise ValueError("The configured TTS voice does not fit the fixed fixture timeline")
        clips.append((clip, start))
        cues.append({"start": start, "end": round(start + seconds, 3), "source": text})
    alternate = directory / "alternate.aiff"
    subprocess.run(
        ["/usr/bin/say", "-v", "Thomas", "-r", "190", "-o", str(alternate), ALTERNATE],
        check=True,
        timeout=30,
    )
    alternate_seconds = float(
        json.loads(
            subprocess.check_output(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(alternate),
                ],
                timeout=10,
            )
        )["format"]["duration"]
    )
    subtitle = directory / "fixture.en.srt"
    subtitle.write_text(
        "\n\n".join(
            f"{index}\n{srt_clock(cue['start'])} --> {srt_clock(cue['end'])}\n{cue['source']}"
            for index, cue in enumerate(cues, 1)
        )
        + "\n"
    )
    video = directory / "fixture.mkv"
    command = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=320x180:r=2:d={duration}",
    ]
    for clip, _start in clips:
        command.extend(["-i", str(clip)])
    command.extend(["-i", str(alternate), "-i", str(subtitle)])
    filters = [
        f"[{index}:a]aresample=16000,adelay={round(start * 1000)}:all=1[a{index}]"
        for index, (_clip, start) in enumerate(clips, 1)
    ]
    inputs = "".join(f"[a{index}]" for index in range(1, len(clips) + 1))
    filters.append(
        f"{inputs}amix=inputs={len(clips)}:normalize=0,apad,atrim=duration={duration}[english]"
    )
    filters.append(
        f"[{len(clips) + 1}:a]aresample=16000,adelay=700:all=1,apad,atrim=duration={duration}[alternate]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[english]",
            "-map",
            "[alternate]",
            "-map",
            f"{len(clips) + 2}:s:0",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=fra",
            "-metadata:s:s:0",
            "language=eng",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            "-c:s",
            "srt",
            "-t",
            str(duration),
            str(video),
        ]
    )
    subprocess.run(command, check=True, capture_output=True, timeout=45)
    manifest = {
        "schema": 1,
        "duration": duration,
        "video": video.name,
        "subtitle": subtitle.name,
        "reference_cues": cues,
        "alternate_text": ALTERNATE,
        "alternate_reference_cues": [
            {"start": 0.7, "end": round(0.7 + alternate_seconds, 3), "source": ALTERNATE}
        ],
        "voice": "Samantha",
        "alternate_voice": "Thomas",
        "rate": 190,
        "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "tracks": [
            {"type": "audio", "id": 1, "ff-index": 1, "lang": "eng"},
            {"type": "audio", "id": 2, "ff-index": 2, "lang": "fra"},
            {
                "type": "sub",
                "id": 1,
                "ff-index": 3,
                "lang": "eng",
                "codec": "subrip",
                "title": "English full dialogue",
            },
        ],
    }
    write_json(directory / "fixture.json", manifest)
    return manifest


def local_json(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        url, timeout=3
    ) as response:
        return json.load(response)


class MeasuredChatResponse:
    """Pass bytes through unchanged, retaining only server timing/token metadata."""

    def __init__(self, response, started, records):
        self.response = response
        self.started = started
        self.records = records
        self.body = bytearray()

    def __enter__(self):
        self.response.__enter__()
        return self

    def read(self, *args, **kwargs):
        data = self.response.read(*args, **kwargs)
        if isinstance(data, bytes) and len(self.body) + len(data) <= 2 * 1024 * 1024:
            self.body.extend(data)
        return data

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __exit__(self, kind, error, traceback):
        record = {
            "wall_seconds": time.perf_counter() - self.started,
            "error": kind.__name__ if kind else None,
        }
        try:
            data = json.loads(self.body)
            for name in (
                "eval_count",
                "prompt_eval_count",
                "eval_duration",
                "prompt_eval_duration",
                "load_duration",
                "total_duration",
            ):
                if type(data.get(name)) is int and data[name] >= 0:
                    record[name] = data[name]
        except (ValueError, TypeError, AttributeError, UnicodeError):
            pass
        self.records.append(record)
        return self.response.__exit__(kind, error, traceback)


def peak_rss_bytes(value=None, system=None):
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if value is None else value
    return int(value if (system or platform.system()) == "Darwin" else value * 1024)


def reference_for_variant(manifest, directory, variant):
    if variant != "audio2":
        return manifest["reference_cues"]
    if manifest.get("alternate_reference_cues"):
        return manifest["alternate_reference_cues"]
    # Earlier smoke fixtures retain the original AIFF. Read its duration rather
    # than changing the already used fixture or inventing a speech interval.
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("ffprobe is needed to read this older fixture's alternate voice duration")
    metadata = json.loads(
        subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(directory / "alternate.aiff"),
            ],
            timeout=10,
        )
    )
    return [
        {
            "start": 0.7,
            "end": round(0.7 + float(metadata["format"]["duration"]), 3),
            "source": manifest["alternate_text"],
        }
    ]


def summarize_source(reference, cues):
    ordered = sorted(cues, key=lambda cue: (cue["start"], cue["end"]))
    transcript = " ".join(cue["source"] for cue in ordered)
    # Count only utterances wholly outside reference speech, with 0.25s slack.
    silence = [
        cue
        for cue in ordered
        if not any(
            cue["start"] < expected["end"] + 0.25 and cue["end"] > expected["start"] - 0.25
            for expected in reference
        )
    ]
    return {
        **word_error_rate(" ".join(cue["source"] for cue in reference), transcript),
        "transcript": transcript,
        "boundary_term_expected": "f sharp minor"
        in " ".join(words(" ".join(cue["source"] for cue in reference))),
        "boundary_term_present": "f sharp minor" in " ".join(words(transcript)),
        "silence_only_cues": len(silence),
    }


def measure(
    repo: Path, directory: Path, cache: Path, variant: str, output: Path, timeout: float = 420
) -> dict:
    if cache.exists():
        raise ValueError("Measurement cache must not exist; use a fresh path")
    cache.mkdir(parents=True)
    # Native settings live beside the subtitle directory. Give every run its
    # own parent too, so a VAD/glossary setting never leaks into another variant.
    subtitle_cache = cache / "subtitles"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.path.insert(0, str(repo.resolve()))
    started = time.perf_counter()
    bridge_module = importlib.import_module("iina_bridge")
    lifecycle = importlib.import_module("live_translation.runtime_lifecycle")
    loaded_runtime = lifecycle.runtime_revision(repo)
    resident_before = local_json(bridge_module.RUNTIME.ollama_url + "/api/ps")
    if resident_before.get("models"):
        raise RuntimeError(
            "Ollama already has resident models; reserve an idle exclusive run first"
        )
    manifest = json.loads((directory / "fixture.json").read_text())
    reference = reference_for_variant(manifest, directory, variant)
    video = (directory / manifest["video"]).resolve()
    if hashlib.sha256(video.read_bytes()).hexdigest() != manifest["video_sha256"]:
        raise ValueError("The benchmark fixture changed")
    counters = {"asr_calls": 0, "translation_requests": 0}
    phases = {"asr": 0.0, "decode": 0.0, "unload_asr": 0.0, "unload_translation": 0.0}
    chat_records = []
    for name in ("live_translation.deterministic", "live_translation.translation_quality"):
        if (
            name.endswith("translation_quality")
            and not (repo / "live_translation/translation_quality.py").is_file()
        ):
            continue
        module = importlib.import_module(name)
        original = module.local_urlopen

        def counted(request, *args, _original=original, **kwargs):
            if isinstance(request, urllib.request.Request) and request.full_url.endswith(
                "/api/chat"
            ):
                counters["translation_requests"] += 1
                request_started = time.perf_counter()
                try:
                    response = _original(request, *args, **kwargs)
                except Exception as error:
                    chat_records.append(
                        {
                            "wall_seconds": time.perf_counter() - request_started,
                            "error": type(error).__name__,
                        }
                    )
                    raise
                return MeasuredChatResponse(response, request_started, chat_records)
            return _original(request, *args, **kwargs)

        module.local_urlopen = counted
    original_asr = bridge_module.Bridge._transcribe

    def transcribe(self, audio):
        counters["asr_calls"] += 1
        phase_started = time.perf_counter()
        try:
            return original_asr(self, audio)
        finally:
            phases["asr"] += time.perf_counter() - phase_started

    bridge_module.Bridge._transcribe = transcribe
    for method_name, label in (
        ("_decode_audio", "decode"),
        ("_release_whisper", "unload_asr"),
        ("_release_translation", "unload_translation"),
    ):
        original_method = getattr(bridge_module.Bridge, method_name)

        def timed(self, *args, _original=original_method, _label=label, **kwargs):
            phase_started = time.perf_counter()
            try:
                return _original(self, *args, **kwargs)
            finally:
                phases[_label] += time.perf_counter() - phase_started

        setattr(bridge_module.Bridge, method_name, timed)
    bridge = bridge_module.Bridge(cache_dir=subtitle_cache, start_worker=True)
    if variant == "audio-vad":
        bridge.control("settings", {"vad_mode": "conservative"})
    elif variant == "audio2":
        bridge.control("settings", {"source_language": "fr"})
    loaded = time.perf_counter()
    payload = {
        "path": str(video),
        "position": 0.0,
        "enabled": True,
        "paused": False,
        "seek_generation": 0,
    }
    if variant != "baseline":
        payload["selection"] = {
            "mode": "audio" if variant.startswith("audio") else "auto",
            "audio_track_id": 2 if variant == "audio2" else 1,
            "subtitle_track_id": 1,
            "tracks": manifest["tracks"],
            "audio_delay": 0.0,
            "subtitle_delay": 0.0,
        }
    first_caption = None
    first_chunk = None
    seek_started = None
    seek_latency = None
    peak_vram = 0
    last_probe = -1.0
    result = None
    try:
        deadline = started + timeout
        while time.perf_counter() < deadline:
            before = time.perf_counter()
            state = bridge.update(payload)
            now = time.perf_counter()
            if state.get("error"):
                raise RuntimeError(state["error"])
            if now - last_probe >= 1:
                resident = local_json(bridge.runtime.ollama_url + "/api/ps")
                peak_vram = max(
                    peak_vram,
                    sum(model.get("size_vram", 0) for model in resident.get("models", [])),
                )
                last_probe = now
            if state.get("path") and first_caption is None:
                first_caption = now - started
            if state.get("current_ready") and first_chunk is None:
                first_chunk = now - started
                if manifest["duration"] > 60:
                    payload.update(position=61.0, seek_generation=1)
                    seek_started = now
                    continue
            if seek_started is not None and seek_latency is None and state.get("current_ready"):
                seek_latency = now - seek_started
            if state.get("done"):
                total = now - started
                revision = state["revision"]
                srt = Path(state["path"]) if state.get("path") else None
                modified = srt.stat().st_mtime_ns if srt else None
                calls = dict(counters)
                payload.update(position=min(35.0, manifest["duration"] - 1), seek_generation=2)
                cached_start = time.perf_counter()
                cached = bridge.update(payload)
                cached_latency = time.perf_counter() - cached_start
                media = bridge.active
                recognized = [cue for values in media.recognized.values() for cue in values]
                phrases = media.phrases()
                result = {
                    "schema": 1,
                    "variant": variant,
                    "duration": manifest["duration"],
                    "repo_commit": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                    ).strip(),
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "models": {
                        "whisper": bridge.whisper_model,
                        "translation": bridge.translation_model,
                    },
                    "generation_profile": media.profile
                    if hasattr(media, "profile")
                    else bridge.profile,
                    "fixture_sha256": manifest["video_sha256"],
                    "runtime_source_sha256": loaded_runtime,
                    "initial_ollama_models": [],
                    "startup_import_seconds": loaded - started,
                    "first_caption_seconds": first_caption,
                    "first_chunk_seconds": first_chunk,
                    "total_prepare_seconds": total,
                    "uncached_seek_seconds": seek_latency,
                    "cached_seek_seconds": cached_latency,
                    "cached_seek_no_generation": counters == calls
                    and cached["revision"] == revision
                    and (srt.stat().st_mtime_ns if srt else None) == modified,
                    "cached_seek_ready": cached.get("current_ready"),
                    "calls": dict(counters),
                    "phase_seconds": {
                        **phases,
                        "llm_wall": sum(record["wall_seconds"] for record in chat_records),
                    },
                    "chat_requests": list(chat_records),
                    "llm_prompt_tokens": sum(
                        record.get("prompt_eval_count", 0) for record in chat_records
                    ),
                    "llm_output_tokens": sum(
                        record.get("eval_count", 0) for record in chat_records
                    ),
                    "peak_worker_rss_bytes": peak_rss_bytes(),
                    "peak_ollama_reported_vram_bytes": peak_vram,
                    "source_kind": media.spec.kind if hasattr(media, "spec") else "audio",
                    "selected_audio_map": media.spec.audio_map
                    if hasattr(media, "spec")
                    else "0:a:0",
                    "source_quality": summarize_source(reference, recognized),
                    "source_reference": reference,
                    "selected_audio_track": 2 if variant == "audio2" else 1,
                    "translated_cues": phrases,
                    "source_cues": recognized,
                    "original_srt_sha256": hashlib.sha256(
                        media.original_path.read_bytes()
                    ).hexdigest()
                    if hasattr(media, "original_path") and media.original_path.is_file()
                    else None,
                    "translated_srt_sha256": hashlib.sha256(srt.read_bytes()).hexdigest()
                    if srt
                    else None,
                }
                break
            # This is a heartbeat lease, matching the player's polling, not a
            # fake-completion hook. Long inference must not expire at 12s.
            bridge.last_heartbeat = time.monotonic()
            time.sleep(max(0.02, 0.10 - (time.perf_counter() - before)))
        if result is None:
            raise TimeoutError("Native pipeline did not finish within the benchmark budget")
    finally:
        bridge.close()
    result["runtime_source_unchanged"] = lifecycle.runtime_revision(repo) == loaded_runtime
    if variant != "baseline":
        # Test the cached artifact independently of engine/model availability,
        # without stopping the user's server or moving installed weights.
        feature_module = importlib.import_module("live_translation.iina_features")
        deterministic = importlib.import_module("live_translation.deterministic")
        probes = []

        def unavailable(*_args, **_kwargs):
            probes.append("unavailable")
            raise RuntimeError("Benchmark simulated an unavailable local model/engine")

        replaced = [
            (bridge_module, "ollama_version"),
            (feature_module, "local_translation_identity"),
            (deterministic, "local_model_identities"),
        ]
        previous = [(module, name, getattr(module, name)) for module, name in replaced]
        reopened = None
        before = dict(counters)
        reopened_started = time.perf_counter()
        try:
            for module, name, _value in previous:
                setattr(module, name, unavailable)
            reopened = bridge_module.Bridge(cache_dir=subtitle_cache, start_worker=False)
            state = reopened.update(payload)
            result["cached_reopen_simulated_unavailable_models"] = {
                "ready": state.get("current_ready") is True,
                "complete": state.get("done") is True,
                "seconds": time.perf_counter() - reopened_started,
                "no_inference": counters == before,
                "model_probe_failures": len(probes),
                "same_srt_bytes": bool(state.get("path"))
                and hashlib.sha256(Path(state["path"]).read_bytes()).hexdigest()
                == result["translated_srt_sha256"],
            }
        finally:
            if reopened is not None:
                reopened.close()
            for module, name, value in previous:
                setattr(module, name, value)
    result["peak_worker_rss_bytes"] = peak_rss_bytes()
    gates = {
        "runtime_unchanged": result["runtime_source_unchanged"],
        "cached_seek_ready": result["cached_seek_ready"] is True,
        "cached_seek_no_generation": result["cached_seek_no_generation"],
    }
    if variant != "baseline":
        reopen = result["cached_reopen_simulated_unavailable_models"]
        gates["cached_reopen_complete_without_inference"] = all(
            reopen[name] for name in ("ready", "complete", "no_inference", "same_srt_bytes")
        )
    if variant == "text":
        gates["text_bypasses_asr"] = result["source_kind"] == "text" and counters["asr_calls"] == 0
    if variant == "audio2":
        gates["selected_second_audio"] = (
            result["source_kind"] == "audio" and result["selected_audio_map"] == "0:2"
        )
    result["acceptance_gates"] = gates
    write_json(output, result)
    if not all(gates.values()):
        raise RuntimeError("A benchmark acceptance gate failed; inspect the saved JSON report")
    return result


def vad_probe(directory: Path, output: Path, ffmpeg: str) -> dict:
    """Probe the shipped CPU VAD with speech, digital silence, and a pure tone."""
    import numpy as np

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from live_translation.speech_vad import SileroSpeechGate, vad_descriptor

    manifest = json.loads((directory / "fixture.json").read_text())
    video = directory / manifest["video"]
    raw = subprocess.check_output(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(video),
            "-t",
            "6",
            "-map",
            "0:1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        timeout=10,
    )
    clock = np.arange(16000 * 8, dtype=np.float32) / 16000
    samples = {
        "english_tts": np.frombuffer(raw, dtype=np.float32).copy(),
        "digital_silence": np.zeros(16000 * 8, dtype=np.float32),
        "440hz_sine": (0.05 * np.sin(2 * np.pi * 440 * clock)).astype(np.float32),
    }
    gate = SileroSpeechGate()
    records = {}
    for label, audio in samples.items():
        original = audio.copy()
        started = time.perf_counter()
        result = gate.analyze(audio)
        second = gate.analyze(audio)
        records[label] = {
            "should_transcribe": result.should_transcribe,
            "max_probability": result.max_probability,
            "sample_count": result.sample_count,
            "two_pass_seconds": time.perf_counter() - started,
            "repeat_identical": result == second,
            "input_unchanged": bool(np.array_equal(audio, original)),
        }
    report = {
        "schema": 1,
        "device": "cpu",
        "descriptor": vad_descriptor(),
        "fixture_sha256": manifest["video_sha256"],
        "signals": records,
        "limitation": "Synthetic speech, zero-valued silence and one pure tone only; not a music/noise/accents quality benchmark.",
    }
    write_json(output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("fixture")
    make.add_argument("--output", type=Path, required=True)
    make.add_argument("--duration", type=int, choices=(16, 75), default=75)
    run = commands.add_parser("measure")
    run.add_argument("--repo", type=Path, required=True)
    run.add_argument("--fixture", type=Path, required=True)
    run.add_argument("--cache", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--variant", choices=("baseline", "text", "audio", "audio-vad", "audio2"), required=True
    )
    run.add_argument("--timeout", type=float, default=420)
    vad = commands.add_parser("vad")
    vad.add_argument("--fixture", type=Path, required=True)
    vad.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "fixture":
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            parser.error("FFmpeg and ffprobe are required for the synthetic fixture")
        fixture(args.output, args.duration, ffmpeg, ffprobe)
        print("Synthetic fixture ready")
    elif args.command == "vad":
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            parser.error("FFmpeg is required to decode the synthetic speech")
        print(json.dumps(vad_probe(args.fixture, args.output, ffmpeg), ensure_ascii=False))
    else:
        result = measure(
            args.repo, args.fixture, args.cache, args.variant, args.output, args.timeout
        )
        summary = {
            key: result[key]
            for key in (
                "variant",
                "first_caption_seconds",
                "total_prepare_seconds",
                "uncached_seek_seconds",
                "cached_seek_seconds",
                "calls",
                "source_kind",
            )
        }
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
