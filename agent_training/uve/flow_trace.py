"""Project bounded telemetry and verify causal ordering without wall-clock inference."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.uve.flow_contract import require, token


def project_events(raw: Any, step: dict, operation_id: str, limit: int) -> list[dict]:
    trace = step.get("trace")
    if not trace:
        return []
    require(isinstance(raw, list) and len(raw) <= limit, "trace observation is missing or exceeds its budget")

    def alias(value: Any, field: str) -> str:
        return content_hash([operation_id, token(value, field)])

    events = []
    for item in raw:
        require(isinstance(item, dict), "invalid trace event")
        require(item.get("operation_id") == operation_id, "trace event belongs to a different operation")
        require(item.get("kind") in trace["kinds"], "trace event kind is outside the step contract")
        sequence = item.get("sequence")
        require(type(sequence) is int and sequence > 0, "invalid trace sequence")
        parents = item.get("parents", [])
        require(isinstance(parents, list) and len(parents) <= limit, "invalid trace parents")
        events.append({
            "event_id": alias(item.get("event_id"), "event id"),
            "operation_id": operation_id,
            "step": step["step_id"],
            "source": trace["source"],
            "kind": item["kind"],
            "attempt": alias(item.get("attempt"), "attempt id"),
            "epoch": alias(item.get("epoch"), "runtime epoch"),
            "sequence": sequence,
            "parents": sorted({alias(parent, "parent id") for parent in parents}),
        })
    return events


def _graph(events: list[dict]) -> tuple[dict, dict]:
    index: dict[str, dict] = {}
    sequences: dict[tuple, list[int]] = defaultdict(list)
    for event in events:
        key = event["event_id"]
        if key in index:
            require(index[key] == event, "conflicting duplicate trace event")
            continue
        index[key] = event
        sequences[(event["source"], event["epoch"])].append(event["sequence"])
    for sequence in sequences.values():
        require(sorted(sequence) == list(range(1, len(sequence) + 1)), "trace source has a sequence gap or duplicate")

    ancestors: dict[str, set[str]] = {}
    # Events can be delivered out of order. The graph uses causal parent references.
    remaining = set(index)
    for event in index.values():
        require(all(parent in index for parent in event["parents"]), "trace references an absent cause")
    while remaining:
        ready = [event_id for event_id in sorted(remaining) if set(index[event_id]["parents"]) <= ancestors.keys()]
        require(ready, "trace contains a causal cycle")
        for event_id in ready:
            event = index[event_id]
            direct = event["parents"]
            for parent_id in direct:
                parent = index[parent_id]
                require(parent["operation_id"] == event["operation_id"], "trace joins different operations")
                if (parent["source"], parent["epoch"]) == (event["source"], event["epoch"]):
                    require(parent["sequence"] < event["sequence"], "causal order conflicts with producer sequence")
            ancestors[event_id] = set(direct).union(*(ancestors[parent] for parent in direct))
            remaining.remove(event_id)
    return index, ancestors


def check_edges(events: list[dict], edges: list[dict]) -> list[dict]:
    index, ancestors = _graph(events)

    def endpoints(spec: dict) -> list[dict]:
        return [row for row in index.values() if row["step"] == spec["step"] and row["kind"] == spec["kind"]]

    def connects(left: dict, right: dict, edge: dict) -> bool:
        if left["event_id"] not in ancestors[right["event_id"]]:
            return False
        if edge.get("different_sources") and left["source"] == right["source"]:
            return False
        relation = edge.get("relation")
        if relation == "new_attempt":
            return left["attempt"] != right["attempt"]
        if relation == "new_epoch":
            return left["epoch"] != right["epoch"]
        return True

    result = []
    for edge in edges:
        left, right = endpoints(edge["from"]), endpoints(edge["to"])
        seen = bool(left and right and all(any(connects(a, b, edge) for a in left) for b in right))
        result.append({"edge_id": edge["edge_id"], "state": "passed" if seen else "not_verified",
                       "from": edge["from"], "to": edge["to"], "source_count": len(left), "target_count": len(right)})
    return result
