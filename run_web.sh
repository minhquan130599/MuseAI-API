#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
muse-web --host 127.0.0.1 --port 8787
