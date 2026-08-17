#!/usr/bin/env bash
# Install AI-Kit from a self-contained .ai directory into a project.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="$(cd "$AI_ROOT/.." && pwd)"
FORCE=0
DRY_RUN=0

usage() {
  echo "Usage: bash .ai/install/install.sh [--target <project-root>] [--force] [--dry-run]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$(cd "$2" && pwd)"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$TARGET" != "$AI_ROOT" ]] || { echo "Target cannot be the .ai directory." >&2; exit 2; }

TEMPLATE_ROOT="$AI_ROOT/install/templates"
CONFIG_TEMPLATE_ROOT="$AI_ROOT/install/config"
PROJECT_ROOT="$(cd "$AI_ROOT/.." && pwd)"
SOURCES=()
DESTINATIONS=()
CONFIG_FILES=(config.yaml runners.yaml automation.yaml registry.yaml contexts.yaml epics.yaml rules.yaml kit.yaml design-policy.json contracts.json delivery.json architecture.json architecture-fitness.json truth.yaml)

add_entry() {
  SOURCES+=("$1")
  DESTINATIONS+=("$2")
}

# The complete .ai tree is the install source. This makes copying only .ai
# sufficient; keep install/config too so the installed kit remains
# self-contained for later re-installation and configuration seeding.
while IFS= read -r -d '' file; do
  rel="${file#$AI_ROOT/}"
  if git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$PROJECT_ROOT" check-ignore -q -- ".ai/$rel" && continue
  fi
  add_entry "$file" "$TARGET/.ai/$rel"
done < <(find "$AI_ROOT" \( -type f -o -type l \) -print0)

# Root-level files are generated from templates owned by .ai.
add_entry "$AI_ROOT/install/AGENTS.md" "$TARGET/AGENTS.md"
while IFS= read -r -d '' file; do
  rel="${file#$TEMPLATE_ROOT/}"
  add_entry "$file" "$TARGET/$rel"
done < <(find "$TEMPLATE_ROOT" \( -type f -o -type l \) -print0)

# Config is project-owned state, so seed it separately and never overwrite it.
for name in "${CONFIG_FILES[@]}"; do
  add_entry "$CONFIG_TEMPLATE_ROOT/$name" "$TARGET/.ai-config/$name"
done

CONFLICTS=0
for index in "${!SOURCES[@]}"; do
  source_path="${SOURCES[$index]}"
  destination="${DESTINATIONS[$index]}"
  if [[ "$destination" == "$TARGET/.ai-config/"* && -e "$destination" ]]; then
    continue
  fi
  if [[ -e "$destination" || -L "$destination" ]] && ! [[ "$source_path" -ef "$destination" ]]; then
    if [[ -f "$source_path" && -f "$destination" ]] && cmp -s "$source_path" "$destination"; then
      continue
    fi
    echo "conflict: ${destination#$TARGET/}" >&2
    CONFLICTS=1
  fi
done
if [[ "$CONFLICTS" -eq 1 && "$FORCE" -ne 1 ]]; then
  echo "Installation stopped: use --force to replace listed managed files." >&2
  exit 1
fi

for index in "${!SOURCES[@]}"; do
  source_path="${SOURCES[$index]}"
  destination="${DESTINATIONS[$index]}"
  if [[ "$destination" == "$TARGET/.ai-config/"* && -e "$destination" ]]; then
    continue
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "copy: ${destination#$TARGET/}"
    continue
  fi
  [[ "$source_path" -ef "$destination" ]] && continue
  mkdir -p "$(dirname "$destination")"
  cp -P "$source_path" "$destination"
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  ignore="$TARGET/.gitignore"
  marker="# AI-Kit runtime state"
  if ! [[ -f "$ignore" ]] || ! grep -qF "$marker" "$ignore"; then
    printf '\n%s\n' "$marker" >> "$ignore"
  fi
  if ! grep -qxF '.ai-work/' "$ignore"; then
    printf '.ai-work/\n' >> "$ignore"
  fi
  if ! grep -qF '.visualizer/*.json' "$ignore"; then
    printf '.visualizer/*.json\n' >> "$ignore"
  fi
fi

echo "AI-Kit installed into $TARGET"
echo "Next: cd \"$TARGET\" && bash .ai/scripts/bootstrap.sh && bash .ai/scripts/doctor.sh"
