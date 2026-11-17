# Cold-install validation

**Status: PASS — cold installation and offline native subtitles, 2026-11-17.**
Private test jobs, environments and model copies have been removed.

Installer candidate **1.3.1** passed in final empty-profile attempt **6**. Attempt
**5** had failed during integration; its separate diagnostic recovery helped
validate the fix but is not substituted for the completed cold rerun.

The earlier 1.3.0 GUI test reused installed
models and dependencies; it was an upgrade test, not evidence of a clean setup.
This document records the stronger test and the fixes it uncovered.

## Method

The test runs on a physical **Apple M4 Max / 36 GB** Mac with **macOS 15.6.1**
and its native GPU. Each qualifying
attempt starts with a separate, empty home and private temporary/cache folders.
The native installer window is launched through macOS LaunchServices with that
environment. **Install Everything** starts the actual setup pipeline.

The installer backend, its child tools, and both installed services run under a
native macOS sandbox profile. The profile denies access to the host's user home,
Homebrew and `/usr/local`, Python framework and Command Line Tools, the system
Python stub, existing global IINA/Ollama apps, and earlier attempts' homes. It
also denies connections to the host's normal Ollama and translation-service ports.
The test uses separate loopback ports and unique LaunchAgent labels.

The native AppKit window itself is outside this backend sandbox so normal
LaunchServices registration and UI automation can work. It performs no package
installation, model transfer, or inference itself.

macOS rejects LaunchAgent registration by a sandboxed caller. A narrow, host-side
test controller therefore forwards only real `launchctl` operations for this
attempt's two private labels. Before registration it checks the selected home,
ports, plist paths, environment, and mandatory sandbox prefix. It returns the
real operating-system result and logs each operation. It does not seed packages
or models, fake HTTP responses, or replace inference. The actual model and bridge
services do not inherit the controller's Python instrumentation.

## Required baseline

Record these checks immediately before pressing the installation button:

| Item | Required initial state |
| --- | --- |
| Private Python and runtime virtual environment | Absent |
| Private IINA, FFmpeg and Ollama | Absent |
| Ollama model store | Absent or empty |
| Hugging Face model cache | Absent or empty |
| pip cache and prerequisite-download cache | Absent or empty |
| Integration LaunchAgents and token | Absent |
| Private Ollama and bridge endpoints | No listeners |
| Host dependency paths and normal service endpoints | Inaccessible from the sandbox |

Capture the artifact SHA-256, source/payload identity, sandbox-profile SHA-256,
selected environment, actual GUI PID, and namespace for that attempt. Compare
working-installation files locally before and after the test; publish only the
unchanged/changed result, never tokens or sensitive file hashes.

After a fix, a qualifying cold attempt starts with another empty home and empty
caches. Retrying an interrupted download is useful recovery coverage, but must be
reported separately from the initial cold-install result.

## Attempt 5: observed result

Attempt 5 performed real prerequisite downloads and installed the private Python
runtime and recognition dependencies. It also downloaded and verified all of the
Gemma artifacts (approximately **8.02 GB**) and the **1,613,977,612-byte** Whisper
weights. Content hashes are retained in local evidence.

The run then failed in first-install integration. The project directory already
existed because the installer had prepared its virtual environment, but the
backup step assumed an old worker file was present. That missing file stopped
setup before successful plugin/service integration.

## Diagnostic recovery 5: passed, separate from cold acceptance

After the integration fix, recovery reused attempt 5's completed model downloads.
With external networking blocked, a new **75-second synthetic video** with no
recognition or translation cache exercised real Whisper and Gemma inference.
All three sections were prepared (**100%**). Authenticated status reported
`native_russian_selected=true` and `paused=false` after automatic continuation.
Russian text was visible in IINA after seeking to **36.5 seconds**, without
manually selecting a subtitle track.

The recovery network fixture allowed the two private service ports and an
explicit loopback ephemeral-port range **49152–65535** needed by Ollama's model
runner, excluding every baseline TCP listener. Direct WAN, existing local proxy ports,
production service ports, and IPv6 loopback probes were denied; the two private
service endpoints returned HTTP 200. This is a controlled host-network fixture,
not a separate network namespace. Future ephemeral loopback listeners remain
possible; the observed listener delta contained only the private Ollama runner.

The private IINA 1.5 process changed seven UID-scoped preference keys despite the
alternate home. Their prior semantics were restored. The production token and
service-agent files remained unchanged. This is a demonstrated CFPreferences
limitation of the same-user test, rather than proof of a new OS account.

Logs, status and subtitle output were archived locally. Both private jobs were
stopped, and attempt 5's disposable home, environments and model stores were
removed before attempt 6. Recovery success remains separate coverage.

## Attempt 6: passed from empty state

Before launch, the new home contained only its temporary directory. After the
native window opened, only its frontend log had been added. Both private service
ports were closed, the actual GUI process environment matched the selected
profile, and the bundled payload matched the candidate. **Install Everything**
was pressed once. Setup began with a real Python download.

Tested candidate DMG SHA-256:

```text
fd9b926f92a46761604c4df2847a55db1896e898a8f31b6c2bd53a0b9c2090f7
```

All prerequisite and model downloads completed from the empty stores. The GUI
reached **Complete** with progress **1.0** after the single setup-button press.
There were no error events or dependency/model reuse events. Runtime Python was
**3.13.18**, `pip check` reported no broken requirements, and the source, bundled
payload and installed-runtime hashes matched. The tested artifact was not rebuilt
between installation and the final checks.

