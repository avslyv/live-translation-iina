"""Start the registered local worker and verify the code it actually loaded."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .local_http import local_urlopen
from .runtime_config import RuntimeConfig, load_runtime_config
from .runtime_lifecycle import runtime_revision, runtime_update_pending
from .service_contract import compatible_health


def start_agent(
    agent: Path,
    label: str | None = None,
    *,
    attempts: int = 21,
    retry_delay: float = 0.5,
    deadline: float | None = None,
) -> None:
    """Replace a user job, allowing launchd to finish an immediately prior bootout."""
    if attempts < 1 or not math.isfinite(retry_delay) or retry_delay < 0:
        raise ValueError("LaunchAgent retry settings must be positive")
    label = label or load_runtime_config().bridge_label
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + label

    def command(arguments: list[str], **kwargs) -> subprocess.CompletedProcess:
        remaining = 10.0 if deadline is None else min(10.0, deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError("Timed out while starting the local translation service")
        return subprocess.run(arguments, timeout=remaining, **kwargs)

    command(
        ["/bin/launchctl", "bootout", target],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    command(["/bin/launchctl", "enable", target], check=True)
    arguments = ["/bin/launchctl", "bootstrap", domain, str(agent)]
    for attempt in range(attempts):
        result = command(arguments, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return
        if result.returncode != 5 or attempt == attempts - 1:
            if result.stderr:
                print(result.stderr.strip(), file=sys.stderr)
            raise subprocess.CalledProcessError(
                result.returncode, arguments, output=result.stdout, stderr=result.stderr
            )
        delay = retry_delay
        if deadline is not None:
            delay = min(delay, max(0, deadline - time.monotonic()))
        time.sleep(delay)


def read_health(config: RuntimeConfig, timeout: float = 0.5):
    """Read only this instance's loopback endpoint, without a system HTTP proxy."""
    try:
        with local_urlopen(config.bridge_url + "/health", timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def ensure_service(
    project: Path | None = None,
    *,
    config: RuntimeConfig | None = None,
    expected_runtime_id: str | None = None,
    force_restart: bool = False,
    timeout: float = 30,
    interval: float = 0.2,
    probe: Callable[[float], object] | None = None,
    restart: Callable[[Path, str], None] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Reuse current code or replace the registered job once and await its identity.

    An old process can answer HTTP successfully after an update. Its loaded-code
    revision and capabilities must match before playback can rely on it. Update
    markers also keep this helper from starting a partially copied runtime.
    """
    if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(interval) or interval <= 0:
        raise ValueError("Service wait settings must be positive")
    config = config or load_runtime_config()
    project = project or Path(__file__).resolve().parents[1]
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    probe = probe or (lambda remaining: read_health(config, timeout=min(0.5, remaining)))
    restarted = False
    prior_process_id = None
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError(
                "The translation service did not load the installed runtime within "
                f"{timeout:g} seconds. Review the worker log and retry."
            )
        if runtime_update_pending(project):
            sleep(min(interval, remaining))
            continue
        wanted = runtime_revision(project)
        if expected_runtime_id is not None and wanted != expected_runtime_id:
            raise RuntimeError("The installed runtime changed while starting; retry the launcher")
        body = probe(remaining)
        if not restarted and isinstance(body, dict):
            process_id = body.get("process_id")
            if type(process_id) is int and process_id > 0:
                prior_process_id = process_id
        fresh_process = (
            not restarted
            or prior_process_id is None
            or (isinstance(body, dict) and body.get("process_id") != prior_process_id)
        )
        if compatible_health(body, wanted) and fresh_process and (restarted or not force_restart):
            # The update marker may have appeared while HTTP was in flight.
            if not runtime_update_pending(project) and runtime_revision(project) == wanted:
                assert isinstance(body, dict)
                return body
            continue
        if not restarted:
            agent = config.home / "Library/LaunchAgents" / (config.bridge_label + ".plist")
            if not agent.is_file():
                raise RuntimeError(
                    "The translation LaunchAgent is missing. Run the installer again."
                )
            # An installer can begin while the health probe is in flight.
            # Never replace the job using files from that unfinished update.
            if runtime_update_pending(project) or runtime_revision(project) != wanted:
                continue
            if restart is None:
                start_agent(agent, config.bridge_label, deadline=deadline)
            else:
                restart(agent, config.bridge_label)
            restarted = True
            continue
        sleep(min(interval, max(0, deadline - clock())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ensure", action="store_true", required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        ensure_service(args.project, timeout=args.timeout)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Could not start Live Translation: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
