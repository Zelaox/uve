from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import load_json_ref
from agent_training.uve.ai_lab_map import load_tier_json, tier_json_to_probe_receipt


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    roots = load_json_ref("config/uve/ai_lab_roots.json")
    tier_path = os.environ.get(roots["tier_json_env"])
    try:
        if tier_path:
            tier_doc = load_tier_json(Path(tier_path))
            source = "historical_receipt"
        else:
            from agent_training.uve.ai_lab_runner import run_training_quality
            result = run_training_quality(ctx["profile"])
            tier = ctx.get("probe_spec", {}).get("tier")
            tier_doc = result["tier_results"][tier]
            source = "live_runner"
        mapped = tier_json_to_probe_receipt(tier_doc, probe_id=probe_id)
        from agent_training.uve.receipts import assert_valid_probe
        assert_valid_probe(mapped)
        return {
            "verdict": mapped["verdict"], "fault_origin": mapped["fault_origin"],
            "failure_class": mapped["failure_class"], "tier": tier_doc["tier"],
            "checks": tier_doc["checks"], "source": source,
            "tier_receipt_sha256": content_hash(tier_doc),
            "candidate_identity": tier_doc["candidate_identity"],
        }
    except (KeyError, OSError, ValueError, ImportError) as exc:
        return {"verdict": "not_verified", "fault_origin": "engine_fault",
                "failure_class": "adapter_error", "error": str(exc)}
