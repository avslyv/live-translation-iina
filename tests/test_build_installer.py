"""Release payload selection must never include a local installation's data."""

import argparse
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path
from unittest import mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "build_installer", Path(__file__).resolve().parents[1] / "tools/build_installer.py"
)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def source_tree(root: Path) -> Path:
    for relative in builder.REQUIRED_PAYLOAD:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    (root / "iina-plugin/main.js").write_text('let token = "";\n')
    (root / "iina-plugin/Info.json").write_text(
        json.dumps(
            {
                "name": "Live Translation",
                "version": "1.4.0",
                "entry": "main.js",
                "identifier": "test.local.translation",
            }
        )
    )
    package = root / "live_translation"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("")
    return root


@pytest.fixture(autouse=True)
def no_native_or_credential_operations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Build unit tests must not execute native tools or touch credentials")

    monkeypatch.setattr(builder, "run", forbidden)
    monkeypatch.setattr(builder, "sign_local", forbidden)
    monkeypatch.setattr(builder.subprocess, "check_output", forbidden)


def test_release_payload_excludes_user_data_and_build_inputs(tmp_path):
    source = source_tree(tmp_path / "source")
    for relative in (
        "iina-token",
        ".env",
        ".venv/secret.py",
        "subtitles/private.json",
        "installer/debug.log",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private fixture")
    destination = tmp_path / "payload"
    hashes = builder.copy_payload(source, destination)
    assert set(hashes) == {*builder.REQUIRED_PAYLOAD, "live_translation/__init__.py"}
    assert not (destination / "iina-token").exists()
    assert not (destination / "subtitles").exists()
    assert all(len(digest) == 64 for digest in hashes.values())


def test_release_payload_rejects_an_installed_secret_bearing_plugin(tmp_path):
    source = source_tree(tmp_path / "source")
    (source / "iina-plugin/main.js").write_text('let token = "fixture-installed-token";\n')
    with pytest.raises(RuntimeError, match="installed plugin"):
        builder.payload_files(source)


def test_release_payload_rejects_a_symlink_outside_the_checkout(tmp_path):
    source = source_tree(tmp_path / "source")
    external = tmp_path / "private.py"
    external.write_text("private fixture")
    (source / "live_translation/escape.py").symlink_to(external)
    with pytest.raises(RuntimeError, match="escapes the checkout"):
        builder.payload_files(source)


def test_new_quality_modules_are_included_without_machine_data(tmp_path):
    source = source_tree(tmp_path / "source")
    quality = source / "live_translation/translation_quality.py"
    quality.write_text("QUALITY_VERSION = 1\n")
    hashes = builder.copy_payload(source, tmp_path / "payload")
    assert hashes["live_translation/translation_quality.py"] == builder.file_hash(quality)


def test_standalone_plugin_is_a_reproducible_token_free_iina_zip(tmp_path):
    source = source_tree(tmp_path / "source")
    (source / "iina-plugin/iina-token").write_text("private excluded fixture")
    (source / "iina-plugin/private.json").write_text("private excluded fixture")
    first, second = tmp_path / "first.iinaplgz", tmp_path / "second.iinaplgz"
    builder.pack_plugin(source, first)
    builder.pack_plugin(source, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {
            "Info.json",
            "main.js",
            "sidebar.html",
            "sidebar.css",
            "sidebar.js",
            "LICENSE",
        }
        assert json.loads(archive.read("Info.json"))["entry"] == "main.js"
        assert b'let token = "";' in archive.read("main.js")
        assert all("private" not in name for name in archive.namelist())


@pytest.mark.parametrize("resource", ["../private.html", "/private.html", "missing.html"])
def test_standalone_plugin_rejects_unsafe_declared_ui_resources(tmp_path, resource):
    source = source_tree(tmp_path / "source")
    info = source / "iina-plugin/Info.json"
    body = json.loads(info.read_text())
    body["preferencesPage"] = resource
    info.write_text(json.dumps(body))
    with pytest.raises(RuntimeError, match="resource"):
        builder.pack_plugin(source, tmp_path / "package.iinaplgz")


def test_ad_hoc_signature_never_claims_developer_id_or_uses_timestamp_service(tmp_path):
    with mock.patch.object(builder, "run") as run:
        builder.sign_app(tmp_path / "Fixture.app", "-")
    command = run.call_args_list[0].args
    assert "--timestamp=none" in command
    assert "--options" in command and "runtime" in command
    assert run.call_count == 2


def test_developer_id_requires_observed_timestamp_team_and_hardened_runtime(tmp_path):
    details = subprocess.CompletedProcess(
        [],
        0,
        stdout="",
        stderr="flags=0x10000(runtime)\nAuthority=Developer ID Application: Example (ABCDE12345)\nTeamIdentifier=ABCDE12345\nTimestamp=2026-11-17\n",
    )
    keychain = tmp_path / "release.keychain-db"
    with mock.patch.object(builder, "run", side_effect=[mock.Mock(), mock.Mock(), details]) as run:
        builder.sign_app(
            tmp_path / "Fixture.app", "Developer ID Application: Example (ABCDE12345)", keychain
        )
    assert "--timestamp" in run.call_args_list[0].args
    assert ("--keychain", str(keychain)) == run.call_args_list[0].args[-3:-1]


def test_unexpected_certificate_type_fails_before_signing():
    with pytest.raises(RuntimeError, match="Developer ID Application"):
        builder.signing_arguments("Apple Development: fixture")


def test_untimestamped_developer_id_signature_is_rejected(tmp_path):
    details = subprocess.CompletedProcess(
        [],
        0,
        stdout="",
        stderr="Authority=Developer ID Application: Example (ABCDE12345)\nTeamIdentifier=ABCDE12345\nflags=0x10000(runtime)\n",
    )
    with (
        mock.patch.object(builder, "run", side_effect=[mock.Mock(), mock.Mock(), details]),
        pytest.raises(RuntimeError, match="timestamped"),
    ):
        builder.sign_app(tmp_path / "Fixture.app", "Developer ID Application: Example (ABCDE12345)")


SUBMISSION = "12345678-1234-1234-1234-123456789abc"


def notary_result(status="Accepted"):
    return subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"id": SUBMISSION, "status": status}), stderr=""
    )


