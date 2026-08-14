#!/usr/bin/env bash
# v2 adaptation of v1 context-pack.sh: router emits deterministic minimal context paths.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
TASK="${1:?usage: context-pack.sh T<n>}"
source .ai/scripts/python-command.sh
PYTHON_CMD="$(ai_kit_python_command)" || { echo "AI-Kit: Python runtime not found" >&2; exit 127; }
exec "$PYTHON_CMD" .ai/engine/ai_kit.py route "$TASK"
