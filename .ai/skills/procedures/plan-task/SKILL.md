---
name: plan-task
description: Create a bounded, dependency-aware task plan. Use for requirements decomposition, scope definition, acceptance criteria, and executable workflow DAGs before implementation begins.
---

# INPUTS
- User request, resolved L0-L3 context package, project context snapshot, existing workflow, and relevant decisions.

# PRECONDITIONS
- Identify a user goal, a project root, and the role that owns each proposed task.

# ACTIONS
- Resolve truth topics and inspect only the canonical requirements and affected boundaries selected by the context package.
- Decompose work into independently verifiable tasks with explicit dependencies.
- State acceptance criteria, declared file scope, contract references, and assumptions.
- Produce or update a plan draft through the control-plane command.

# OUTPUTS
- Plan draft with task DAG, scope, ownership, acceptance criteria, and risks.

# VALIDATION
- Validate task IDs, owners, dependency DAG, and acceptance criteria before materialization.

# FORBIDDEN
- Do not modify implementation files, invent architecture facts, claim worker tasks, or bypass plan approval.