def notary_log(issues=None):
    return subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps({"jobId": SUBMISSION, "status": "Accepted", "issues": issues}),
        stderr="",
    )


def test_notary_acceptance_requires_json_status_and_reviews_log(tmp_path):
    with mock.patch.object(
        builder, "run", side_effect=[notary_result(), notary_log([{"severity": "warning"}])]
    ) as run:
        result = builder.notarize(
            tmp_path / "release.zip", "profile-fixture", tmp_path / "temporary.keychain-db"
        )
    assert result["status"] == "Accepted" and result["submission_id"] == SUBMISSION
    assert result["warnings"] == 1 and len(result["log_sha256"]) == 64
    assert "--output-format" in run.call_args_list[0].args
    assert "--timeout" in run.call_args_list[0].args
    assert "--keychain" in run.call_args_list[0].args
    assert run.call_args_list[1].args[1:4] == ("notarytool", "log", SUBMISSION)


@pytest.mark.parametrize("status", ["Invalid", "In Progress", None])
def test_successful_cli_exit_is_not_accepted_notarization(tmp_path, status):
    with (
        mock.patch.object(builder, "run", side_effect=[notary_result(status), notary_log()]),
        pytest.raises(RuntimeError, match="did not accept"),
    ):
        builder.notarize(tmp_path / "release.zip", "profile-fixture")


def test_nonzero_notary_exit_with_json_still_fetches_rejection_log(tmp_path):
    failure = subprocess.CalledProcessError(
        65, ["notarytool"], output=json.dumps({"id": SUBMISSION, "status": "Invalid"})
    )
    with (
        mock.patch.object(builder, "run", side_effect=[failure, notary_log()]) as run,
        pytest.raises(RuntimeError, match=SUBMISSION),
    ):
        builder.notarize(tmp_path / "release.zip", "profile-fixture")
    assert run.call_count == 2


@pytest.mark.parametrize(
    "output", ["not json", "[]", '{"status":"Accepted"}', '{"id":"bad","status":"Accepted"}']
)
def test_malformed_notary_proof_is_rejected(tmp_path, output):
    with (
        mock.patch.object(
            builder, "run", return_value=subprocess.CompletedProcess([], 0, stdout=output)
        ),
        pytest.raises(RuntimeError, match="valid submission"),
    ):
        builder.notarize(tmp_path / "release.zip", "profile-fixture")


def test_notary_error_issues_are_not_hidden_by_accepted_status(tmp_path):
    with (
        mock.patch.object(
            builder, "run", side_effect=[notary_result(), notary_log([{"severity": "error"}])]
        ),
        pytest.raises(RuntimeError, match="still reports an error"),
    ):
        builder.notarize(tmp_path / "release.zip", "profile-fixture")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"jobId": SUBMISSION, "status": "Invalid"},
        {"jobId": "another-submission", "status": "Accepted"},
    ],
)
def test_accepted_notarization_requires_matching_notary_log(tmp_path, body):
    log = subprocess.CompletedProcess([], 0, stdout=json.dumps(body))
    with (
        mock.patch.object(builder, "run", side_effect=[notary_result(), log]),
        pytest.raises(RuntimeError, match="does not match"),
    ):
        builder.notarize(tmp_path / "release.zip", "profile-fixture")


