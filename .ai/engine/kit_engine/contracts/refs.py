"""Pure normalization for task-to-contract references."""

from __future__ import annotations

import re
from collections.abc import Collection


def normalize_contract_refs(value: object, relations: Collection[str]) -> list[dict]:
    refs: list[dict] = []
    for item in value or []:
        if isinstance(item, str):
            match = re.fullmatch(r"(defines|implements|consumes|verifies):([^@\s]+)@([^\s]+)", item)
            if not match:
                raise ValueError(f"invalid contract ref {item!r}; expected RELATION:CONTRACT_ID@SEMVER")
            relation, contract_id, version = match.groups()
            item = {"id": contract_id, "version": version, "relation": relation}
        if not isinstance(item, dict):
            raise ValueError("contract_refs entries must be objects or RELATION:ID@VERSION strings")
        ref = {
            "id": str(item.get("id") or "").strip(),
            "version": str(item.get("version") or "").strip(),
            "relation": str(item.get("relation") or "").strip(),
        }
        if not ref["id"] or not ref["version"] or ref["relation"] not in relations:
            raise ValueError(f"invalid contract ref: {item!r}")
        refs.append(ref)
    return refs

