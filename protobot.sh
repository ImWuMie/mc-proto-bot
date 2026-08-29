#!/bin/sh
# ProtoBot launcher for the portable release.
# No install step: anything you would type as "protobot ..." goes after this
# script, e.g. "./protobot.sh run", "./protobot.sh login".
# Only Python 3.12+ is required.
set -e

if command -v python3 >/dev/null 2>&1; then
    exec python3 -m protobot.cli_app "$@"
fi
if command -v python >/dev/null 2>&1; then
    exec python -m protobot.cli_app "$@"
fi

echo "[error] Python 3.12 or newer is required but not found." >&2
echo "        Install it from https://www.python.org/downloads/ and run this again." >&2
exit 1
