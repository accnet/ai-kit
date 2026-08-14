#!/usr/bin/env bash
# Validate AI-Kit v2 skill contracts and content quality markers.
#
# Environment variable override for testing:
#   CHECK_SKILLS_ROOT=/some/path  use this as the repo root instead of auto-detect
set -euo pipefail

ROOT="${CHECK_SKILLS_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$ROOT"
mode="${1:-all}"
fail=0

placeholder_re='PLACEHOLDER|not yet written|generic kit template'

bad() {
  echo "FAIL[$mode]: $1" >&2
  fail=1
}

is_list_field() {
  [[ "$1" =~ ^\[[^]]*\]$ ]]
}

validate_no_placeholder() {
  local file="$1"
  if grep -Eqi "$placeholder_re" "$file"; then
    bad "$file contains placeholder markers"
  fi
}

validate_core_skill() {
  local folder="$1"
  local file="$folder/SKILL.md"
  [[ -s "$file" ]] || { bad "$file missing or empty"; return; }
  validate_no_placeholder "$file"

  local fm_end
  fm_end="$(grep -n '^---$' "$file" | sed -n '2p' | cut -d: -f1 || true)"
  if [[ -z "$fm_end" ]]; then
    bad "$file missing closing front matter delimiter"
    return
  fi

  for key in name description version tier stack owner gates; do
    if ! head -n "$fm_end" "$file" | grep -Eq "^$key:"; then
      bad "$file missing front matter field '$key'"
    fi
  done
}

validate_technology_skill() {
  local folder="$1"
  local rel="${folder#./}"
  local domain name meta path_field entrypoint_field docs_field status_field deprecated_field

  domain="${rel#.ai/skills/}"
  domain="${domain%%/*}"
  name="$(basename "$folder")"

  for doc in overview patterns best-practices pitfalls examples; do
    [[ -s "$folder/$doc.md" ]] || bad "$folder/$doc.md missing or empty"
    [[ -f "$folder/$doc.md" ]] && validate_no_placeholder "$folder/$doc.md"
  done

  meta="$folder/skill.meta.yaml"
  [[ -s "$meta" ]] || { bad "$meta missing or empty"; return; }

  for field in name domain version owner reviewed_at entrypoint path documents; do
    if ! grep -Eq "^$field:" "$meta"; then
      bad "$meta missing required field '$field'"
    fi
  done

  path_field="$(awk -F': ' '/^path:/ {print $2}' "$meta" | head -n1)"
  entrypoint_field="$(awk -F': ' '/^entrypoint:/ {print $2}' "$meta" | head -n1)"
  docs_field="$(awk -F': ' '/^documents:/ {print $2}' "$meta" | head -n1)"
  status_field="$(awk -F': ' '/^status:/ {print $2}' "$meta" | head -n1)"
  deprecated_field="$(awk -F': ' '/^deprecated:/ {print $2}' "$meta" | head -n1)"

  [[ "$path_field" == "$rel" ]] || bad "$meta path mismatch (expected $rel, got $path_field)"
  [[ "$entrypoint_field" == "$rel/overview.md" || "$entrypoint_field" == .ai/skills/*/"$name"/overview.md ]] || bad "$meta entrypoint must resolve to skill overview (got $entrypoint_field)"
  [[ -f "$entrypoint_field" ]] || bad "$meta entrypoint file missing: $entrypoint_field"

  [[ -n "$status_field" ]] && [[ "$status_field" =~ ^(active|draft|experimental|deprecated)$ ]] || {
    [[ -n "$status_field" ]] && bad "$meta status must be one of active|draft|experimental|deprecated"
  }

  if [[ -n "$deprecated_field" && ! "$deprecated_field" =~ ^(true|false)$ ]]; then
    bad "$meta deprecated must be true or false"
  fi

  if [[ -n "$docs_field" ]]; then
    if ! is_list_field "$docs_field"; then
      bad "$meta documents must be YAML inline list"
    else
      docs_field="${docs_field#[}"
      docs_field="${docs_field%]}"
      IFS=',' read -r -a docs_arr <<< "$docs_field"
      for item in "${docs_arr[@]}"; do
        local_doc="$(echo "$item" | xargs)"
        [[ -n "$local_doc" ]] || continue
        [[ -f "$folder/$local_doc" ]] || bad "$meta lists missing document $folder/$local_doc"
      done
    fi
  fi

  for field in reviewers depends_on triggers; do
    if grep -Eq "^$field:" "$meta"; then
      value="$(awk -F': ' -v f="$field" '$1==f {print $2}' "$meta" | head -n1)"
      is_list_field "$value" || bad "$meta optional field '$field' must be YAML inline list"
    fi
  done

  reviewed_at="$(awk -F': ' '/^reviewed_at:/ {print $2}' "$meta" | head -n1)"
  [[ "$reviewed_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || bad "$meta reviewed_at must be YYYY-MM-DD"

  name_field="$(awk -F': ' '/^name:/ {print $2}' "$meta" | head -n1)"
  domain_field="$(awk -F': ' '/^domain:/ {print $2}' "$meta" | head -n1)"
  [[ "$name_field" == "$name" ]] || bad "$meta name mismatch for $folder"
  [[ "$domain_field" == "$domain" ]] || bad "$meta domain mismatch for $folder"
}

collect_core() {
  find .ai/skills/core -mindepth 1 -maxdepth 1 -type d | sort
}

collect_tech_all() {
  find .ai/skills -type f -name skill.meta.yaml ! -path '.ai/skills/core/*' -print | sed 's#/skill.meta.yaml$##' | sort
}

collect_tech_ai() {
  find .ai/skills/ai -mindepth 1 -maxdepth 1 -type d | sort
}

case "$mode" in
  all)
    while IFS= read -r folder; do
      validate_technology_skill "$folder"
    done < <(collect_tech_all)
    while IFS= read -r folder; do
      validate_core_skill "$folder"
    done < <(collect_core)
    ;;
  core)
    while IFS= read -r folder; do
      validate_core_skill "$folder"
    done < <(collect_core)
    ;;
  ai)
    while IFS= read -r folder; do
      validate_technology_skill "$folder"
    done < <(collect_tech_ai)
    for core_name in threat-modeling security-review performance-profiling observability test-and-validation e2e-testing integration-contracts contract-testing webhooks-and-retries architecture-decisions; do
      validate_core_skill ".ai/skills/core/$core_name"
    done
    ;;
  *)
    bad "unknown mode '$mode' (use: all|core|ai)"
    ;;
esac

if [[ "$fail" -eq 0 ]]; then
  echo "check-skills[$mode]: valid"
fi

exit "$fail"
