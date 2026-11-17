"""Portable installation contracts: no macOS service or model downloads needed."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "install_iina", Path(__file__).resolve().parents[1] / "tools/install_iina.py"
)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_installed_plugin_has_no_filesystem_api_and_quotes_token(self):
        source = (installer.SOURCE / "iina-plugin/main.js").read_text()
        token = 'scoped-token"with\\literal'
        rendered = installer.render_plugin(source, token)
        self.assertIn("let token = " + json.dumps(token) + ";", rendered)
        self.assertNotIn("file.", rendered)
        self.assertNotIn('token = ""', rendered)
        self.assertIn("function authToken() { return token; }", rendered)

    def test_changed_plugin_template_fails_before_writing(self):
        with self.assertRaisesRegex(RuntimeError, "template changed"):
            installer.render_plugin("const changed = true;", "scoped-token")

    def test_runtime_copy_excludes_checkout_secrets_venv_and_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "checkout", root / "runtime"
            (source / "live_translation").mkdir(parents=True)
            (source / ".venv").mkdir()
            (source / ".venv/token").write_text("secret")
            (source / "iina-token").write_text("secret")
            (source / "video.mp4").write_bytes(b"private media")
            (source / "iina_bridge.py").write_text("# worker")
            (source / "requirements-iina.txt").write_text("mlx-whisper==0.4.3")
            (source / "live_translation/__init__.py").write_text("")
            installer.copy_runtime(source, destination)
            self.assertEqual(
                {
                    str(path.relative_to(destination))
                    for path in destination.rglob("*")
                    if path.is_file()
                },
                {"iina_bridge.py", "requirements-iina.txt", "live_translation/__init__.py"},
            )
            # Repeated updates preserve the installed venv instead of moving it.
            (destination / ".venv").mkdir()
            (destination / ".venv/keep").write_text("environment")
            (source / "iina_bridge.py").write_text("# updated worker")
            installer.copy_runtime(source, destination)
            self.assertEqual((destination / ".venv/keep").read_text(), "environment")
            self.assertEqual((destination / "iina_bridge.py").read_text(), "# updated worker")
            self.assertTrue(source.exists())

    def test_launch_agent_uses_resolved_executables_and_offline_models(self):
        service = installer.launch_agent(
            Path("/Users/example/Runtime"),
            Path("/Users/example/Support"),
            "/custom/bin/ffmpeg",
            "/custom/bin/ffprobe",
            "test/whisper",
            "test-model:12b",
        )
        environment = service["EnvironmentVariables"]
        self.assertEqual(environment["LT_FFMPEG"], "/custom/bin/ffmpeg")
        self.assertEqual(environment["LT_FFPROBE"], "/custom/bin/ffprobe")
        self.assertEqual(environment["LT_WHISPER_MODEL"], "test/whisper")
        self.assertEqual(environment["LT_TRANSLATION_MODEL"], "test-model:12b")
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertNotIn("token", str(service).lower())

    def test_launch_agent_can_use_portable_ffmpeg_without_ffprobe(self):
        service = installer.launch_agent(
            Path("/example/Runtime"),
            Path("/example/Support"),
            "/portable/ffmpeg",
            None,
            "test/whisper",
            "test-model:12b",
        )
        self.assertEqual(service["EnvironmentVariables"]["LT_FFMPEG"], "/portable/ffmpeg")
        self.assertNotIn("LT_FFPROBE", service["EnvironmentVariables"])

    def test_environment_override_selects_the_private_ffmpeg(self):
        with (
            mock.patch.dict(installer.os.environ, {"LT_FFMPEG": "/private-engine/ffmpeg"}),
            mock.patch.object(
                installer.shutil, "which", return_value="/private-engine/ffmpeg"
            ) as which,
        ):
            self.assertEqual(installer.command("ffmpeg"), "/private-engine/ffmpeg")
        which.assert_called_once_with("/private-engine/ffmpeg", path=installer.SEARCH_PATH)

    def test_native_subtitle_permission_matches_iina_api_version(self):
        original = {"permissions": ["network-request", "file-system", "show-osd"]}
        for version, needs_filesystem in (
            ((1, 3, 5), False),
            ((1, 4, 0), False),
            ((1, 5, 0), True),
            ((1, 5, 1), True),
        ):
            with self.subTest(version=version):
                metadata = installer.installed_plugin_metadata(original, version)
                self.assertEqual("file-system" in metadata["permissions"], needs_filesystem)
        self.assertIn("file-system", original["permissions"])

    def test_start_agent_retries_transient_bootstrap_eio_after_enabling_the_job(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        busy = subprocess.CompletedProcess(
            [], 5, stdout="", stderr="Bootstrap failed: 5: Input/output error"
        )
        with (
            mock.patch.object(
                installer.subprocess, "run", side_effect=[ok, ok, busy, ok]
            ) as process,
            mock.patch.object(installer.time, "sleep") as sleep,
        ):
            installer.start_agent(Path("/example/agent.plist"), "test.agent", attempts=3)
        commands = [list(call.args[0]) for call in process.call_args_list]
        self.assertEqual(
            [command[1] for command in commands], ["bootout", "enable", "bootstrap", "bootstrap"]
        )
        self.assertEqual(commands[-1][-1], "/example/agent.plist")
        sleep.assert_called_once_with(0.5)

    def test_start_agent_keeps_the_permanent_error_after_bounded_eio_retries(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        failed = subprocess.CompletedProcess([], 5, stdout="", stderr="invalid job fixture")
        with (
            mock.patch.object(
                installer.subprocess, "run", side_effect=[ok, ok, failed, failed, failed]
            ) as process,
            mock.patch.object(installer.time, "sleep") as sleep,
            mock.patch.object(installer.sys, "stderr"),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            installer.start_agent(Path("/example/agent.plist"), attempts=3)
        self.assertEqual(raised.exception.returncode, 5)
        self.assertEqual(raised.exception.stderr, "invalid job fixture")
        self.assertEqual(process.call_count, 5)
        self.assertEqual(sleep.call_count, 2)

    def test_start_agent_does_not_retry_an_unrelated_launchctl_error(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        failed = subprocess.CompletedProcess([], 125, stdout="", stderr="invalid domain fixture")
        with (
            mock.patch.object(installer.subprocess, "run", side_effect=[ok, ok, failed]),
            mock.patch.object(installer.time, "sleep") as sleep,
            mock.patch.object(installer.sys, "stderr"),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            installer.start_agent(Path("/example/agent.plist"))
        self.assertEqual(raised.exception.returncode, 125)
        sleep.assert_not_called()

    def test_dry_run_never_writes_or_starts_a_process(self):
        with (
            mock.patch.object(installer, "atomic_write") as write,
            mock.patch.object(installer.subprocess, "run") as process,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(installer.main(["--dry-run", "--start", "--download-models"]), 0)
        write.assert_not_called()
        process.assert_not_called()

    def test_install_has_no_dependency_or_model_downloads_by_default(self):
        args = installer.parser().parse_args([])
        self.assertFalse(args.install_dependencies)
        self.assertFalse(args.download_models)
        self.assertFalse(args.start)

    def test_existing_token_is_preserved_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "iina-token"
            token = "a-scoped_token-that-is-long-enough"
            path.write_text(token + "\n")
            self.assertEqual(installer.local_token(path), token)
            self.assertEqual(path.read_text(), token + "\n")

    def test_weak_or_header_unsafe_existing_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "iina-token"
            for token in ("", "short", "a" * 24 + "\nunsafe-header"):
                with self.subTest(token=token):
                    path.write_text(token)
                    with self.assertRaisesRegex(RuntimeError, "token is invalid"):
                        installer.local_token(path)


if __name__ == "__main__":
    unittest.main()
