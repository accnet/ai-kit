"""Stable, collision-safe graph construction for contract projections."""

from __future__ import annotations

import hashlib
import json


class ContractGraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}

    def add_node(self, identifier: str, node_type: str, label: str, **values: object) -> str:
        candidate = {"id": identifier, "type": node_type, "label": label, **values}
        existing = self.nodes.get(identifier)
        if existing is not None and existing != candidate:
            raise ValueError(f"contract impact node collision: {identifier}")
        self.nodes[identifier] = candidate
        return identifier

    def add_edge(self, source: str, target: str, relation: str, **values: object) -> str:
        identity = json.dumps([source, target, relation, values], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        identifier = f"contract-impact-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
        self.edges[identifier] = {"id": identifier, "from": source, "to": target, "relation": relation, **values}
        return identifier

    def projection(self) -> tuple[list[dict], list[dict], dict[str, int]]:
        nodes = sorted(self.nodes.values(), key=lambda item: item["id"])
        edges = sorted(self.edges.values(), key=lambda item: item["id"])
        counts: dict[str, int] = {}
        for node in nodes:
            counts[node["type"]] = counts.get(node["type"], 0) + 1
        return nodes, edges, dict(sorted(counts.items()))
