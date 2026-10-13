#!/usr/bin/env bash
# W6: create the two IoT identities (certificates and policies). The logic is in iot_up.py.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=
# shellcheck disable=SC1091
[ -f .local/config.env ] && . .local/config.env
exec "${PYTHON:-python3}" deploy/iot_up.py "$@"
