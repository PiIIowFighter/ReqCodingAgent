#!/usr/bin/env sh
set -eu
exec python3.11 -m evalsys.cli validate-all "$@"
