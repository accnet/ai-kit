#!/usr/bin/env bash
# v2 adaptation of v1 G4 gate; safe in both Git and non-Git directories.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
mode="${1:-all}"
staged=0
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [[ "$mode" == "staged" ]]; then staged=1; files=$(git diff --cached --name-only --diff-filter=ACM); else files=$(git ls-files); fi
else
  files=$(find . -path './.ai-work' -prune -o -type f -print | sed 's#^./##')
fi
fail=0
# In `staged` mode the content that matters is the blob in the index, not the
# file on disk: `git add secrets.env && sed -i /KEY/d secrets.env` used to pass
# the hook and commit the secret anyway, while an unstaged edit to an unrelated
# file could fail it. Read each candidate from the index instead.
# The secret pattern MUST be passed with `grep -e`. It begins with "-----"
# (the PEM header), so as a positional argument grep parses it as a bundle of
# short options and dies with "unknown option -- ---BEGIN ..." and exit 2 --
# swallowed by the `2>/dev/null` below, leaving every content scan silently
# passing. No private key, AWS id or API token was ever actually detected.
read_candidate() {
  if [[ "$staged" == "1" ]]; then git show ":$1" 2>/dev/null; else cat -- "$1" 2>/dev/null; fi
}
candidate_exists() {
  if [[ "$staged" == "1" ]]; then git cat-file -e ":$1" 2>/dev/null; else [[ -f "$1" ]]; fi
}
while IFS= read -r file; do
  [[ -n "$file" ]] || continue
  [[ "$file" == .ai-work/* ]] && { echo "G4 FAIL: transient state must not be committed: $file" >&2; fail=1; }
  case "$file" in
    .env|.env.*|*/.env|*/.env.*) [[ "$file" == *.example || "$file" == *.sample ]] || { echo "G4 FAIL: environment file must not be committed: $file" >&2; fail=1; } ;;
    *.pem|*.p12|*.pfx|id_rsa|id_ed25519) echo "G4 FAIL: credential file must not be committed: $file" >&2; fail=1 ;;
  esac
  candidate_exists "$file" || continue
  case "$file" in *.png|*.jpg|*.jpeg|*.gif|*.pdf|*.zip) continue ;; esac
  if read_candidate "$file" | grep -nE -e '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{30,}' >/dev/null 2>&1; then
    echo "G4 FAIL: possible secret in $file" >&2; fail=1
  fi
done <<< "$files"
exit "$fail"
