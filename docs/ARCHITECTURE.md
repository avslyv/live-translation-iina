# Architecture

The IINA path prepares persistent native subtitles from a local video's text subtitle source or selected internal audio stream. English → Russian is the default. The sidebar offers 11 source/target languages: English, Russian, Spanish, German, French, Italian, Portuguese, Japanese, Korean, Chinese, and Ukrainian. The system-audio overlay remains a separate entry point.

![Local text/audio sources, fixed context, persistent cache, and native IINA tracks](architecture.svg)

## Components

| Component | Responsibility |
| --- | --- |
| `iina-plugin/main.js` | Observes playback/source changes, sends heartbeats, owns automatic pauses, and manages native primary/secondary subtitles. |
| `iina-plugin/sidebar.*` | Russian settings, installed-model list, coverage, errors, glossary, and saved versions. The WebView has no direct network access. |
| `iina_bridge.py` | Local HTTP boundary, one model worker, decoding, scheduling, memory handoff, and subtitle export. |
| `live_translation/iina_features.py` | Source/profile selection, asynchronous initialization, persistent settings, cache versions, and incremental publication. |
| `live_translation/subtitle_sources.py` | Resolves selected text/internal audio, parses SRT/ASS/SSA, extracts supported embedded text, and records source identity. |
| `live_translation/translation_quality.py` | Canonical glossary, stable cue IDs, bounded source context, structured batches, validation, and missing-cue retries. |
| `live_translation/speech_vad.py`, `assets/` | Optional pinned local Silero detector, model digest, and MIT license. |
| `live_translation/deterministic.py`, `media_cache.py` | Fixed inference settings, model/engine identities, content hashes, and cache validation. |
| `service_contract.py`, `service_control.py`, `runtime_lifecycle.py` | Loaded-runtime identity, verified startup, update transactions, and worker restart. |

## Sources and asynchronous initialization

The plugin supplies the local path, selected audio/subtitle track metadata, source mode, and audio/subtitle delays. **Auto** prefers suitable text subtitles, excluding generated Live Translation and forced/signs-only tracks. Supported external formats are SRT, ASS, and SSA; supported embedded text includes SubRip/ASS/SSA and MOV/MP4 `mov_text`. Image subtitles are not OCR input. If auto mode finds no suitable text, it uses the selected internal audio. Explicit text mode reports unusable input instead of silently changing its source.

Audio mapping uses the selected FFmpeg stream index or audio ordinal. Older clients without metadata retain a first-audio fallback. External audio, remote media, and DVD navigation are outside this adapter's scope.

Production HTTP requests queue initialization. The model worker hashes/probes the media, extracts text, and inspects model/profile data outside the HTTP condition lock. A large hash does not block settings or seeks. Each request carries frozen settings and a generation; an obsolete initialization cannot become active or replace the current cache-index entry.

## Recognition, context, and time

FFmpeg decodes mono float32 audio at 16 kHz into fixed 30-second sections with one second of recognition context. Word midpoints assign overlapping recognition output to one section. The full multilingual Whisper Turbo model receives the selected source language. A running call retains its media's language even if global settings change.

Experimental **conservative** VAD is off by default. When enabled, Silero 6.2.1 runs locally through PyTorch using the bundled SHA-256-pinned MIT model. It can skip an entire source window with no speech candidate. It never crops, concatenates, or time-compresses audio: retained samples reach Whisper intact. Padding is used only inside the detector. Quiet speech can be missed, and VAD does not guarantee rejection of music/noise or all false captions.

Translation uses neighboring **source** cues in fixed timeline order, including section boundaries. Text mode uses the parsed source timeline; audio mode obtains neighboring fixed recognition sections. Default context takes up to two cues on either side, bounded to 12 cues and 2400 source characters. It contains no translated conversation or seek-order-dependent history. Matching canonical glossary terms accompany the request.

Ollama receives one or four requested cues according to settings and a JSON schema. Short IDs are assigned once per batch and restored to stable internal cue IDs after validation; context omits unused opaque IDs. IDs and strings are validated before publication. Missing or invalid strings are retried without recomputing validated siblings; unknown/duplicate IDs are rejected. The wire-ID map, context, glossary, and decoding options remain fixed across a retry.

Validated cues can appear incrementally in SRT while their section is translating. Only complete sections enter durable JSON `chunks`; uncommitted partial output is a display artifact. A long translated page may extend beyond its 30-second section. Its `owner_start` ties it to the original source cue's section so reload validation preserves it.

Source and display times are separate. Text keeps raw timestamps; subtitle delay determines display/section ownership and is applied once by IINA. Audio preparation accounts for audio and subtitle delays when mapping recognition to playback. SRT export removes the already-accounted subtitle offset before the player reapplies its global delay. Track or delay changes select another source identity.

## Queue and native playback

