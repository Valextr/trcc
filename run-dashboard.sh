#!/bin/bash
# Derive the host timezone from /etc/localtime and launch the dashboard.
# This avoids timdatectl (which needs system dbus) and works in systemd user context.
set -euo pipefail

# Parse timezone from the host's /etc/localtime symlink
TZ=$(readlink -f /etc/localtime | sed 's|.*/zoneinfo/||')

export TZ
export LD_LIBRARY_PATH=/usr/lib64/nvidia

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/.venv/bin/python3" "$SCRIPT_DIR/dashboard.py" "$@"