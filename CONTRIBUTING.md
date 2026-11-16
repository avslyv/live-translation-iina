# Contributing

Bug reports, focused fixes and documentation improvements are welcome. For larger
changes, open an issue describing the intended behavior and tradeoffs first.

This project builds on [KazKozDev/live-translation](https://github.com/KazKozDev/live-translation).
Keep upstream copyright notices and credit when reusing or adapting its code.

## Development setup

Most tests run on Linux or macOS without models, an audio device, IINA, or Ollama:

```bash
python3.13 -m venv .venv-dev
./.venv-dev/bin/python -m pip install -r requirements-dev.txt -r .github/requirements-ci.txt
./.venv-dev/bin/python -m ruff check .
./.venv-dev/bin/python -m pyright
./.venv-dev/bin/python -m pytest -q
node --test tests/test_plugin_autostart.js
bash -n setup.sh install-app.sh setup-iina.sh
```

On Linux, importing the overlay requires PortAudio (`sudo apt-get install libportaudio2`).
One optional MLX cleanup test skips when MLX Whisper is unavailable. Node.js 24 LTS
is used for the plugin harness; it uses only built-in Node modules.

Running inference or testing the actual IINA UI requires an Apple silicon Mac and
the model/runtime dependencies described in the README. Keep development tooling
in a separate environment from a working installation.

## Change guidelines

- Keep Python changes compatible with Python 3.12 and format imports with Ruff.
- Use small, focused commits and describe the behavior a reviewer should verify.
- Add regression coverage for cache invalidation, timestamp ownership, seek races,
  pause ownership, and manual subtitle selection when changing those behaviors.
- Update documentation and the changelog for user-visible changes.
- Never commit media, transcript caches, model weights, auth tokens, or private logs.

The IINA integration uses fixed generation settings and a content-addressed cache.
When changing a prompt, model identity, decode setting, or subtitle segmentation,
update the generation profile so existing output is not silently overwritten.

## Manual IINA verification

Use a short sample you have permission to share, or keep the media private and
describe it. Verify automatic selection on a new file, a seek into an uncached
section, a cached seek, rapid repeated seeks, an existing manual pause, and manual
selection or hiding of subtitles. Confirm only a pause owned by the plugin resumes
automatically. Check that reopening the same file reuses its finished cache.

## Pull requests

Explain the original problem, resulting behavior, and tests. Include a screenshot
for visible UI changes and call out migration or compatibility implications. Do
not claim a model or platform was tested unless you actually ran it.
