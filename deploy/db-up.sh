#!/usr/bin/env bash
# W5: create the private subnets, SG-db and the private RDS. The logic is in db_up.py.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=
# shellcheck disable=SC1091
[ -f .local/config.env ] && . .local/config.env
exec "${PYTHON:-python3}" deploy/db_up.py "$@"
