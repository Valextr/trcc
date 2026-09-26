#!/bin/bash
# Launch the AIO LCD dashboard inside the hud container.
#
# Derives the host timezone and passes it into the container so
# datetime.now() renders local time instead of UTC.
#
# Machine-specific settings come from the operator's environment /
# local systemd unit, so this script stays clone-agnostic (no
# hardcoded home paths, device IDs, or container names):
#   TRCC_CONTAINER   container name (default: systemd-hud — the podman
#                    quadlet names its container systemd-<unit>)
#   TRCC_DIR         repo path AS SEEN INSIDE the container (default:
#                    $SCRIPT_DIR, i.e. the host path — correct when the
#                    host and container home paths match; override when
#                    they differ)
#   PC_POWER_LIMIT_W whole-PC PSU rating in watts (optional; unset ->
#                    the dashboard shows wattage only)
#   TRCC_DEVICE      USB VID:PID of the display (optional; the --device
#                    CLI arg on the dashboard takes priority)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse timezone from the HOST's /etc/localtime
TZ=$(readlink -f /etc/localtime | sed 's|.*/zoneinfo/||')

CONTAINER="${TRCC_CONTAINER:-systemd-hud}"
TRCC_DIR="${TRCC_DIR:-$SCRIPT_DIR}"

# Optional per-machine env, omitted entirely when unset so a fresh
# clone (no local unit) still runs with neutral defaults.
EXTRA_ENV=()
if [ -n "${PC_POWER_LIMIT_W:-}" ]; then
  EXTRA_ENV+=(-e "PC_POWER_LIMIT_W=${PC_POWER_LIMIT_W}")
fi
if [ -n "${TRCC_DEVICE:-}" ]; then
  EXTRA_ENV+=(-e "TRCC_DEVICE=${TRCC_DEVICE}")
fi

exec podman exec \
  -e TZ="$TZ" \
  "${EXTRA_ENV[@]}" \
  "$CONTAINER" \
  "$TRCC_DIR/.venv/bin/python3" -u \
  "$TRCC_DIR/dashboard.py" \
  "$@"