With external networking blocked, a new unique **75-second English synthetic
video** was encoded after the block and verified to have no existing recognition
or translation cache. Real Whisper and Gemma processing prepared all **3/3**
sections (**100%**). The native Russian track was selected automatically and
playback resumed. Russian full sentences were observed directly in IINA.

A later seek to **35 seconds** used the completed cache immediately. Revision
remained **3**, and the SRT modification time predated the seek. No manual
subtitle-track selection was used.

Runtime GPU evidence reported Gemma **11.9B / Q4_K_M**,
`size_vram=8,061,934,631`, with **49/49 layers offloaded to Metal**. The only new
observed listener belonged to that private model runner.

The offline policy denied WAN, existing proxy endpoints, production service
ports, and IPv6 loopback probes with `EPERM`; both private APIs returned HTTP 200.
Its explicit ephemeral-loopback allowances and limits are described above.
Offline sandbox-profile SHA-256:

```text
6abf5e384a6ebddaac43f53ed69dafb170ffb70e6a7bd46557ecfb9ae1b43b1d
```

The private IINA process has been closed and its UID-scoped preference mutations
restored to the pre-test semantics. Production token and service-agent hashes
remain unchanged. Both private jobs were stopped, their ports were closed, and
the disposable environment and model stores were removed.

## Fixes uncovered during this work

| Area | Change |
| --- | --- |
| Unreadable existing software | An `OSError` during binary discovery now means the candidate is unavailable, allowing installation of a verified private runtime. |
| Service isolation | Explicit home/cache/model paths, configurable loopback ports and namespaced labels prevent launchd from silently using the login user's installation. Preference writes target the selected absolute plist. |
| Official model transfer | A restricted native registry/CDN route can fall back to verified official artifacts. HTTPS host/redirect validation, exact sizes and SHA-256 checks remain enforced; TLS verification is not disabled. |
| Hugging Face TLS | Removed a second global truststore injection that could recursively wrap SSL context setters. The pinned Hub transport already uses macOS trust roots. |
| Xet cache identity | Reconstructed weight bytes are hashed for newer sharded Xet caches; their object identifier is not treated as the content SHA-256. |
| Local HTTP | Validated loopback requests bypass inherited system proxy routing and keep redirects on the same origin. External dependency/model HTTPS downloads retain their normal TLS configuration. |
| First-install backup | Separate strict source-payload validation from lenient backup of a previous or partially prepared runtime. A directory containing only the new virtual environment must not be treated as a complete old installation. Both diagnostic recovery and the final cold rerun passed. |

## Completion evidence

These results come from final attempt 6, not from reused diagnostic-recovery
output:

| Check | Result |
| --- | --- |
| Final attempt and tested artifact SHA-256 | PASS — attempt 6; exact tested candidate hash recorded above |
| All baseline absence and isolation checks | PASS — verified before attempt 6; evidence retained locally |
| Python, IINA, FFmpeg and Ollama downloaded and installed | PASS — fresh downloads and private installation |
| Fresh dependency resolution and installation without Homebrew or compiler repair | PASS — private Python 3.13.18; no broken requirements |
| Gemma and Whisper downloaded into the empty private stores | PASS — no dependency/model reuse events |
| Artifact sizes and model identities/content hashes verified | PASS — verified model identities matched attempt 5; installed payload matched tested source |
| Authenticated bridge status and private service configuration | PASS — private endpoints and isolated context verified |
| Installer reaches completion after the single setup button | PASS — all GUI stages complete, progress 1.0, no error events |
| New, uncached media transcribed and translated with external networking blocked | PASS — fresh 75-second media; real 3/3 sections prepared offline |
| Native Russian IINA subtitle track selected and visible | PASS — automatic selection/resume and direct visual confirmation; cached seek immediate |
| Production token/agent unchanged; UID preferences checked/restored; private jobs cleaned up | PASS — token/agent unchanged; preferences restored; private jobs and model copies removed |

For the offline test, use new synthetic media with no recognition or translation
cache. Block external networking while retaining the private loopback endpoints,
then require real Whisper and Ollama execution and observable native subtitles.
A successful `/health` response or playback of previously cached text is not
sufficient evidence of model execution.

A [machine-readable evidence summary](validation/cold-install-1.3.1.json) records
the artifact identity, dependency/model versions, and acceptance results.

## Supporting automated checks

The candidate also passed **303 Python tests** and **15 subtests**, with one
optional test skipped, plus **22 Node plugin tests**. Ruff and Pyright were
clean. These checks supplement the real GUI/model run; they do not replace it.

## Limits

This is a hermetic dependency/model test on the existing host OS, **not a fresh
macOS virtual machine, erased Mac, or separate OS user**. macOS frameworks, GPU,
certificate trust, and same-user system services remain host-provided.
CFPreferences is UID-scoped; private preference-file writes and production-file
comparisons do not establish a new user account's entire preferences behavior.

The test also does not certify Safari quarantine, Gatekeeper first-open approval,
managed-device policy, or every supported macOS/hardware combination. A separate
browser-download test in a clean macOS VM/account is needed for that OS-level
acceptance path. Community builds remain ad-hoc signed and are not notarized by
Apple; see the [installer guide](INSTALLER.md) for first-launch instructions.

Raw evidence stays local. Public reports must omit bearer tokens, signed CDN
URLs, private host paths, and personal media or transcripts.
