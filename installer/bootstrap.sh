#!/bin/bash
# Native installer entry point; requires only tools included with macOS.
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$SCRIPT_DIR/payload/installer" ]; then
    PAYLOAD_DIR="$SCRIPT_DIR/payload"
else
    PAYLOAD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
MANIFEST="$PAYLOAD_DIR/installer/python.lock.json"
if [ "$(/usr/bin/uname -m)" != arm64 ]; then
    echo '{"type":"error","message":"An Apple silicon Mac is required."}'
    exit 1
fi
if [ "${1:-}" = --dry-run ]; then
    echo '{"type":"progress","stage":"preflight","label":"Checking installation plan","detail":"No files or settings will be changed.","progress":0.1}'
    echo '{"type":"complete","message":"Ready to install Python, IINA, FFmpeg, Ollama, the plugin and local models. This was a read-only preview."}'
    exit 0
fi
VERSION="$(/usr/bin/plutil -extract version raw -o - "$MANIFEST")"
ASSET_URL="$(/usr/bin/plutil -extract url raw -o - "$MANIFEST")"
EXPECTED_SHA="$(/usr/bin/plutil -extract sha256 raw -o - "$MANIFEST")"
SUPPORT_DIR="$HOME/Library/Application Support/LiveTranslate"
CACHE_DIR="$HOME/Library/Caches/LiveTranslationInstaller"
PYTHON_DIR="$SUPPORT_DIR/engines/python-$VERSION"
PYTHON_BIN="$PYTHON_DIR/bin/python3"
mkdir -p "$CACHE_DIR" "$SUPPORT_DIR/engines"
if [ ! -x "$PYTHON_BIN" ]; then
    echo '{"type":"progress","stage":"python","label":"Preparing Python","detail":"Downloading a verified private Python runtime. System Python is unchanged.","progress":0.02}'
    ARCHIVE="$CACHE_DIR/python-$VERSION.tar.gz"
    if [ ! -f "$ARCHIVE" ] || [ "$(/usr/bin/shasum -a 256 "$ARCHIVE" | /usr/bin/awk '{print $1}')" != "$EXPECTED_SHA" ]; then
        /usr/bin/curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' \
            --retry 3 --retry-all-errors --connect-timeout 25 --max-time 1800 "$ASSET_URL" -o "$ARCHIVE.part"
        ACTUAL_SHA="$(/usr/bin/shasum -a 256 "$ARCHIVE.part" | /usr/bin/awk '{print $1}')"
        if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
            echo '{"type":"error","message":"Python download checksum did not match. Retry the installation."}'
            exit 1
        fi
        /bin/mv "$ARCHIVE.part" "$ARCHIVE"
    fi
    STAGING_DIR="$(/usr/bin/mktemp -d "$SUPPORT_DIR/engines/.python-install.XXXXXX")"
    trap '/bin/rm -rf "$STAGING_DIR"' EXIT
    # This archive is an immutable upstream release verified against its pinned digest.
    /usr/bin/tar -xzf "$ARCHIVE" -C "$STAGING_DIR"
    "$STAGING_DIR/python/bin/python3" -c 'import sys; assert sys.version_info[:2] == (3,13)'
    /bin/mv "$STAGING_DIR/python" "$PYTHON_DIR"
    /bin/rmdir "$STAGING_DIR"
    trap - EXIT
fi
exec "$PYTHON_BIN" "$PAYLOAD_DIR/installer/bootstrap.py" "$@"
