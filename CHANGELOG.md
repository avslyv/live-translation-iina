# Changelog

## 1.4.0 — release candidate

- Reuse a private local signing certificate for installer/DMG and the installed
  IINA launcher, preserving their identity across local rebuilds without changing
  trust settings. Keep ad-hoc CI and Developer ID/notarization as explicit modes.

### Sources, native sidebar and durable versions

- Prefer a full source-language text subtitle track, including external SRT/ASS/SSA
  and supported embedded text. Use speech recognition when no suitable text exists.
- Decode the audio track selected in IINA and fingerprint source content, stream,
  language and timing offsets. Clear stale native tracks when the input changes.
- Add a Russian IINA sidebar with source/model/language settings, timeline coverage,
  playback-wait control, glossary, model discovery and saved-translation management.
- Use fixed neighboring source context across chunk boundaries, validated local
  translation batches and incremental native SRT publication. Recheck model
  identities before committing work and preserve older model versions separately.
- Move file hashing and source initialization off the HTTP request path.
- Read completed translations with Ollama stopped or weights unavailable. Discover
  pre-1.4 caches as immutable saved versions, without mixing new inference into them.
- Add optional original-language secondary subtitles and eleven explicit language
  choices. Preserve manual primary/secondary selections and pauses.
- Ship a pinned MIT-licensed Silero VAD model for experimental whole-window speech
  gating. Retained audio is never cropped or concatenated; filtering is off by default.
- Add a standalone token-free IINA package, artifact verification report and
  Developer ID/notarization CI support. Actual Apple signing and fresh-OS acceptance
  remain separate release gates; see [release signing](docs/RELEASE_SIGNING.md).

Validation and measured results are tracked in [the benchmark guide](docs/BENCHMARK_1_4.md).

## 1.3.2 — 2026-11-17

### Automatic update recovery

- Restart the service by default after installation and verify its loaded code,
  protocol capabilities and new process ID before reporting success.
- Watch completed runtime updates and let launchd start a fresh interpreter.
  Atomic update markers and syntax checks prevent restarts during file copying.
- Make the IINA launcher repair a missing or outdated worker while preserving an
  already compatible running process.
- Negotiate service compatibility before sending player state. Reconnect and
  restore the native Russian track after code changes or same-version restarts.
- Bound stalled requests, ignore late responses, and preserve explicit pauses,
  hidden subtitles, other selected tracks and stopped translation.
- Add regression coverage and a [live update test report](docs/UPDATE_TEST.md).

## 1.3.1 — 2026-11-17

### First-install reliability

- Back up partially prepared runtime folders without requiring an existing worker.
  A first installation can now reuse its newly created virtual environment while
  copying the worker and plugin from the installer payload.
- Treat inaccessible existing applications and executables as unavailable, then
  install verified private copies. This fixes prerequisite discovery when host
  software cannot be read.
- Add a restricted fallback for official Ollama model transfers on affected DNS
  routes. It validates HTTPS destinations before each redirect, resumes bounded
  parallel ranges, checks every artifact's size and SHA-256, and commits the model
  manifest only after all artifacts are verified.
- Connect Python clients directly to validated loopback services instead of
  routing them through a system HTTP proxy. Local redirects must stay on the same
  origin; external HTTPS verification remains enabled.
- Remove a duplicate truststore injection from Whisper download setup and correct
  content identity for reconstructed weights in newer Hugging Face Xet caches.
- Make runtime homes, cache locations, service labels and loopback ports explicit
  for isolated validation. Normal installation defaults remain unchanged.
- Track cold-install methodology, current status and evidence separately from the
  earlier upgrade test in [the cold-install report](docs/COLD_INSTALL_TEST.md).

## 1.3.0 — 2026-11-17

### Downloadable macOS installer

- Added a native installer app with one-button setup, progress, details, retry,
  cancellation, and an option to reopen IINA. No Terminal or Homebrew is required.
- Provision a private Python runtime, IINA, FFmpeg and Ollama from pinned,
  SHA-256-verified artifacts when working installations are unavailable.
- Download Whisper and Gemma locally, reuse installed models and subtitle caches,
  and verify the models before activating the integration.
- Added arm64 DMG/ZIP builds, release checksums, and a GitHub release workflow.
  Public builds are ad-hoc signed; Developer ID signing and notarization are
  supported when the required signing credentials are configured.
- Added FFmpeg-only probing for the portable runtime, version-aware subtitle
  permissions for IINA 1.5+, transient LaunchAgent restart retries, and installer
  regression tests for cancellation, integrity and environment recovery.

## 1.2.0 — 2026-11-16

### Native IINA subtitles

- Added a local IINA plugin and authenticated loopback worker for English-to-Russian
  translation of local videos. Output is a regular SRT track rendered by IINA.
- Automatically attach the Russian track when a video opens, and reload it as
  translated sections become available.
- Prioritize the playhead after a seek. Unprepared sections wait on an owned pause
  and resume after subtitles load; manual pauses and track choices are preserved.
- Ignore stale responses after rapid seeks or file changes. Release an owned wait
  on errors, timeout, or an explicit user resume.

### Cache and local inference

- Cache complete translated sections by video content and generation profile,
  including timestamps, source text, model identity, and fixed generation settings.
- Retain independent recognition results so a missing translation does not require
  running Whisper again. Recover missing SRT output from authoritative JSON caches.
- Prepare the whole file in the background, with phased batches when a useful
  playback buffer is ready. Whisper and the translator use memory in sequence.
- Added portable installation, model-free Python and plugin tests, and contributor
  documentation. Existing system-audio overlay functionality remains available.

## Upstream changes included after 1.1.0

- **All Whisper languages in the menus**: the From/To pickers now list every language Whisper can transcribe (Japanese, Korean, Arabic, Hindi, …) instead of nine.
- **Setup preflight**: `setup.sh` checks for Apple silicon, macOS 14+ and Python 3.12+ up front and explains what is missing, instead of failing inside pip.

## 1.1.0

Faster, smoother transcription and translation — and cleaner text at chunk boundaries.

### Speed
- **Whisper reads audio in memory** instead of writing a temp WAV every pass, skipping the file write + ffmpeg decode + resample on each transcription.
- **Shorter decode fallback** (`temperature=(0.0, 0.2)`) on the chunked path caps worst-case re-decoding on hard speech.
- **Single resample per pass**, shared between the Silero VAD gate and Whisper in streaming mode.
- **No re-fed overlap on clean cuts**: when VAD cuts on a silence boundary, the next chunk starts fresh — less audio per chunk and no boundary duplication.

### Translation
- **Live streaming translation**: the translation now appears word-by-word as it's generated instead of all at once when the block finishes.
- **Cross-block context**: recent source→translation pairs are fed back to the translator so terminology, names and pronouns stay consistent across sentences.
- **Ellipsis phrases merge forward**: a phrase ending in `…` is glued to the following sentence before translating, so trailing-off thoughts are translated as a whole instead of as a dangling fragment.

### Transcription quality (all languages)
- **Time-based seam dedup** using Whisper word timestamps removes overlap duplicates regardless of spelling — catching inflection changes and words split across the seam that text matching misses.
- **Smarter text dedup** also collapses short and inflected repeats at chunk seams (e.g. repeated short words, or the same word re-transcribed with a different ending).

### Housekeeping
- The app's boot log is trimmed to the last 2000 lines on each launch, so it can no longer grow without bound.

Upstream history: [KazKozDev/live-translation](https://github.com/KazKozDev/live-translation/commits/main).
