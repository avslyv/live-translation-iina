#!/usr/bin/env python3
"""Install the native IINA bridge without moving or modifying the source checkout.

The installed runtime lives in the current user's Application Support directory.
Models are downloaded only when --download-models is requested; playback itself
runs with Hugging Face networking disabled.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from live_translation.runtime_config import load_runtime_config  # noqa: E402

LABEL = "local.live-translation.iina"
DEFAULT_WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_TRANSLATION_MODEL = "gemma4:12b"
SEARCH_PATH = os.pathsep.join(("/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")))


def command(name: str) -> str:
    found = optional_command(name)
    if not found:
        raise RuntimeError(f"{name} was not found. Install it and run the installer again.")
    return found


def optional_command(name: str) -> str | None:
    requested = os.environ.get("LT_" + name.upper()) or name
    return shutil.which(str(Path(requested).expanduser()), path=SEARCH_PATH)


def installed_plugin_metadata(info: dict, iina_version: tuple[int, ...]) -> dict:
    """IINA 1.5 guards native subtitle file loading with filesystem permission."""
    result = dict(info)
    result["permissions"] = [
        permission
        for permission in info["permissions"]
        if permission != "file-system" or iina_version >= (1, 5, 0)
    ]
    return result


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), check=True, **kwargs)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".install-tmp")
    temporary.write_bytes(data)
    temporary.chmod(mode)
    temporary.replace(path)


def local_token(path: Path) -> str:
    token = path.read_text().strip() if path.exists() else secrets.token_urlsafe(32)
    if len(token) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise RuntimeError("The existing local token is invalid; restore it from a settings backup")
    return token


def render_plugin(source: str, token: str) -> str:
    """Remove direct filesystem API access from the installed plugin.

    The source version reads a scoped token from disk. The installed version
    embeds that same scoped token. IINA 1.5+ still requires its filesystem
    permission for native subtitle commands; older versions do not.
    Fail if the template changes, rather than publishing a broken installation.
    """
    config = load_runtime_config()
    replacements = {
        'const SERVICE_URL = "http://127.0.0.1:18743/state";': "const SERVICE_URL = "
        + json.dumps(config.bridge_url + "/state")
        + ";",
        "const { console, core, event, file, http, menu, mpv, preferences }": "const { console, core, event, http, menu, mpv, preferences }",
        'let token = "";': "let token = " + json.dumps(token) + ";",
        " || !file.exists(path)": "",
        "path && file.exists(path)": "path",
        'if (code === 401 || code === 403) token = "";': "",
    }
    for before, after in replacements.items():
        if before not in source:
            raise RuntimeError(f"The IINA plugin installation template changed: {before!r}")
        source = source.replace(before, after)
    start = source.index("function authToken() {")
    end = source.index("\nfunction stopPath(", start)
    source = source[:start] + "function authToken() { return token; }\n" + source[end:]
    if "file." in source:
        raise RuntimeError("The installed IINA plugin still requires filesystem access")
    return source


def copy_runtime(source: Path, destination: Path, *, require_worker: bool = True) -> None:
    """Copy runtime source, allowing an incomplete prior install only for backups."""
    if source.resolve() == destination.resolve():
        return
    worker = source / "iina_bridge.py"
    files = [worker] if require_worker or worker.exists() else []
    for name in ("requirements-iina.txt", "requirements-iina.lock.txt"):
        if (source / name).exists():
            files.append(source / name)
    files.extend(sorted((source / "live_translation").glob("*.py")))
    for path in files:
        atomic_write(destination / path.relative_to(source), path.read_bytes(), 0o644)


def launch_agent(
    project: Path,
    support: Path,
    ffmpeg: str,
    ffprobe: str | None,
    whisper_model: str,
    translation_model: str,
) -> dict:
    config = load_runtime_config()
    service = {
        "Label": config.bridge_label,
        "ProgramArguments": config.wrap_arguments(
            [str(project / ".venv/bin/python"), str(project / "iina_bridge.py")]
        ),
        "WorkingDirectory": str(project),
        "EnvironmentVariables": {
            **config.environment(),
            "PATH": SEARCH_PATH,
            "PYTHONUNBUFFERED": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "LT_TRANSLATION_MODEL": translation_model,
            "LT_WHISPER_MODEL": whisper_model,
            "LT_FFMPEG": ffmpeg,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "ProcessType": "Background",
        "StandardOutPath": str(support / "iina-worker.log"),
        "StandardErrorPath": str(support / "iina-worker.error.log"),
    }
    if ffprobe:
        service["EnvironmentVariables"]["LT_FFPROBE"] = ffprobe
    return service


def start_agent(
    agent: Path, label: str | None = None, *, attempts: int = 21, retry_delay: float = 0.5
) -> None:
    """Replace a per-user job, allowing launchd time to finish its prior bootout."""
    if attempts < 1 or retry_delay < 0:
        raise ValueError("LaunchAgent retry settings must be positive")
    label = label or load_runtime_config().bridge_label
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + label
    subprocess.run(
        ["/bin/launchctl", "bootout", target],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    run("/bin/launchctl", "enable", target)
    arguments = ["/bin/launchctl", "bootstrap", domain, str(agent)]
    for attempt in range(attempts):
        result = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return
        # launchctl can report EIO while an immediately preceding bootout is
        # still tearing down the old job. Other failures should surface now.
        if result.returncode != 5 or attempt == attempts - 1:
            if result.stderr:
                print(result.stderr.strip(), file=sys.stderr)
            raise subprocess.CalledProcessError(
                result.returncode,
                arguments,
                output=result.stdout,
                stderr=result.stderr,
            )
        time.sleep(retry_delay)


def install_launcher(app: Path, iina: Path, agent: Path, plugin_info: dict, backup: Path) -> None:
    config = load_runtime_config()
    if app.exists():
        shutil.copytree(app, backup / app.name, symlinks=True)
    contents = app / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    launcher = (
        "#!/bin/bash\nset -e\n"
        + "".join(
            "export " + name + "=" + shlex.quote(value) + "\n"
            for name, value in config.environment().items()
        )
        + f'launchctl bootstrap "gui/$(id -u)" {shlex.quote(str(agent))} >/dev/null 2>&1 || true\n'
        f'launchctl kickstart "gui/$(id -u)/{config.bridge_label}" >/dev/null 2>&1 || true\n'
        f"exec /usr/bin/open -a {shlex.quote(str(iina))}\n"
    )
    atomic_write(contents / "MacOS/LiveTranslationIINA", launcher.encode(), 0o755)
    info = {
        "CFBundleName": "Live Translation for IINA",
        "CFBundleDisplayName": "Live Translation for IINA",
        "CFBundleIdentifier": plugin_info["identifier"] + ".launcher",
        "CFBundleVersion": plugin_info["version"],
        "CFBundleShortVersionString": plugin_info["version"],
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "LiveTranslationIINA",
        "LSMinimumSystemVersion": "14.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    icon = SOURCE / "LiveTranslate.app/Contents/Resources/AppIcon.icns"
    if icon.exists():
        shutil.copy2(icon, resources / "AppIcon.icns")
        info["CFBundleIconFile"] = "AppIcon"
    atomic_write(contents / "Info.plist", plistlib.dumps(info), 0o644)
    run("/usr/bin/codesign", "--force", "--deep", "-s", "-", str(app))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--python", default=sys.executable, help="Python 3.12+ used for the runtime venv"
    )
    result.add_argument(
        "--iina", type=Path, default=Path("/Applications/IINA.app"), help="IINA.app location"
    )
    result.add_argument(
        "--applications-dir",
        type=Path,
        default=Path.home() / "Applications",
        help="destination for the optional IINA launcher",
    )
    result.add_argument(
        "--install-dependencies",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="create/update the runtime venv and install Python dependencies",
    )
    result.add_argument(
        "--download-models",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="download the selected Whisper and Ollama models before offline playback",
    )
    result.add_argument(
        "--start",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="load/reload the LaunchAgent immediately",
    )
    result.add_argument(
        "--launcher",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="install a separate Live Translation for IINA.app launcher",
    )
    result.add_argument(
        "--locked",
        action="store_true",
        help="use the exact requirements-iina.lock.txt environment (Python 3.13 required)",
    )
    result.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    result.add_argument("--translation-model", default=DEFAULT_TRANSLATION_MODEL)
    result.add_argument(
        "--dry-run",
        action="store_true",
        help="print the installation plan without changing anything",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_runtime_config()
    support = config.home / "Library/Application Support/LiveTranslate"
    project = support / "project"
    installed_plugin = support / "iina-plugin"
    plugin_link = (
        config.home
        / "Library/Application Support/com.colliderli.iina/plugins/iina-plugin.iinaplugin-dev"
    )
    agent = config.home / "Library/LaunchAgents" / (config.bridge_label + ".plist")
    preferences = config.cf_home / "Library/Preferences/com.colliderli.iina.plist"
    app = args.applications_dir.expanduser() / "Live Translation for IINA.app"
    iina = args.iina.expanduser().resolve()
    info = json.loads((SOURCE / "iina-plugin/Info.json").read_text())
    plan = {
        "source": str(SOURCE),
        "runtime": str(project),
        "plugin": str(installed_plugin),
        "launch_agent": str(agent),
        "launcher": str(app) if args.launcher else None,
        "iina": str(iina),
        "install_dependencies": args.install_dependencies,
        "download_models": args.download_models,
        "start": args.start,
        "whisper_model": args.whisper_model,
        "translation_model": args.translation_model,
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError(
                "The IINA bridge requires an Apple silicon Mac running macOS 14 or newer"
            )
        if int(platform.mac_ver()[0].split(".", 1)[0]) < 14:
            raise RuntimeError("macOS 14 or newer is required")
        run(
            args.python,
            "-c",
            "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'",
        )
        if not (iina / "Contents/MacOS/IINA").is_file():
            raise RuntimeError(
                f"IINA was not found at {iina}. Install it or pass --iina /path/to/IINA.app"
            )
        with (iina / "Contents/Info.plist").open("rb") as stream:
            iina_info = plistlib.load(stream)
        version = str(iina_info.get("CFBundleShortVersionString", ""))
        match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version)
        if not match or tuple(int(part or 0) for part in match.groups()) < (1, 3, 5):
            raise RuntimeError(f"IINA 1.3.5 or newer is required (found {version or 'unknown'})")
        ffmpeg, ffprobe = command("ffmpeg"), optional_command("ffprobe")
        if plugin_link.is_symlink():
            if plugin_link.resolve() != installed_plugin.resolve():
                raise RuntimeError(f"A different plugin already occupies {plugin_link}")
        elif plugin_link.exists():
            raise RuntimeError(f"A different plugin already occupies {plugin_link}")
        python = project / ".venv/bin/python"
        if not args.install_dependencies and not python.exists():
            raise RuntimeError(
                "Runtime venv missing. Run ./setup-iina.sh or add --install-dependencies"
            )
        if args.locked:
            locked_python = str(python) if python.exists() else args.python
            run(
                locked_python,
                "-c",
                "import sys; assert sys.version_info[:2] == (3, 13), "
                "'--locked requires Python 3.13, the version used to produce the lockfile'",
            )
        # Validate the plugin template before any persistent mutation.
        render_plugin((SOURCE / "iina-plugin/main.js").read_text(), "template-validation")
        token_file = support / "iina-token"
        token = local_token(token_file)
        support.mkdir(parents=True, exist_ok=True)
        support.chmod(0o700)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = support / "backups" / stamp
        backup.mkdir(parents=True, mode=0o700)
        for previous in (agent, preferences):
            if previous.exists():
                shutil.copy2(previous, backup / previous.name)
        if installed_plugin.exists():
            shutil.copytree(installed_plugin, backup / "iina-plugin", symlinks=True)
        if project.exists() and SOURCE.resolve() != project.resolve():
            old_runtime = backup / "runtime"
            copy_runtime(project, old_runtime, require_worker=False)
        copy_runtime(SOURCE, project)
        if args.install_dependencies:
            if not python.exists():
                run(args.python, "-m", "venv", str(project / ".venv"))
            requirements = "requirements-iina.lock.txt" if args.locked else "requirements-iina.txt"
            run(str(python), "-m", "pip", "install", "-r", str(project / requirements))
        run(str(python), "-c", "import mlx_whisper, numpy, huggingface_hub")
        if args.download_models:
            ollama = command("ollama")
            run(
                ollama,
                "pull",
                args.translation_model,
                env={**os.environ, **config.environment(), "OLLAMA_HOST": config.ollama_url},
            )
            download = (
                "import pathlib,sys; from huggingface_hub import snapshot_download; "
                "p=pathlib.Path(sys.argv[1]).expanduser(); "
                "snapshot_download(sys.argv[1]) if not p.is_dir() else None"
            )
            run(
                str(python),
                "-c",
                download,
                args.whisper_model,
                env={
                    **os.environ,
                    **config.environment(),
                    "HF_HUB_OFFLINE": "0",
                    "TRANSFORMERS_OFFLINE": "0",
                },
            )
        atomic_write(token_file, (token + "\n").encode())
        installed_plugin.mkdir(parents=True, exist_ok=True)
        installed_plugin.chmod(0o700)
        info = installed_plugin_metadata(info, tuple(int(part or 0) for part in match.groups()))
        code = render_plugin((SOURCE / "iina-plugin/main.js").read_text(), token)
        atomic_write(installed_plugin / "main.js", code.encode())
        atomic_write(
            installed_plugin / "Info.json",
            (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        node = shutil.which("node", path=SEARCH_PATH)
        if node:
            run(node, "--check", str(installed_plugin / "main.js"))
        plugin_link.parent.mkdir(parents=True, exist_ok=True)
        if not plugin_link.is_symlink():
            plugin_link.symlink_to(installed_plugin, target_is_directory=True)
        service = launch_agent(
            project, support, ffmpeg, ffprobe, args.whisper_model, args.translation_model
        )
        atomic_write(agent, plistlib.dumps(service))
        preferences.parent.mkdir(parents=True, exist_ok=True)
        run(
            "/usr/bin/defaults",
            "write",
            str(preferences.with_suffix("")),
            "iinaEnablePluginSystem",
            "-bool",
            "true",
        )
        run(
            "/usr/bin/defaults",
            "write",
            str(preferences.with_suffix("")),
            "PluginEnabled." + info["identifier"],
            "-bool",
            "true",
        )
        if args.launcher:
            install_launcher(app, iina, agent, info, backup)
        if args.start:
            start_agent(agent)
        print(f"Installed IINA integration {info['version']}\nRuntime: {project}\nBackup: {backup}")
        if args.launcher:
            print(f"Launcher: {app}")
        print("Quit and reopen IINA to load the updated plugin, then open a local English video.")
        if not args.download_models:
            print(
                "Ensure the selected Whisper and Ollama models are already downloaded before playback."
            )
        if not args.start:
            print(
                f"Start the service: launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(agent))}"
            )
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        print(
            "No video, subtitle cache, model weights or source checkout were removed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