The queue prepares the current missing section, the next 180 seconds, then remaining gaps. Work continues during a normal pause. Once at least 60 seconds are buffered, phase-based recognition can handle up to four future sections per Whisper load, then release it and translate. This ASR phase batch is separate from the sidebar's one/four-cue LLM request size.

A seek supersedes older work. File/source signatures, generation, model artifacts, and engine provenance are checked before commits. A model replaced during an in-flight request cannot write new output under the old profile. Cancellation takes effect at these checks rather than interrupting a running GPU kernel.

With **wait for translation** enabled, a missing current section may trigger a plugin-owned pause. Only that pause is resumed after matching subtitles are applied. User pauses, manual subtitle selection, and hiding subtitles are respected. Errors and a 45-second timeout release the automatic pause. Disabling this option keeps playback running during preparation.

The primary translation and optional secondary original are ordinary IINA tracks updated with mpv `sub-add`/`sub-reload`. Bilingual mode preserves a secondary track selected manually. Appearance, placement, visibility, and delay remain native player controls.

**One service owns one active video.** Multiple windows do not have independent model queues or profiles. A newer active-file request can supersede another window's work.

## Persistent versions and reproducibility

Cache identity combines media-content SHA-256 with a generation profile. Source identity includes stream metadata, subtitle-content hash, language, and delays. The profile records exact model artifacts, engine versions (including FFmpeg for audio), prompt/context policy, glossary, VAD policy/model, inference options, and segmentation. Moving or renaming unchanged media reuses a matching version; changed source/generation settings select another version without deleting older output.

Whisper uses greedy decoding at temperature zero. Translation fixes seed `42`, temperature `0`, `top_k=1`, and `top_p=1`, with thinking and speculative decoding disabled. Fixed prompts reduce variation; fresh computation is not promised bit-identical across hardware, kernels, or engine versions. Reusing a committed artifact guarantees the same saved text.

The default private data layout under `~/Library/Application Support/LiveTranslate/` is:

```text
iina-settings.json                     # Persisted settings and glossary
subtitles/
  index.json                           # Discoverable saved versions
  <key>.json                           # Complete translated sections and provenance
  <key>/<language> — Live Translation.srt
  <key>/Original — Live Translation.srt
  recognition/<asr-key>.json            # Recognition separate from translation
```

Writes use atomic replacement. Existing output can reopen with Ollama stopped or its weights unavailable. Recognition has a separate identity, allowing compatible source work to survive translation-only changes. Partial output from an older engine is preserved while new work receives a new profile; old/new engine output is never mixed in one version. Display preferences do not require regenerating translation.

The cache panel discovers committed JSON even when an older install has no index. Compatible 1.3.2 output can be selected explicitly for its original first audio track as **read-only**. Its JSON and existing SRT remain unchanged, and missing sections are not filled using current models. New preparation creates a separate current-profile version. Deletion is scoped to one translation version and preserves other versions and shared recognition.

## Local protocol and updates

The worker binds to `127.0.0.1:18743`; Ollama defaults to `127.0.0.1:11434`. Local calls bypass system HTTP proxies. The plugin normally has one state request in flight and polls about once per second, with immediate requests after playback/source events.

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /health` | Public | Service/version/protocol, loaded runtime revision, process ID, and capabilities; no private playback state. |
| `POST /state` | Bearer token | File, position, pause, tracks/delays, client/seek generation, selected saved version, or stop. |
| `POST /control` | Bearer token | `describe`, persisted `settings`, `retry`, `cache_use`, and scoped `cache_delete`. |
| `GET /status` | Bearer token | Diagnostic source/playback state, readiness, progress, generations, and client information. |

POST accepts JSON or form data; nested control data is JSON inside the form. The body limit is **256 KiB**. Unrelated browser origins are rejected. Responses carry loaded-runtime identity and `Cache-Control: no-store`. A delayed stop for an old file does not stop a newly selected file.

Heartbeats expire after 12 seconds. The worker stops scheduling and releases models; completed preparation also unloads them after ten idle seconds. The lightweight LaunchAgent remains available. Installation brackets runtime changes with `updating`/`ready` markers, then verifies a matching new process. The launcher reuses compatible code or performs one bounded verified restart. A source watcher exits after completed updates so launchd reloads the worker. Environments, tokens, model weights, and caches survive updates.

## Privacy and release limits

No microphone, BlackHole, or output routing is required. Initial installation downloads components/models; playback uses installed local weights and loopback services. The sidebar sends actions through the plugin. A scoped token is embedded only in the installed copy, outside the checkout. IINA 1.5+ requires filesystem permission for native subtitle-file commands; direct JavaScript filesystem calls are removed from the configured copy.

Saved data contains paths, transcript/translation text, timings, and settings/glossary. Review caches and diagnostics before sharing them publicly.

Developer ID/notarization automation is available, but a real notarized build requires authorized signing material. A fresh macOS VM acceptance run remains separate from the host-isolated cold-install test. Neither is claimed completed by adding automation. See [release signing and test limits](RELEASE_SIGNING.md).
