#!/usr/bin/env python3
"""Install all native-IINA components for the current user; emit NDJSON progress."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from installer.dependencies import ensure_ffmpeg, ensure_iina, ensure_ollama  # noqa: E402
from live_translation.local_http import local_urlopen  # noqa: E402
from live_translation.model_download import download_ollama_model  # noqa: E402
from live_translation.runtime_config import load_runtime_config  # noqa: E402
from live_translation.runtime_lifecycle import (  # noqa: E402
    begin_runtime_update,
    finish_runtime_update,
    runtime_revision,
    runtime_update_pending,
)
from live_translation.service_contract import compatible_health  # noqa: E402
from live_translation.service_control import start_agent  # noqa: E402

WHISPER = "mlx-community/whisper-large-v3-turbo"
TRANSLATOR = "gemma4:12b"
RUNTIME = load_runtime_config()
OLLAMA_URL = RUNTIME.ollama_url
BRIDGE_LABEL = RUNTIME.bridge_label
OLLAMA_LABEL = RUNTIME.ollama_label


class InstallationCancelled(Exception):
    """An explicit stop request, distinct from retryable download failures."""


class ModelTransferUnavailable(RuntimeError):
    """The native model client cannot use the current network route."""


class Reporter:
    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path
        self.progress = 0.0
        self.lock = threading.RLock()

    def __call__(self, event: dict):
        with self.lock:
            self._write(event)

    def _write(self, event: dict):
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
    with local_urlopen(load_runtime_config().ollama_url + route, timeout=5) as response:
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


def service_ready(project: Path) -> bool:
    """Verify the loaded worker, rather than accepting an older HTTP listener."""
    try:
        if runtime_update_pending(project):
            return False
        expected = runtime_revision(project)
        with local_urlopen(load_runtime_config().bridge_url + "/health", timeout=1) as response:
            body = json.load(response)
        return (
            compatible_health(body, expected)
            and not runtime_update_pending(project)
            and runtime_revision(project) == expected
        )
    except (OSError, ValueError):
        return False


def ensure_ollama_server(binary: Path, support: Path, report: Reporter):
    runtime = load_runtime_config()
    try:
        body = local_json("/api/version")
    except (OSError, ValueError):
        body = None
    if isinstance(body, dict) and body.get("version"):
        report({"type": "log", "message": "Using the running local Ollama server."})
        return
    agent = runtime.home / "Library/LaunchAgents" / (runtime.ollama_label + ".plist")
    agent.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": runtime.ollama_label,
        "ProgramArguments": runtime.wrap_arguments([str(binary), "serve"]),
        "EnvironmentVariables": {
            **runtime.environment(),
            "OLLAMA_HOST": f"127.0.0.1:{runtime.ollama_port}",
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
    start_agent(agent, runtime.ollama_label)
    for _ in range(60):
        try:
            body = local_json("/api/version")
            if isinstance(body, dict) and body.get("version"):
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(
        f"Ollama could not start on port {runtime.ollama_port}. Review the Ollama log and retry."
    )


def native_model_pull(report: Reporter):
    request = urllib.request.Request(
        load_runtime_config().ollama_url + "/api/pull",
        data=json.dumps({"name": TRANSLATOR, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with local_urlopen(request, timeout=180) as response:
        for line in response:
            item = json.loads(line)
            if item.get("error"):
                reason = str(item["error"])
                if any(
                    marker in reason.lower()
                    for marker in (
                        "redirect target not allowed",
                        "max retries exceeded",
                        "certificate",
                        "tls handshake",
                        "dial tcp",
                        "connection reset",
                        "unexpected eof",
                    )
                ):
                    raise ModelTransferUnavailable("The native model download route is unavailable")
                safe_reason = re.sub(r"https?://\S+", "[download URL]", reason)
                raise RuntimeError("Ollama download failed: " + safe_reason)
            total, completed = item.get("total", 0), item.get("completed", 0)
            progress = 0.5 + 0.2 * min(1, completed / total) if total else 0.5
            detail = str(item.get("status", "Downloading Gemma"))
            if total:
                detail += f" · {completed / 1e9:.2f} / {total / 1e9:.2f} GB"
            report.stage(
                "translation-model", "Downloading Gemma translation model", progress, detail
            )


def pull_translation_model(report: Reporter):
    tags = local_json("/api/tags")
    if any(model.get("name") == TRANSLATOR for model in tags.get("models", [])):
        report(
            {"type": "log", "message": "The translation model is already installed; reusing it."}
        )
        return
    try:
        native_model_pull(report)
    except InstallationCancelled:
        raise
    except (ModelTransferUnavailable, OSError, ValueError):
        report(
            {
                "type": "log",
                "message": "The native transfer could not complete. Downloading verified model artifacts from the official registry.",
            }
        )
        download_ollama_model(TRANSLATOR, load_runtime_config().ollama_models, report)
    tags = local_json("/api/tags")
    if not any(model.get("name") == TRANSLATOR for model in tags.get("models", [])):
        raise RuntimeError("The translation model download did not complete. Retry to resume it.")


def download_whisper(report: Reporter):
    # The pinned Hub/httpcore2 transport already uses macOS trust roots.
    # Globally injecting pip's separate vendored truststore would wrap the
    # installed copy twice and recurse in SSLContext's verification setters.
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
    runtime = load_runtime_config()
    support = runtime.home / "Library/Application Support/LiveTranslate"
    cache = runtime.home / "Library/Caches/LiveTranslationInstaller"
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
        minimum = 3 if whisper_ready() and (runtime.ollama_models / "manifests").exists() else 16
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
        agent = runtime.home / "Library/LaunchAgents" / (runtime.bridge_label + ".plist")
        previous_agent = agent.exists()
        stopped = False
        backup_venv = None
        created_venv = False
        integration_attempted = False
        try:
            if not runtime_ready(python):
                begin_runtime_update(project)
                report.stage(
                    "runtime",
                    "Installing speech recognition libraries",
                    0.3,
                    "This can take a few minutes. Downloads stay on this Mac for retries.",
                )
                if previous_agent:
                    subprocess.run(
                        ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{runtime.bridge_label}"],
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
                if service_ready(project):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "The translation service did not become ready. Review the log and retry."
                )
        except BaseException:
            if integration_attempted or stopped:
                subprocess.run(
                    ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{runtime.bridge_label}"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if created_venv and backup_venv:
                if (project / ".venv").exists():
                    shutil.rmtree(project / ".venv")
                backup_venv.rename(project / ".venv")
            # Before integration begins the old source is still intact. Release
            # its marker only after restoring dependencies and validating it.
            # A partially copied integration remains blocked until a retry.
            if stopped and not integration_attempted:
                try:
                    finish_runtime_update(project)
                except (OSError, RuntimeError, ValueError, SyntaxError) as restore_error:
                    report(
                        {
                            "type": "log",
                            "message": "Runtime recovery needs a retry: " + str(restore_error),
                        }
                    )
            if (stopped or integration_attempted) and previous_agent:
                try:
                    if runtime_update_pending(project):
                        raise RuntimeError("The runtime update is incomplete; retry the installer.")
                    start_agent(agent, runtime.bridge_label)
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
        raise InstallationCancelled("Installation cancelled. Downloads are retained for a retry.")

    signal.signal(signal.SIGTERM, cancelled)
    try:
        if args.whisper_only:
            download_whisper(report)
        else:
            install(report)
        return 0
    except InstallationCancelled as error:
        report({"type": "log", "message": str(error)})
        return 130
    except Exception as error:
        report({"type": "error", "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
