#!/usr/bin/env bash
# AI-Kit Full Dispatch: take one task from its current status all the way to `done`.
#
# Two lifecycles exist and they need different commands:
#
#   governed (schema v5, every task created by `add-task`/`plan`): QA, review
#     and close are control-plane-only. `approve` and `transition close` are
#     refused for these tasks ("qa-pass is control-plane-only for governed
#     assigned tasks"), which used to make this script fail at its QA step for
#     every task it had just dispatched. The supported chain is
#     `dispatch` -> `pipeline`, where pipeline runs authoritative QA, dispatches
#     an independent reviewer, and closes delivery.
#
#   legacy (pre-v5 tasks with no governance baseline): the original
#     verify -> approve -> approve -> close flow still applies.
#
# REASON is only used on the legacy path; on the governed path QA and review
# verdicts come from the configured runners, never from a canned string here.
set -euo pipefail

TASK_ID="${1:?Usage: $0 TASK_ID RUNNER [REASON]}"
RUNNER="${2:?Usage: $0 TASK_ID RUNNER [REASON]}"
REASON="${3:-Auto-approved after successful verification}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_KIT="$SCRIPT_DIR/ai-kit"
source "$SCRIPT_DIR/python-command.sh"
PYTHON_CMD="$(ai_kit_python_command)" || { echo "AI-Kit: Python runtime not found" >&2; exit 127; }

MODE="$("$AI_KIT" show "$TASK_ID" | "$PYTHON_CMD" -c '
import json, sys
task = json.load(sys.stdin).get("task", {})
print("legacy" if task.get("governance_baseline") is None else "governed")
')"

echo "🚀 Dispatching $TASK_ID to $RUNNER ($MODE lifecycle)..."
"$AI_KIT" dispatch "$TASK_ID" --runner "$RUNNER"

if [[ "$MODE" == "governed" ]]; then
    echo ""
    echo "🔁 Running control-plane pipeline (authoritative QA → independent review → delivery close)..."
    # pipeline resumes from whatever status the executor left behind, so the
    # dispatch above is not repeated. It refuses to let QA or review run under
    # the executor's own runner/model, and never fabricates a verdict.
    "$AI_KIT" pipeline "$TASK_ID"
    echo ""
    echo "🎉 Task $TASK_ID complete!"
    "$AI_KIT" status
    exit 0
fi

echo ""
echo "✅ Dispatch complete. Waiting 2s before verification..."
sleep 2

echo ""
echo "🔍 Verifying $TASK_ID..."
# `ai-kit verify` exits non-zero unless the report says passed (a FAIL, or an
# INCONCLUSIVE run where no functional check was configured at all). It used to
# exit 0 regardless, which made this guard a no-op: a task whose checks failed
# was auto-approved through QA and review and closed at `done`. The report is
# also re-read below so this stays correct even if the exit contract changes.
VERIFY_REPORT="$("$AI_KIT" verify "$TASK_ID")" || {
    echo "$VERIFY_REPORT"
    echo "❌ Verification did not pass. Stopping here (task stays at implementation-complete)."
    exit 1
}
echo "$VERIFY_REPORT"
if ! printf '%s' "$VERIFY_REPORT" | grep -q '"passed": true'; then
    echo "❌ Verification did not pass. Stopping here (task stays at implementation-complete)."
    exit 1
fi

echo ""
echo "✅ Verification passed. Proceeding to approvals..."

echo ""
echo "📋 QA approval for $TASK_ID..."
"$AI_KIT" approve "$TASK_ID" --role qa --reason "$REASON"

echo ""
echo "👀 Review approval for $TASK_ID..."
"$AI_KIT" approve "$TASK_ID" --role review --reason "$REASON"

echo ""
echo "🔒 Closing $TASK_ID..."
"$AI_KIT" transition "$TASK_ID" close --actor system --detail "Auto-closed by dispatch-full"

echo ""
echo "🎉 Task $TASK_ID complete!"
"$AI_KIT" status