def test_installer_zip_validation_rejects_missing_frontend_and_unsafe_paths(tmp_path):
    archive_path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside", "unsafe fixture")
    with pytest.raises(RuntimeError, match="integrity"):
        builder.verify_app_archive(archive_path)


def test_notarized_app_checks_ticket_signature_and_gatekeeper(tmp_path):
    with mock.patch.object(builder, "run") as run:
        builder.verify_notarized_app(tmp_path / "Fixture.app")
    commands = [call.args for call in run.call_args_list]
    assert commands[0][:3] == ("/usr/bin/xcrun", "stapler", "validate")
    assert any(command[0] == "/usr/bin/codesign" for command in commands)
    assert any(
        command[:4] == ("/usr/sbin/spctl", "--assess", "--type", "execute") for command in commands
    )


@pytest.fixture
def simulated_build(tmp_path, monkeypatch):
    source = source_tree(tmp_path / "source")
    (source / "installer/Installer.swift").write_text("// frontend fixture\n")
    (source / "installer/bootstrap.sh").write_text("#!/bin/bash\n")
    monkeypatch.setattr(builder, "SOURCE", source)
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        builder.subprocess, "check_output", lambda *args, **kwargs: "/fixture/SDK\n"
    )
    commands = []

    def run(*args, **kwargs):
        commands.append(args)
        if "swiftc" in args or args[:2] == ("/usr/bin/hdiutil", "create"):
            Path(args[-1]).write_bytes(b"native build fixture")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def zip_app(app, destination):
        with zipfile.ZipFile(destination, "w") as archive:
            for path in app.rglob("*"):
                if path.is_file():
                    archive.write(path, builder.APP_NAME + "/" + path.relative_to(app).as_posix())

    monkeypatch.setattr(builder, "run", run)
    monkeypatch.setattr(builder, "zip_app", zip_app)
    args = argparse.Namespace(
        version="1.4.0",
        output_dir=tmp_path / "dist",
        sign_identity="-",
        notary_profile=None,
        keychain=None,
    )
    return args, commands


def test_ad_hoc_build_reports_truthfully_and_emits_complete_consistent_artifact_set(
    simulated_build,
):
    args, commands = simulated_build
    result = builder.build(args)
    assert set(result) == {"app", "dmg", "zip", "plugin", "verification", "checksums"}
    verification = json.loads(result["verification"].read_text())
    assert verification["notarized"] is False and verification["gatekeeper_assessed"] is False
    assert verification["signing"] == "ad-hoc"
    assert not any(
        "notarytool" in command or "stapler" in command or "/usr/sbin/spctl" in command
        for command in commands
    )
    for line in result["checksums"].read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert digest == builder.file_hash(args.output_dir / name)


def test_notarized_build_staples_and_validates_both_app_and_dmg(simulated_build):
    args, commands = simulated_build
    args.sign_identity = "Developer ID Application: Example (ABCDE12345)"
    args.notary_profile = "profile-fixture"
    with (
        mock.patch.object(builder, "sign_app"),
        mock.patch.object(
            builder, "notarize", return_value={"status": "Accepted", "submission_id": SUBMISSION}
        ) as notarize,
    ):
        result = builder.build(args)
    report = json.loads(result["verification"].read_text())
    assert report["notarized"] is True and report["gatekeeper_assessed"] is True
    assert notarize.call_count == 2
    for suffix in (".app", ".dmg"):
        assert any(
            command[:3] == ("/usr/bin/xcrun", "stapler", "staple") and command[-1].endswith(suffix)
            for command in commands
        )
        assert any(
            command[:3] == ("/usr/bin/xcrun", "stapler", "validate")
            and command[-1].endswith(suffix)
            for command in commands
        )


def test_failed_notarization_never_replaces_existing_release_files(simulated_build):
    args, _commands = simulated_build
    args.output_dir.mkdir()
    old = args.output_dir / (builder.ARTIFACT + ".dmg")
    old.write_bytes(b"existing verified release fixture")
    args.sign_identity = "Developer ID Application: Example (ABCDE12345)"
    args.notary_profile = "profile-fixture"
    with (
        mock.patch.object(builder, "sign_app"),
        mock.patch.object(builder, "notarize", side_effect=RuntimeError("Apple rejected fixture")),
        pytest.raises(RuntimeError, match="rejected fixture"),
    ):
        builder.build(args)
    assert old.read_bytes() == b"existing verified release fixture"
    assert not (args.output_dir / "SHA256SUMS").exists()


