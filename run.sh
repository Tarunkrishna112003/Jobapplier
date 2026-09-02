#!/usr/bin/env bash
# Convenience wrapper so you don't have to remember the venv path.
cd "$(dirname "$0")" && exec .venv/bin/python -m jobapplier.cli "$@"
