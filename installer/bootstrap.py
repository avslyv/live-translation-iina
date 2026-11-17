#!/usr/bin/env python3
"""Install all native-IINA components for the current user; emit NDJSON progress."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from installer.dependencies import ensure_ffmpeg, ensure_iina, ensure_ollama  # noqa: E402
from tools.install_iina import start_agent  # noqa: E402

WHISPER = "mlx-community/whisper-large-v3-turbo"
TRANSLATOR = "gemma4:12b"
OLLAMA_URL = "http://127.0.0.1:11434"
BRIDGE_LABEL = "local.live-translation.iina"
OLLAMA_LABEL = "local.live-translation.ollama"


class Reporter:
    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path
        self.progress = 0.0

    def __call__(self, event: dict):
        if "progress" in event:
            self.progress = max(self.progress, min(1.0, float(event["progress"])))
            event = {**event, "progress": self.progress}
        print(json.dumps(event, ensure_ascii=False), flush=True)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.log_path.chmod(0o600)

    def stage(self, name: str, label: str, progress: float, detail: str = ""):
        self(
            {
                "type": "progress",
                "stage": name,
                "label": label,
                "detail": detail,
                "progress": progress,
            }
        )


def run(args: list[str], report: Reporter, *, env: dict | None = None):
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict) and event.get("type") in {"progress", "log", "error"}:
                report(event)
            else:
                report({"type": "log", "message": line})
        if process.wait() != 0:
            raise RuntimeError(
                f"{Path(args[0]).name} failed. See the installation log for details."
            )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def local_json(route: str):
    with urllib.request.urlopen(OLLAMA_URL + route, timeout=5) as response:
        return json.load(response)


def whisper_ready() -> bool:
    cache = Path(
        os.environ.get(
            "HF_HUB_CACHE",
            str(Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))) / "hub"),
        )
    )
    repository = cache / ("models--" + WHISPER.replace("/", "--"))
    try:
        revision = (repository / "refs/main").read_text().strip()
        if not revision or any(c not in "0123456789abcdef" for c in revision):
            return False
        snapshot = repository / "snapshots" / revision
        configuration = json.loads((snapshot / "config.json").read_text())
        return (
            isinstance(configuration, dict)
            and bool(configuration)
            and any(
                (snapshot / name).is_file() and (snapshot / name).stat().st_size > 0
                for name in ("weights.npz", "weights.safetensors", "model.safetensors")
            )
        )
    except (OSError, ValueError):
        return False


def runtime_ready(python: Path) -> bool:
    if not python.exists():
        return False
    expected = dict(
        line.strip().split("==", 1)
        for line in (SOURCE / "requirements-iina.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    script = (
        "import importlib.metadata as m,sys,json; "
        "v=json.loads(sys.argv[1]); "
        "assert all(m.version(k)==x for k,x in v.items()); "
        "assert sys.version_info>=(3,12)"
    )
    result = subprocess.run([str(python), "-c", script, json.dumps(expected)], capture_output=True)
    return result.returncode == 0


def ensure_ollama_server(binary: Path, support: Path, report: Reporter):
    try:
        body = local_json("/api/version")
    except (OSError, ValueError):
        body = None
    if isinstance(body, dict) and body.get("version"):
        report({"type": "log", "message": "Using the running local Ollama server."})
        return
    agent = Path.home() / "Library/LaunchAgents" / (OLLAMA_LABEL + ".plist")
    agent.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": OLLAMA_LABEL,
        "ProgramArguments": [str(binary), "serve"],
        "EnvironmentVariables": {
            "OLLAMA_HOST": "127.0.0.1:11434",
            "OLLAMA_MODELS": str(Path.home() / ".ollama/models"),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "ProcessType": "Background",
        "WorkingDirectory": str(binary.parent),
        "StandardOutPath": str(support / "ollama.log"),
        "StandardErrorPath": str(support / "ollama.error.log"),
    }
    if agent.exists():
        backup = support / "backups" / ("ollama-agent-" + str(time.time_ns()) + ".plist")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(agent, backup)
    temporary = agent.with_suffix(".install-tmp")
    temporary.write_bytes(plistlib.dumps(config))
    temporary.chmod(0o600)
    temporary.replace(agent)
    start_agent(agent, OLLAMA_LABEL)
    for _ in range(60):
        try:
            body = local_json("/api/version")
            if isinstance(body, dict) and body.get("version"):
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Ollama could not start on port 11434. Review the Ollama log and retry.")


def pull_translation_model(report: Reporter):
    tags = local_json("/api/tags")
    if any(model.get("name") == TRANSLATOR for model in tags.get("models", [])):
        report(
            {"type": "log", "message": "The translation model is already installed; reusing it."}
        )
        return
    request = urllib.request.Request(
        OLLAMA_URL + "/api/pull",
        data=json.dumps({"name": TRANSLATOR, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        for line in response:
            item = json.loads(line)
            if item.get("error"):
                raise RuntimeError("Ollama download failed: " + str(item["error"]))
            total, completed = item.get("total", 0), item.get("completed", 0)
            progress = 0.5 + 0.2 * min(1, completed / total) if total else 0.5
            detail = str(item.get("status", "Downloading Gemma"))
            if total:
                detail += f" · {completed / 1e9:.2f} / {total / 1e9:.2f} GB"
            report.stage(
                "translation-model", "Downloading Gemma translation model", progress, detail
            )
    tags = local_json("/api/tags")
    if not any(model.get("name") == TRANSLATOR for model in tags.get("models", [])):
        raise RuntimeError("The translation model download did not complete. Retry to resume it.")


def download_whisper(report: Reporter):
    # Use macOS trust roots without disabling HTTPS verification.
    with contextlib.suppress(ImportError):
        from pip._vendor import truststore

        truststore.inject_into_ssl()
    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm

    class Progress(tqdm):
        def __init__(self, *args, **kwargs):
            import io

            kwargs["file"] = io.StringIO()
            self.last_event = 0.0
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            result = super().update(n)
            now = time.monotonic()
            if self.total and now - self.last_event > 0.5:
                self.last_event = now
                fraction = min(1, self.n / self.total) if self.total > 1_000_000 else 0
                report.stage(
                    "speech-model",
                    "Downloading Whisper speech model",
                    0.73 + 0.15 * fraction,
                    self.desc or "Downloading model files",
                )
            return result

    snapshot_download(
        WHISPER, allow_patterns=["config.json", "*.safetensors", "weights.npz"], tqdm_class=Progress
    )
    if not whisper_ready():
        raise RuntimeError("Whisper download is incomplete. Retry to resume it.")


def install(report: Reporter):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("An Apple silicon Mac running macOS 14 or newer is required.")
    if int(platform.mac_ver()[0].split(".")[0]) < 14:
        raise RuntimeError("macOS 14 or newer is required.")
    support = Path.home() / "Library/Application Support/LiveTranslate"
    cache = Path.home() / "Library/Caches/LiveTranslationInstaller"
    support.mkdir(parents=True, exist_ok=True)
    support.chmod(0o700)
    cache.mkdir(parents=True, exist_ok=True)
    lock = (support / ".installer.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise RuntimeError("Another Live Translation installation is already running.") from error
    with lock:
        report.stage("preflight", "Checking your Mac", 0.05)
        minimum = (
            3 if whisper_ready() and (Path.home() / ".ollama/models/manifests").exists() else 16
        )
        if shutil.disk_usage(support).free < minimum * 1024**3:
            raise RuntimeError(f"Free at least {minimum} GB of disk space, then retry.")
        iina = ensure_iina(cache, emit=report)
        report.stage("components", "Preparing FFmpeg", 0.15)
        ffmpeg = ensure_ffmpeg(cache, support / "tools", emit=report)
        report.stage("components", "Preparing Ollama", 0.22)
        ollama = ensure_ollama(cache, support / "tools", emit=report)
        ensure_ollama_server(ollama, support, report)
        os.environ["PATH"] = os.pathsep.join(
            (str(ffmpeg.parent), str(ollama.parent), os.environ.get("PATH", "/usr/bin:/bin"))
        )
        os.environ["LT_FFMPEG"] = str(ffmpeg)
        project = support / "project"
        python = project / ".venv/bin/python"
        agent = Path.home() / "Library/LaunchAgents" / (BRIDGE_LABEL + ".plist")
        previous_agent = agent.exists()
        stopped = False
        backup_venv = None
        created_venv = False
        integration_attempted = False
        try:
            if not runtime_ready(python):
                report.stage(
                    "runtime",
                    "Installing speech recognition libraries",
                    0.3,
                    "This can take a few minutes. Downloads stay on this Mac for retries.",
                )
                if previous_agent:
                    subprocess.run(
                        ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{BRIDGE_LABEL}"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    stopped = True
                if python.parent.parent.exists():
                    backup_venv = support / "backups" / ("venv-" + str(time.time_ns()))
                    backup_venv.parent.mkdir(parents=True, exist_ok=True)
                    python.parent.parent.rename(backup_venv)
                project.mkdir(parents=True, exist_ok=True)
                created_venv = True
                run([sys.executable, "-m", "venv", str(project / ".venv")], report)
                run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "-r",
                        str(SOURCE / "requirements-iina.txt"),
                    ],
                    report,
                )
            else:
                report.stage("runtime", "Using installed recognition libraries", 0.43)
            report.stage("translation-model", "Preparing Gemma translation model", 0.5)
            pull_translation_model(report)
            report.stage("speech-model", "Preparing Whisper speech model", 0.73)
            if not whisper_ready():
                run(
                    [str(python), str(Path(__file__).resolve()), "--whisper-only"],
                    report,
                    env={
                        **os.environ,
                        "HF_HUB_OFFLINE": "0",
                        "TRANSFORMERS_OFFLINE": "0",
                        "PYTHONUNBUFFERED": "1",
                    },
                )
            else:
                report(
                    {"type": "log", "message": "The speech model is already installed; reusing it."}
                )
            report.stage("verification", "Verifying downloaded local models", 0.9)
            check = (
                "from live_translation.deterministic import local_model_identities; local_model_identities('"
                + WHISPER
                + "','"
                + TRANSLATOR
                + "'); print('Local models verified.')"
            )
            run(
                [
                    str(python),
                    "-c",
                    "import sys; sys.path.insert(0,sys.argv[1]); " + check,
                    str(SOURCE),
                ],
                report,
            )
            report.stage("integration", "Installing IINA plugin and background service", 0.92)
            integration_attempted = True
            run(
                [
                    str(python),
                    str(SOURCE / "tools/install_iina.py"),
                    "--python",
                    sys.executable,
                    "--iina",
                    str(iina),
                    "--no-install-dependencies",
                    "--no-download-models",
                    "--start",
                ],
                report,
            )
            report.stage("verification", "Checking translation service", 0.97)
            for _ in range(30):
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:18743/health", timeout=3
                    ) as response:
                        if json.load(response).get("status") == "ok":
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError(
                    "The translation service did not become ready. Review the log and retry."
                )
        except BaseException:
            if integration_attempted or stopped:
                subprocess.run(
                    ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{BRIDGE_LABEL}"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if created_venv and backup_venv:
                if (project / ".venv").exists():
                    shutil.rmtree(project / ".venv")
                backup_venv.rename(project / ".venv")
            if (stopped or integration_attempted) and previous_agent:
                try:
                    start_agent(agent)
                except (OSError, RuntimeError, subprocess.SubprocessError) as restore_error:
                    report(
                        {
                            "type": "log",
                            "message": "Could not restart the previous service: "
                            + str(restore_error),
                        }
                    )
            raise
        report.stage("complete", "Installation complete", 1)
        report(
            {
                "type": "complete",
                "message": "Plugin, service and local models are ready. Quit and reopen IINA to activate the plugin.",
                "iina_path": str(iina),
                "log_path": str(report.log_path),
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--whisper-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        Reporter()(
            {
                "type": "complete",
                "message": "Read-only preview: install private runtime, dependencies, plugin, service and local models.",
            }
        )
        return 0
    report = Reporter(Path.home() / "Library/Logs/LiveTranslationInstaller/installer.log")

    def cancelled(_signum, _frame):
        raise RuntimeError("Installation cancelled. Downloads are retained for a retry.")

    signal.signal(signal.SIGTERM, cancelled)
    try:
        if args.whisper_only:
            download_whisper(report)
        else:
            install(report)
        return 0
    except Exception as error:
        report({"type": "error", "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