@pytest.mark.parametrize(
    "environment,expected",
    [
        ({}, "local"),
        ({"GITHUB_ACTIONS": "true"}, "-"),
        ({"GITHUB_ACTIONS": "false"}, "local"),
        ({"MACOS_SIGNING_IDENTITY": "-"}, "-"),
        ({"MACOS_SIGNING_IDENTITY": "local", "GITHUB_ACTIONS": "true"}, "local"),
        (
            {
                "MACOS_SIGNING_IDENTITY": "Developer ID Application: Example (ABCDE12345)",
                "GITHUB_ACTIONS": "true",
            },
            "Developer ID Application: Example (ABCDE12345)",
        ),
    ],
)
def test_signing_default_preserves_explicit_identity_and_ci_ad_hoc(environment, expected):
    assert builder.default_sign_identity(environment) == expected


def local_signature_fixture():
    return {
        "mode": "local-certificate",
        "authority": "Live Translation Local Build",
        "certificate_sha1": "12" * 20,
        "certificate_sha256": "34" * 32,
        "designated_requirement": 'identifier "test.installer" and certificate leaf = H"' + "12" * 20 + '"',
    }


def test_local_app_dispatch_uses_dedicated_signer_without_developer_id_checks(tmp_path):
    app = tmp_path / "Installer.app"
    signature = local_signature_fixture()
    with mock.patch.object(builder, "sign_local", return_value=signature) as local:
        assert builder.sign_app(app, "local") == signature
    local.assert_called_once_with(app)


def test_local_build_signs_app_and_disk_image_and_reports_without_apple_claims(simulated_build):
    args, commands = simulated_build
    args.sign_identity = "local"
    signature = local_signature_fixture()
    with mock.patch.object(builder, "sign_local", return_value=signature) as local:
        result = builder.build(args)
    assert local.call_count == 2
    app_call, image_call = local.call_args_list
    assert app_call.args[0].suffix == ".app" and app_call.kwargs == {}
    assert image_call.args[0].suffix == ".dmg" and image_call.kwargs == {"hardened": False}
    report = json.loads(result["verification"].read_text())
    assert report["signing"] == "local-certificate"
    assert report["local_signature"] == signature
    assert report["local_disk_image_signature"] == signature
    assert report["notarized"] is False and report["gatekeeper_assessed"] is False
    assert report["notarization"] == {}
    assert not any(
        "notarytool" in command or "stapler" in command or "/usr/sbin/spctl" in command
        for command in commands
    )
    for line in result["checksums"].read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert digest == builder.file_hash(args.output_dir / name)
    with zipfile.ZipFile(result["zip"]) as archive:
        metadata = json.loads(
            archive.read(builder.APP_NAME + "/Contents/Resources/installer-build.json")
        )
    assert metadata["signing"] == "local-certificate"


@pytest.mark.parametrize("option", ["notary_profile", "keychain"])
def test_local_build_rejects_apple_notary_or_external_keychain_before_any_native_call(
    simulated_build, option,
):
    args, commands = simulated_build
    args.sign_identity = "local"
    setattr(args, option, "fixture-notary" if option == "notary_profile" else Path("/fixture/keychain"))
    with pytest.raises(RuntimeError, match="Developer ID|dedicated keychain"):
        builder.build(args)
    assert commands == []
    assert not args.output_dir.exists()


@pytest.mark.parametrize("failure_suffix", [".app", ".dmg"])
def test_failed_local_signature_never_replaces_existing_release_files(
    simulated_build, failure_suffix,
):
    args, _commands = simulated_build
    args.output_dir.mkdir()
    old = args.output_dir / (builder.ARTIFACT + ".dmg")
    old.write_bytes(b"existing verified release fixture")
    args.sign_identity = "local"

    def sign(bundle, **_kwargs):
        if bundle.suffix == failure_suffix:
            raise RuntimeError("local signing failed fixture")
        return local_signature_fixture()

    with (
        mock.patch.object(builder, "sign_local", side_effect=sign),
        pytest.raises(RuntimeError, match="local signing failed fixture"),
    ):
        builder.build(args)
    assert old.read_bytes() == b"existing verified release fixture"
    assert not (args.output_dir / "SHA256SUMS").exists()


def test_local_build_requires_certificate_metadata_before_publishing(simulated_build):
    args, _commands = simulated_build
    args.sign_identity = "local"
    with (
        mock.patch.object(builder, "sign_local", return_value=None),
        pytest.raises(RuntimeError, match="certificate identity"),
    ):
        builder.build(args)
    assert not (args.output_dir / "SHA256SUMS").exists()
