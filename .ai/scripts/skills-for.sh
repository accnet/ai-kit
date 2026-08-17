#!/usr/bin/env bash
# Print v2 knowledge documents relevant to a role and optional stack override.
# Stack is resolved in priority order:
#   1. Explicit second argument (override)
#   2. project.stack from installed .ai-config/kit.yaml (or the kit's
#      .ai/install/config/kit.yaml seed when run from the source repository)
#   3. Empty (no stack-specific filtering; uses registry owners)
#
# Environment variable override for testing:
#   SKILLS_FOR_ROOT=/some/path  use this as the repo root instead of auto-detect
set -euo pipefail

ROOT="${SKILLS_FOR_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$ROOT"
role="${1:-any}"
override="${2:-}"
CONFIG_ROOT=".ai-config"
if [[ ! -d "$CONFIG_ROOT" && -d ".ai/install/config" ]]; then
  CONFIG_ROOT=".ai/install/config"
fi

# Read project.stack from kit.yaml when no explicit override is given.
# Uses only awk to stay dependency-free.
kit_stack=""
if [[ -z "$override" && -f "$CONFIG_ROOT/kit.yaml" ]]; then
  # Extract the value of "stack:" under the "project:" block.
  # Handles: stack: [openai, rag]  or  stack: []  or missing key.
  kit_stack="$(awk '
    /^project:/ { in_project=1; next }
    in_project && /^[^ ]/ { in_project=0 }
    in_project && /stack:/ {
      gsub(/.*stack:[[:space:]]*/, "")
      gsub(/[\[\]]/, "")
      gsub(/,/, " ")
      gsub(/[[:space:]]+/, " ")
      sub(/^[[:space:]]+/, "")
      sub(/[[:space:]]+$/, "")
      print
      exit
    }
  ' "$CONFIG_ROOT/kit.yaml" 2>/dev/null || true)"
fi

# Determine the effective stack (space-separated list or empty).
stack=""
if [[ -n "$override" ]]; then
  stack="${override//,/ }"
elif [[ -n "$kit_stack" ]]; then
  stack="${kit_stack//,/ }"
fi

# Resolve domains from registry owners when no stack override is active.
if [[ -z "$stack" ]]; then
  domains="$(awk -v role="$role" '
    $1 == role ":" {gsub(/.*\[/, ""); gsub(/\].*/, ""); gsub(/,/, " "); print; found=1}
    END {if (!found) print "any"}
  ' "$CONFIG_ROOT/registry.yaml")"
else
  # Each stack value may be a domain name (e.g. "ai", "backend") or a
  # technology name (e.g. "openai", "rag"). Resolve both.
  domains="$stack"
fi

# registry.yaml's stack_skills maps a skill folder to the stack tags it serves,
# which is how ai_kit.py's router resolves `project.stack`. Without consulting
# it, this script could only match a tag against a folder's own directory name
# or its domain -- so `stack: [nestjs]` found nothing at all (the folders are
# `nestjs-core`/`nestjs-data-access`), and the same held for compose, vite and
# vitest. `ai-kit route` returned those skills while this script returned none,
# for the same project.stack.
registry_matches=""
if [[ -n "$stack" && -f "$CONFIG_ROOT/registry.yaml" ]]; then
  registry_matches="$(awk -v want="$stack" '
    /^stack_skills:/ { in_section=1; next }
    in_section && /^[^ 	]/ { in_section=0 }
    in_section && match($0, /^  [^:]+:[[:space:]]*\{path:[[:space:]]*[^,}]+,[[:space:]]*stack:[[:space:]]*\[[^]]*\]/) {
      line = $0
      path = line; sub(/^[^{]*\{path:[[:space:]]*/, "", path); sub(/[[:space:]]*,.*$/, "", path)
      tags = line; sub(/^.*stack:[[:space:]]*\[/, "", tags); sub(/\].*$/, "", tags)
      gsub(/,/, " ", tags)
      n = split(tags, tag, /[[:space:]]+/)
      m = split(want, wanted, /[[:space:]]+/)
      for (i = 1; i <= n; i++) for (j = 1; j <= m; j++)
        if (tag[i] != "" && tag[i] == wanted[j]) { print path; next }
    }
  ' "$CONFIG_ROOT/registry.yaml" 2>/dev/null || true)"
fi

# Emit skill folder paths, de-duplicated.
{ [[ -n "$registry_matches" ]] && printf '%s
' "$registry_matches"
for domain_or_tech in $domains; do
  if [[ "$domain_or_tech" == "any" ]]; then
    find .ai/skills -mindepth 2 -maxdepth 2 -type d ! -path '.ai/skills/core/*' | sort
  elif [[ -d ".ai/skills/$domain_or_tech" ]]; then
    # It's a domain name — list all technology skill folders under it.
    find ".ai/skills/$domain_or_tech" -mindepth 1 -maxdepth 1 -type d | sort
  else
    # Treat as a technology name — find the matching folder under any domain.
    find .ai/skills -mindepth 2 -maxdepth 2 -type d \
      -name "$domain_or_tech" ! -path '.ai/skills/core/*' | sort
  fi
done
} | awk 'NF && !seen[$0]++' | while IFS= read -r folder; do
  entrypoint="$folder/overview.md"
  if [[ -f "$folder/skill.meta.yaml" ]]; then
    configured_entrypoint="$(awk -F': ' '/^entrypoint:/ {print $2}' "$folder/skill.meta.yaml" | head -n1)"
    if [[ -n "$configured_entrypoint" && -f "$configured_entrypoint" ]]; then
      entrypoint="$configured_entrypoint"
    fi
  fi
  # A folder selected via stack_skills is already known to serve one of the
  # requested tags; only folders found by directory-name matching still need
  # the name/domain check.
  if [[ -n "${stack:-}" ]] && ! grep -qxF "$folder" <<< "$registry_matches"; then
    name="$(basename "$folder")"
    domain="$(basename "$(dirname "$folder")")"
    if ! grep -Eiq "(^|[[:space:]])$name([[:space:]]|$)|(^|[[:space:]])$domain([[:space:]]|$)" <<< "$stack"; then
      continue
    fi
  fi
  printf '%s\n' "$entrypoint"
done

case "$role" in
  planner|researcher) core="requirements-intake skill-router" ;;
  architect) core="refactoring api-contract" ;;
  backend) core="api-contract observability" ;;
  frontend) core="frontend-core test-and-validation" ;;
  database) core="data-migration api-contract" ;;
  devops) core="deployment-infra observability" ;;
  qa) core="test-and-validation debugging" ;;
  reviewer) core="code-review api-contract" ;;
  security) core="security-review threat-modeling" ;;
  integration) core="integration-contracts webhooks-and-retries" ;;
  performance) core="performance-profiling observability" ;;
  scheduler) core="workflow-orchestration" ;;
  router) core="workflow-orchestration skill-router" ;;
  document) core="documentation-maintenance architecture-decisions" ;;
  release) core="release-management deployment-infra github-actions-ci" ;;
  *) core="skill-router" ;;
esac
for skill in $core; do
  path=".ai/skills/core/$skill/SKILL.md"
  if [[ -f "$path" ]]; then printf '%s\n' "$path"; fi
done
