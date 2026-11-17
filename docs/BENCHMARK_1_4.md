# Native pipeline benchmark

This recipe compares the unchanged 1.3.2 audio pipeline with the 1.4 candidate
using synthetic, non-personal media and already installed local models. It is
not a cross-product translation-quality ranking. Reserve the local Ollama GPU
exclusively; do not run variants concurrently.

## Reproduce

On the same Apple silicon Mac, use the same Python environment, Whisper/Gemma
weights, Ollama engine and FFmpeg. The fixture requires locally installed macOS
Samantha and Thomas voices. No voice, model or dependency downloads are performed.

```bash
python -B tools/benchmark_native.py fixture --output /tmp/lt-corpus --duration 75
python -B tools/benchmark_native.py measure \
  --repo /path/to/unchanged-1.3.2 --fixture /tmp/lt-corpus \
  --variant baseline --cache /tmp/lt-baseline-cache --output /tmp/lt-baseline.json
python -B tools/benchmark_native.py measure \
  --repo /path/to/1.4-candidate --fixture /tmp/lt-corpus \
  --variant text --cache /tmp/lt-text-cache --output /tmp/lt-text.json
python -B tools/benchmark_native.py measure \
  --repo /path/to/1.4-candidate --fixture /tmp/lt-corpus \
  --variant audio --cache /tmp/lt-audio-cache --output /tmp/lt-audio.json
python -B tools/benchmark_native.py measure \
  --repo /path/to/1.4-candidate --fixture /tmp/lt-corpus \
  --variant audio-vad --cache /tmp/lt-vad-cache --output /tmp/lt-audio-vad.json
```

Each measurement must run in a fresh interpreter and use a new cache path.
It also requires no model already resident in Ollama; the runner refuses to
unload someone else's existing work. Runtime source hashes are recorded before
and after measurement. Changed source, failed cached-seek/reopen invariants, an
unexpected ASR call in text mode, or the wrong second-audio mapping fails an
acceptance gate and produces a nonzero exit after saving the report.
Use a separate 16-second fixture first (`--duration 16`) as a smoke check.
For the selected second-audio acceptance check, use a fresh 16-second corpus and
run `--variant audio2`. This explicitly selects FFmpeg stream 2, sets the source
language to French, and compares recognized text against the French reference.
It is a track-selection check, not an English-to-Russian speed comparison.
The runner uses an isolated subtitle/settings cache, never the production cache
or its old `performance-benchmark.json`. It sends ordinary loopback inference
requests to the existing Ollama and releases its models when complete. The
production worker, plugin, service configuration and source are not updated.

## Corpus and measurements

The 75-second black video contains English speech, a distinct French audio
stream, and matching embedded English SubRip. Four reproducible English phrases
cover a long silent interval, an utterance crossing 30 seconds, the term
“F sharp minor”, the number “twenty five”, and the proper name “Atlas”. The exact
source script, offsets, voices and fixture hash are recorded in `fixture.json`;
the public recipe is `tools/benchmark_native.py`. Generated media stays outside
the repository.

Record first translated SRT availability, first completed chunk, complete-file
preparation, an uncached seek to 61 seconds, and a cached seek to 35 seconds.
The scenario waits for the first chunk, seeks to 61 seconds, and then lets
background preparation finish. Its total is not uninterrupted linear throughput:
cancelled background work is part of this seek scenario.
Cached-seek acceptance requires ready state with unchanged revision, subtitle
mtime and inference-call counts. Counters distinguish ASR calls from actual
Ollama chat requests, including translation retries. Models are local and
Hugging Face networking is disabled during each measurement.

The report retains the generation profile and controlled source/translation
cues. Word error rate measures source recognition only. A text-source WER of
zero means the existing reference subtitles were reused; it is not evidence that
its speech recognizer improved. Boundary-term presence and wholly silent-region
cues are narrow diagnostics. Russian translation quality requires reviewing the
recorded output; no BLEU/COMET score or human-quality advantage is inferred.

Worker peak RSS uses the operating system's process high-water mark. Ollama's
reported resident VRAM is sampled separately; the peaks occur in different
phases and must not be added as a measured simultaneous RAM peak. This is one
controlled synthetic corpus, not a movie/accents/noise benchmark or a p50/p95
latency distribution. OS file caches are not flushed. Caption polling has
approximately 100 ms granularity. A 16-second smoke run is not the 75-second
acceptance run.
“First caption” in the JSON is first translated SRT availability at the worker;
it excludes IINA's track-attachment and on-screen rendering time. Native playback
acceptance is checked separately in IINA.

Cached-only reopen checks explicitly make model/version probes unavailable in
the benchmark process, without stopping the real Ollama service or moving
weights. This is a simulated dependency outage, separately labelled in reports;
it is not a network-sandbox offline inference test. Original and translated SRT
hashes verify that reopening the committed artifact preserves saved text.

