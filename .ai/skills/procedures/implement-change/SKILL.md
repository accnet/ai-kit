---
name: implement-change
description: Implement a scoped task in an isolated worktree. Use for backend, frontend, refactoring, and integration implementation after required plans and contracts are available.
---

# INPUTS
- Assigned task, declared file scope, approved contract references, policy skills, and selected technology packs.

# PRECONDITIONS
- Hold a valid worker lease; dependencies and required contracts must be runnable.

# ACTIONS
- Read the task handoff and selected procedure, policy, and technology entrypoints.
- Implement only the accepted scope in the assigned worktree.
- Add or update focused tests and record implementation completion through the allowed worker transition.

# OUTPUTS
- Scoped code and tests in the task worktree, plus implementation-complete handoff context.

# VALIDATION
- Run focused checks and ensure changed paths remain inside the declared scope.

# FORBIDDEN
- Do not edit canonical workflow state directly, self-approve QA/review/delivery, modify unrelated files, or bypass an approved contract boundary.
