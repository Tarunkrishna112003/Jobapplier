#!/usr/bin/env bash
# Starts the local web UI on http://127.0.0.1:8765
cd "$(dirname "$0")" || exit 1
echo "Jobapplier UI → http://127.0.0.1:8765"
exec .venv/bin/python -m jobapplier.server
