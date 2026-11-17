"""Exercise the real worker's update exit with isolated ports and no models."""

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from live_translation.runtime_lifecycle import begin_runtime_update, finish_runtime_update


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def get_health(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        connection.close()


def test_running_worker_reports_loaded_code_then_exits_after_completed_update(tmp_path):
    source = Path(__file__).resolve().parents[1]
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("iina_bridge.py", "requirements-iina.txt", "requirements-iina.lock.txt"):
        shutil.copy2(source / name, runtime / name)
    shutil.copytree(
        source / "live_translation",
        runtime / "live_translation",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    initial_id = finish_runtime_update(runtime)
    home = tmp_path / "home"
    support = home / "Library/Application Support/LiveTranslate"
    support.mkdir(parents=True)
    (support / "iina-token").write_text("isolated-http-fixture-token-0123456789")
    port = free_port()
    ollama_port = free_port()
    while ollama_port == port:
        ollama_port = free_port()
    environment = {
        **os.environ,
        "HOME": str(home),
        "CFFIXED_USER_HOME": str(home),
        "LT_INSTANCE_ID": "local.live-translation.update-process-test",
        "LT_BRIDGE_PORT": str(port),
        "LT_OLLAMA_PORT": str(ollama_port),
        "HF_HOME": str(home / "hf"),
        "HF_HUB_CACHE": str(home / "hf/hub"),
        "OLLAMA_MODELS": str(home / "ollama"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.pop("LT_SANDBOX_PROFILE", None)
    environment.pop("PYTHONPATH", None)
    with (tmp_path / "worker.log").open("w+") as output:
        worker = subprocess.Popen(
            [sys.executable, str(runtime / "iina_bridge.py")],
            cwd=runtime,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert worker.poll() is None
                try:
                    health = get_health(port)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            assert health["runtime_id"] == initial_id
            assert "auto_restart" in health["capabilities"]
            begin_runtime_update(runtime)
            target = runtime / "live_translation/service_contract.py"
            target.write_bytes(target.read_bytes() + b"\n# Updated runtime fixture.\n")
            # An in-progress transaction must keep serving the old identity,
            # even after the normal two-second restart debounce has elapsed.
            time.sleep(2.6)
            assert worker.poll() is None
            assert get_health(port)["runtime_id"] == initial_id
            next_id = finish_runtime_update(runtime)
            assert next_id != initial_id
            assert worker.wait(timeout=10) == 0
            output.seek(0)
            assert "runtime updated; restarting the local service" in output.read()
        finally:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