The optional CPU VAD probe uses the shipped model on synthetic speech, digital
silence and a 440 Hz sine. It repeats each input after resetting state and checks
that the original samples are unchanged:

```bash
python -B tools/benchmark_native.py vad \
  --fixture /tmp/lt-corpus --output /tmp/lt-vad.json
```

Every 30-second section in the 75-second fixture contains some speech. A VAD-on
run therefore tests preservation and overhead here, not fewer ASR calls. It does
not establish accuracy on real music, background noise or unfamiliar accents.

## Observed results

The pre-optimization runs on the Apple M4 Max / 36 GB / macOS 15.6.1 used
Python 3.13.3, Whisper Turbo, and the installed Gemma 4 11.9B Q4_K_M model through
Ollama 0.40.16. All caches were private and initially empty; model weights were
already installed. The initial text-source run exposed a cross-boundary cache
validation bug. Its recovery is not substituted for the subsequent fresh-cache
run, which preserved complete subtitles on model-unavailable cached reopen.

The first audio-only candidate was slower despite making fewer LLM requests.
A separate phase-instrumented diagnostic pair found:

| 75-second diagnostic | 1.3.2 audio | Initial 1.4 audio |
| --- | ---: | ---: |
| ASR calls / chat requests | 3 / 8 | 3 / 3 |
| ASR wall time | 4.150 s | 4.019 s |
| LLM request wall time | 11.202 s | 15.761 s |
| Reported prompt / output tokens | 1,382 / 79 | 1,684 / 339 |
| Reported model-load time | 5.610 s | 3.474 s |
| Reported output-generation time | 1.714 s | 8.190 s |

That result motivated testing shorter wire IDs rather than claiming that fewer
requests automatically improve latency. The first candidate's 24-hexadecimal
wire IDs were replaced by fixed short aliases, restored to stable internal IDs
after strict validation. The optimized audio run emitted **145** output tokens
instead of **339**; reported output-generation time fell from **8.190 s** to
**3.446 s**. No source text or model sampling controls were changed for this
wire-format optimization.

### Optimized version-2 snapshot

These are individual observed runs, not averages or latency percentiles. The
phase-instrumented baseline and optimized candidates used the same runner and
75-second fixture. The candidate's source tree stayed unchanged throughout all
optimized audio, text and French runs.

| Measurement | 1.3.2 audio baseline | 1.4 audio, short IDs | 1.4 existing English text |
| --- | ---: | ---: | ---: |
| First translated SRT available | 7.729 s | 11.745 s | 4.802 s |
| Complete 75-second seek scenario | 16.339 s | 18.503 s | 7.998 s |
| Uncached seek to 61 seconds | 4.351 s | 4.795 s | 3.196 s |
| Cached seek, worker API only | 0.104 ms | 0.111 ms | 0.235 ms |
| ASR calls / actual chat requests | 3 / 8 | 3 / 3 | 0 / 3 |
| Reported prompt / output tokens | 1,382 / 79 | 1,156 / 145 | 1,137 / 151 |
| Peak worker RSS, excluding Ollama/FFmpeg | 1.780 GiB | 1.800 GiB | 51.3 MiB |

The text-input branch removed speech recognition and reduced preparation in this
specific scenario. The richer audio branch **did not beat the baseline** in this
run, despite fewer requests and the wire optimization. Its reported model-load
time was 6.736 s versus 5.610 s in the baseline, and structured output still
generated more tokens. A universal audio speedup or Russian-quality advantage
is not established. Cached API timings are all below a millisecond here and do
not establish a meaningful relative difference or IINA rendering latency.

All optimized acceptance gates passed: selected source, stable runtime, cached
seek without generation, and complete cached reopen with unchanged SRT bytes
under simulated unavailable-model probes. The source hash for this exact
candidate was:

```text
f524d5f0a2ab65e6d5b1b336bd27b64cd1d255d5f5ecafa7d8fbc9559b30fb42
```

The selected-second-audio smoke used the independent French stream, not the
English embedded subtitles. It completed in **7.998 s**, made **one ASR call and
one chat request**, recognized the 15-word French reference without word edits,
and passed the same cached-reopen gates. This validates selection through real
inference; it is not a cross-language latency comparison.

Real CPU Silero probes retained the English synthetic speech and rejected digital
silence and a 440 Hz sine. Repeated results were identical and input arrays were
unchanged. On the speech-containing 75-second fixture, VAD off/on produced the
same recognized cues and translated SRT. It did **not** remove an ASR “Thank” in
a mixed speech/silence window. The text-source branch avoided that ASR output by
using existing text; this does not establish better speech recognition.
