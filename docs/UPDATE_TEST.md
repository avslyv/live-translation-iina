# Automatic update recovery

Version **1.3.2**, tested on **2026-11-17** with IINA **1.3.5**, macOS **15.6.1**,
and an Apple M4 Max. The existing local model weights were reused: this checks
updates and recovery, separately from the [1.3.1 cold-install test](COLD_INSTALL_TEST.md).

## Behavior

The installer starts the service by default. It verifies the loaded source
revision, protocol capabilities and a new process ID, so an old worker that still
answers HTTP cannot make an update appear complete.

A running worker observes source changes. Installers mark the transaction as
updating before replacing files and publish a syntax-valid ready revision last.
After the completed revision is stable, the old worker exits and launchd KeepAlive
starts a fresh interpreter. Direct source updates are also detected after they
settle. Temporary, invalid or incomplete copies do not trigger an early restart.

The IINA launcher checks the installed revision before opening the player. It
keeps an already compatible worker and replaces a missing or stale one once,
with a bounded readiness wait. The explicit CLI `--no-start` option skips this
immediate startup step; an already running update-aware worker still observes
committed source changes.

The plugin negotiates the service protocol, supported request format, source
revision and process identity. After a restart it reconnects and reloads or
reattaches the cached native Russian track. Request timeouts and capped retry
backoff also recover from stalled or dropped connections. A late reply cannot
replace the state from a recovered connection.

The user's explicit pause, hidden subtitles, another chosen track, or stopped
translation remains in effect.

## Real Mac verification

- An older running worker with legacy health metadata was replaced by the default
  installer invocation, without an extra `--start` or manual launchctl command.
- A completed source update triggered a launchd restart and native Russian-track
  reattachment without a restart command.
- Restoring the source triggered a second automatic restart and reattachment.
- A same-source reinstallation forced a new worker PID while keeping the same
  source identity. IINA recovered automatically.
- The launcher readiness helper then kept the healthy PID instead of restarting
  an already current worker.
- The paused test video retained its pause, cache key and Russian SRT bytes.
  The scoped authentication token was preserved. Native Russian text was observed
  in IINA after the automatic restarts.

The final concurrency fix was committed through the same update transaction and
also restarted the worker automatically. All **383 Python tests**, **15 subtests**
and **46 Node tests** passed, with one optional Python test skipped. Ruff and
Pyright were clean. A [machine-readable summary](validation/update-1.3.2.json)
records the checks without tokens or personal media paths. Raw status and process
evidence remains local.

## Automated coverage

The full test suite covers atomic markers, stable and incomplete source changes,
a real isolated worker exiting after an update, stale and draining old processes,
new-PID verification, launcher reuse, incompatible protocols, disconnected and
stalled HTTP, late responses, native-track reattachment, and user-choice
preservation.
