#!/usr/bin/env bash
# Create a task. This previously ran `ai-kit ready`, making it a byte-for-byte
# duplicate of next-task.sh -- it listed existing runnable work and created
# nothing, despite the name.
#
# Usage:
#   new-task.sh T3 "Add OAuth login" backend build "Login works end to end" [needs...]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ $# -lt 5 ]]; then
  cat >&2 <<'USAGE'
usage: new-task.sh <id> <title> <owner> <phase> <acceptance> [needs...]

  id          task id, e.g. T3
  title       short description
  owner       role that owns it (see .ai/agents/)
  phase       plan | build | verify | release
  acceptance  one observable acceptance criterion
  needs...    zero or more task ids this depends on

To list work that is ready to start instead, use next-task.sh.
USAGE
  exit 2
fi

ID="$1"; TITLE="$2"; OWNER="$3"; PHASE="$4"; ACCEPTANCE="$5"; shift 5
source .ai/scripts/python-command.sh
PYTHON_CMD="$(ai_kit_python_command)" || { echo "AI-Kit: Python runtime not found" >&2; exit 127; }

args=(add-task "$ID" --title "$TITLE" --owner "$OWNER" --phase "$PHASE" --acceptance "$ACCEPTANCE")
[[ $# -gt 0 ]] && args+=(--needs "$@")

exec "$PYTHON_CMD" .ai/engine/ai_kit.py "${args[@]}"
