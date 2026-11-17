"""Release payload selection must never include a local installation's data."""

import importlib.util
from pathlib import Path

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
    package = root / "live_translation"
    package.mkdir()
    (package / "__init__.py").write_text("")
    return root


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
