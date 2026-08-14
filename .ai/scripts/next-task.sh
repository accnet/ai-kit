#!/usr/bin/env bash
# v2 adaptation of v1 next-task.sh: Scheduler returns only dependency-ready work.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source .ai/scripts/python-command.sh
PYTHON_CMD="$(ai_kit_python_command)" || { echo "AI-Kit: Python runtime not found" >&2; exit 127; }
exec "$PYTHON_CMD" .ai/engine/ai_kit.py ready
