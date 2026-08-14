#!/usr/bin/env bash
# Health check for the v2 nested agent and skill contracts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
fail=0
warn=0
ok() { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }
note() { printf '  WARN %s\n' "$1"; warn=$((warn + 1)); }

echo "AI-Kit v2 doctor - $ROOT"
for file in AGENTS.md .ai-config/kit.yaml .ai-config/rules.yaml .ai-config/registry.yaml \
  .ai/engine/ai_kit.py .ai/scripts/check-kit.sh .ai/scripts/skills-for.sh .ai-work/state/current.json; do
  [[ -f "$file" ]] && ok "$file" || bad "$file missing"
done

source .ai/scripts/python-command.sh
PYTHON_CMD="$(ai_kit_python_command 2>/dev/null || true)"
if [[ -n "$PYTHON_CMD" ]]; then
  ok "python found: $PYTHON_CMD"
else
  bad "Python not found (set AI_KIT_PYTHON to an executable if PATH is ambiguous)"
fi

if bash .ai/scripts/check-kit.sh; then
  ok "v2 contracts valid"
else
  bad "v2 contract validation failed"
fi

if [[ -f .ai-work/state/workflow.json && -n "$PYTHON_CMD" ]]; then
  "$PYTHON_CMD" .ai/engine/ai_kit.py validate >/dev/null && ok "workflow state valid" || bad "workflow state invalid"
elif [[ -f .ai-work/state/workflow.json ]]; then
  bad "workflow state cannot be validated without Python"
else
  note "workflow state not initialized (run .ai/scripts/bootstrap.sh)"
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  hooks=$(git config --get core.hooksPath || true)
  [[ "$hooks" == ".githooks" ]] && ok "Git hooks configured" || note "Git hooks not configured; run bootstrap.sh"
fi

configured_checks=0
for key in test_command typecheck_command build_command lint_command; do
  cmd="$(awk -v key="$key" '$1 == key ":" {sub(/^[^:]*:[[:space:]]*/, ""); print; exit}' .ai-config/kit.yaml)"
  # "true" is the kit.yaml sentinel for "no check configured" (ai_kit.py's
  # cmd_verify skips it rather than running it) -- it is not a real command.
  if [[ -n "$cmd" && "$cmd" != "true" ]]; then
    ok "$key configured: $cmd"
    configured_checks=$((configured_checks + 1))
  else
    # An individual gap is fine (not every project has a typecheck step);
    # having none at all is not -- that is the case cmd_verify reports as
    # inconclusive, which blocks `ai-kit pipeline` outright.
    note "$key not configured (verify will skip this check)"
  fi
done
if [[ "$configured_checks" -eq 0 ]]; then
  bad "no verification command configured at all — 'ai-kit verify' cannot check functional correctness (reports INCONCLUSIVE) and 'ai-kit pipeline' will refuse to run. Fix: ai-kit onboard --apply, or edit .ai-config/kit.yaml's verification section"
fi

# project.stack drives skill routing: with it empty, a task tagged with its own
# domain loads every technology skill in that domain (e.g. a backend task pulls
# PHP + Python + FastAPI + both NestJS skills at once), which is the opposite of
# the minimal_context rule AGENTS.md mandates.
stack="$(awk '
  /^project:/ { in_project=1; next }
  in_project && /^[^ ]/ { in_project=0 }
  in_project && /stack:/ {
    gsub(/.*stack:[[:space:]]*/, ""); gsub(/[\[\]]/, ""); gsub(/,/, " ")
    gsub(/[[:space:]]+/, " "); sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "")
    print; exit
  }
' .ai-config/kit.yaml 2>/dev/null || true)"
if [[ -n "$stack" ]]; then
  ok "project.stack configured: $stack"
else
  bad "project.stack is empty in .ai-config/kit.yaml — skill routing cannot narrow to this project's technologies and will load every skill in a matched domain. Fix: ai-kit onboard --apply, or set project.stack"
fi

echo "summary: $fail failure(s), $warn warning(s)"
[[ "$fail" -eq 0 ]]
