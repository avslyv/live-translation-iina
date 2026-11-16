#!/bin/bash
# Install the native-subtitle IINA mode. --help and --dry-run are side-effect free.
set -euo pipefail
cd "$(dirname "$0")"

INSTALL_PYTHON=""
for candidate in python3.13 python3.12 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
        INSTALL_PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$INSTALL_PYTHON" ]; then
    echo "Python 3.12+ is required. Install it with: brew install python@3.13" >&2
    exit 1
fi

exec "$INSTALL_PYTHON" tools/install_iina.py \
    --install-dependencies --download-models --start "$@"
