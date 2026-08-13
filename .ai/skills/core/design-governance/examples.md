# Design governance examples

Inspect applicable rules:

```bash
bash .ai/scripts/ai-kit design rules --task ORDER-BE
```

Submit an assessment produced by an architect or AI assessor:

```bash
bash .ai/scripts/ai-kit design assess ORDER-BE \
  --input .ai-work/evidence/design/order-be.input.json \
  --actor architect --agent-id design-17
bash .ai/scripts/ai-kit design validate ORDER-BE
```

A result entry names the stable rule ID and evidence:

```json
{
  "rule_id": "DG-BOUNDARY-TESTS",
  "result": "pass",
  "rationale": "Producer and consumer conformance tests cover order-api v1.",
  "evidence": ["tests/contracts/order-api-v1.test.ts"]
}
```

Request a `MUST` exception through an independent reviewer:

```bash
bash .ai/scripts/ai-kit design exception ORDER-BE DG-MINIMAL-CHANGE \
  --reason "Required compatibility adapter keeps v1 consumers live" \
  --actor reviewer-42
```

The exception becomes evidence input; it does not advance task state.
