"""Portable installation contracts: no macOS service or model downloads needed."""

import importlib.util
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from live_translation import service_control
from live_translation.runtime_lifecycle import runtime_revision

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

    def test_runtime_backup_accepts_partial_source_but_payload_requires_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "incomplete"
            (source / "live_translation").mkdir(parents=True)
            (source / "live_translation/__init__.py").write_text("# previous helper")
            installer.copy_runtime(source, root / "backup", require_worker=False)
            self.assertEqual(
                (root / "backup/live_translation/__init__.py").read_text(),
                "# previous helper",
            )
            with self.assertRaises(FileNotFoundError):
                installer.copy_runtime(source, root / "runtime")

    def test_first_install_preserves_prepared_venv_and_copies_real_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "Fresh Home"
            support = home / "Library/Application Support/LiveTranslate"
            project = support / "project"
            python = project / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("prepared private Python environment")
            python_inode = python.stat().st_ino
            marker = project / ".venv/installed-dependencies"
            marker.write_text("already installed")
            token_file = support / "iina-token"
            token_file.write_text("existing-scoped-token-preserved-on-update\n")
            model = home / ".cache/huggingface/model-fixture"
            model.parent.mkdir(parents=True)
            model.write_text("existing local weights")
            self.assertFalse((project / "iina_bridge.py").exists())
            iina = root / "IINA.app"
            contents = iina / "Contents"
            (contents / "MacOS").mkdir(parents=True)
            (contents / "MacOS/IINA").touch()
            (contents / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleShortVersionString": "1.5.1"})
            )
            original_copy = installer.copy_runtime

            def copy_with_marker(source, destination, **kwargs):
                if destination == project:
                    state = json.loads((project / "runtime-revision.json").read_text())
                    self.assertEqual(state["state"], "updating")
                original_copy(source, destination, **kwargs)

            def start_after_commit(*args, **kwargs):
                state = json.loads((project / "runtime-revision.json").read_text())
                self.assertEqual(state["state"], "ready")
                self.assertEqual(state["revision"], kwargs["expected_runtime_id"])

            with (
                mock.patch.dict(installer.os.environ, {"HOME": str(home)}, clear=True),
                mock.patch.object(installer.platform, "system", return_value="Darwin"),
                mock.patch.object(installer.platform, "machine", return_value="arm64"),
                mock.patch.object(
                    installer.platform, "mac_ver", return_value=("15.6.1", (), "arm64")
                ),
                mock.patch.object(installer, "command", return_value="/private/ffmpeg"),
                mock.patch.object(installer, "optional_command", return_value=None),
                mock.patch.object(installer.shutil, "which", return_value=None),
                mock.patch.object(installer, "copy_runtime", side_effect=copy_with_marker),
                mock.patch.object(installer, "run") as run,
                mock.patch.object(
                    installer, "ensure_service", side_effect=start_after_commit
                ) as start,
                mock.patch("builtins.print"),
            ):
                self.assertEqual(installer.main(["--iina", str(iina), "--no-launcher"]), 0)
            self.assertEqual(python.read_text(), "prepared private Python environment")
            self.assertEqual(python.stat().st_ino, python_inode)
            self.assertEqual(marker.read_text(), "already installed")
            self.assertEqual(token_file.read_text(), "existing-scoped-token-preserved-on-update\n")
            self.assertEqual(model.read_text(), "existing local weights")
            self.assertEqual(
                (project / "iina_bridge.py").read_bytes(),
                (installer.SOURCE / "iina_bridge.py").read_bytes(),
            )
            self.assertEqual(
                (project / "live_translation/runtime_config.py").read_bytes(),
                (installer.SOURCE / "live_translation/runtime_config.py").read_bytes(),
            )
            plugin = support / "iina-plugin"
            plugin_info = json.loads((plugin / "Info.json").read_text())
            self.assertIn("file-system", plugin_info["permissions"])
            self.assertIn(
                "function authToken() { return token; }", (plugin / "main.js").read_text()
            )
            plugin_link = (
                home
                / "Library/Application Support/com.colliderli.iina/plugins/iina-plugin.iinaplugin-dev"
            )
            self.assertEqual(plugin_link.resolve(), plugin.resolve())
            agent = home / "Library/LaunchAgents/local.live-translation.iina.plist"
            service = plistlib.loads(agent.read_bytes())
            self.assertEqual(
                service["ProgramArguments"], [str(python), str(project / "iina_bridge.py")]
            )
            self.assertEqual(service["EnvironmentVariables"]["HOME"], str(home))
            start.assert_called_once_with(
                project,
                config=mock.ANY,
                expected_runtime_id=runtime_revision(project),
                force_restart=True,
            )
            self.assertEqual(
                json.loads((project / "runtime-revision.json").read_text())["state"], "ready"
            )
            self.assertFalse(
                any("pip" in call.args or "venv" in call.args for call in run.call_args_list)
            )

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

    def test_separate_instance_keeps_launchd_home_caches_ports_and_sandbox_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "Fresh Home"
            profile = root / "clean.sb"
            profile.write_text("(version 1)\n")
            with mock.patch.dict(
                installer.os.environ,
                {
                    "HOME": str(home),
                    "LT_INSTANCE_ID": "local.live-translation.clean-test",
                    "LT_BRIDGE_PORT": "18784",
                    "LT_OLLAMA_PORT": "11484",
                    "LT_SANDBOX_PROFILE": str(profile),
                },
                clear=True,
            ):
                service = installer.launch_agent(
                    home / "Runtime",
                    home / "Support",
                    "/portable/ffmpeg",
                    None,
                    "test/whisper",
                    "test-model:12b",
                )
            self.assertEqual(service["Label"], "local.live-translation.clean-test.iina")
            self.assertEqual(
                service["ProgramArguments"][:3], ["/usr/bin/sandbox-exec", "-f", str(profile)]
            )
            self.assertEqual(
                service["ProgramArguments"][3:],
                [str(home / "Runtime/.venv/bin/python"), str(home / "Runtime/iina_bridge.py")],
            )
            environment = service["EnvironmentVariables"]
            self.assertEqual(environment["HOME"], str(home))
            self.assertEqual(environment["CFFIXED_USER_HOME"], str(home))
            self.assertEqual(environment["HF_HOME"], str(home / ".cache/huggingface"))
            self.assertEqual(environment["HF_HUB_CACHE"], str(home / ".cache/huggingface/hub"))
            self.assertEqual(environment["OLLAMA_MODELS"], str(home / ".ollama/models"))
            self.assertEqual(environment["LT_BRIDGE_PORT"], "18784")
            self.assertEqual(environment["LT_OLLAMA_PORT"], "11484")
            self.assertEqual(environment["LT_SANDBOX_PROFILE"], str(profile))

    def test_rendered_plugin_connects_only_to_the_configured_instance(self):
        source = (installer.SOURCE / "iina-plugin/main.js").read_text()
        with mock.patch.dict(installer.os.environ, {"LT_BRIDGE_PORT": "18784"}):
            rendered = installer.render_plugin(source, "test-scoped-token")
        self.assertIn('const SERVICE_URL = "http://127.0.0.1:18784/state";', rendered)
        self.assertNotIn("127.0.0.1:18743", rendered)

    def test_launcher_uses_its_instance_label_and_explicit_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "Fresh Home"
            app = home / "Applications/Test Launcher.app"
            with (
                mock.patch.dict(
                    installer.os.environ,
                    {
                        "HOME": str(home),
                        "LT_INSTANCE_ID": "local.live-translation.clean-test",
                        "LT_BRIDGE_PORT": "18784",
                        "LT_OLLAMA_PORT": "11484",
                    },
                    clear=True,
                ),
                mock.patch.object(installer, "run"),
                mock.patch.object(installer, "sign_local") as sign,
            ):
                installer.install_launcher(
                    app,
                    home / "Applications/IINA.app",
                    home / "Library/LaunchAgents/clean.plist",
                    {"identifier": "test.plugin", "version": "1.3.0"},
                    root / "Backup",
                )
            sign.assert_called_once_with(app)
            script = (app / "Contents/MacOS/LiveTranslationIINA").read_text()
            self.assertIn("export LT_INSTANCE_ID=local.live-translation.clean-test", script)
            self.assertIn("-m live_translation.service_control --ensure", script)
            self.assertIn("/.venv/bin/python'", script)
            self.assertNotIn("launchctl", script)
            self.assertLess(script.index("--ensure"), script.index("exec /usr/bin/open"))
            self.assertIn("export LT_BRIDGE_PORT=18784", script)
            self.assertIn("export LT_OLLAMA_PORT=11484", script)
            self.assertIn("export CFFIXED_USER_HOME=", script)
            self.assertIn(str(home), script)

    def test_preference_writes_target_the_computed_home_file_not_the_login_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "Fresh Home"
            iina = root / "IINA.app"
            contents = iina / "Contents"
            (contents / "MacOS").mkdir(parents=True)
            (contents / "MacOS/IINA").touch()
            (contents / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleShortVersionString": "1.5.1"})
            )
            python = home / "Library/Application Support/LiveTranslate/project/.venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            with (
                mock.patch.dict(installer.os.environ, {"HOME": str(home)}, clear=True),
                mock.patch.object(installer.platform, "system", return_value="Darwin"),
                mock.patch.object(installer.platform, "machine", return_value="arm64"),
                mock.patch.object(
                    installer.platform, "mac_ver", return_value=("15.6.1", (), "arm64")
                ),
                mock.patch.object(installer, "command", return_value="/private/ffmpeg"),
                mock.patch.object(installer, "optional_command", return_value=None),
                mock.patch.object(installer, "copy_runtime"),
                mock.patch.object(installer, "finish_runtime_update"),
                mock.patch.object(installer, "ensure_service") as ensure,
                mock.patch.object(installer, "atomic_write"),
                mock.patch.object(installer, "run") as run,
                mock.patch("builtins.print"),
            ):
                self.assertEqual(
                    installer.main(["--iina", str(iina), "--no-launcher", "--no-start"]), 0
                )
            ensure.assert_not_called()
            writes = [
                call.args
                for call in run.call_args_list
                if call.args[:2] == ("/usr/bin/defaults", "write")
            ]
            self.assertEqual(len(writes), 2)
            self.assertTrue(
                all(
                    call[2] == str(home / "Library/Preferences/com.colliderli.iina")
                    for call in writes
                )
            )

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
                service_control.subprocess, "run", side_effect=[ok, ok, busy, ok]
            ) as process,
            mock.patch.object(service_control.time, "sleep") as sleep,
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
                service_control.subprocess, "run", side_effect=[ok, ok, failed, failed, failed]
            ) as process,
            mock.patch.object(service_control.time, "sleep") as sleep,
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
            mock.patch.object(service_control.subprocess, "run", side_effect=[ok, ok, failed]),
            mock.patch.object(service_control.time, "sleep") as sleep,
            mock.patch.object(installer.sys, "stderr"),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            installer.start_agent(Path("/example/agent.plist"))
        self.assertEqual(raised.exception.returncode, 125)
        sleep.assert_not_called()

    def test_start_agent_default_label_respects_the_instance_namespace(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            mock.patch.dict(installer.os.environ, {"LT_INSTANCE_ID": "local.clean-test"}),
            mock.patch.object(service_control.subprocess, "run", return_value=ok) as process,
        ):
            installer.start_agent(Path("/example/agent.plist"))
        commands = [list(call.args[0]) for call in process.call_args_list]
        self.assertTrue(commands[0][-1].endswith("/local.clean-test.iina"))
        self.assertEqual(commands[1][-1], commands[0][-1])

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
        self.assertTrue(args.start)
        self.assertFalse(installer.parser().parse_args(["--no-start"]).start)

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
