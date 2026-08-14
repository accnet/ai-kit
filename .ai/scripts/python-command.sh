#!/usr/bin/env bash
# Shared Python runtime selection for AI-Kit shell entrypoints.

ai_kit_python_command() {
  # AI_KIT_PYTHON is the documented, AI-Kit-specific override. Keep the
  # older PYTHON_CMD override for existing automations.
  local override="${AI_KIT_PYTHON:-${PYTHON_CMD:-}}"
  if [[ -n "$override" ]]; then
    command -v "$override" 2>/dev/null || [[ -x "$override" ]] || return 1
    printf '%s\n' "$override"
    return 0
  fi

  local kernel
  kernel="$(uname -s 2>/dev/null || true)"
  if [[ "${OS:-}" == "Windows_NT" || "$kernel" =~ ^(MINGW|MSYS|CYGWIN) ]]; then
    command -v python 2>/dev/null || command -v python3 2>/dev/null
  else
    command -v python3 2>/dev/null || command -v python 2>/dev/null
  fi
}
