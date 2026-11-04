#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ACTION="${1:-start}"
exec ./inmusic-control.sh "$ACTION" public
