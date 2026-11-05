from __future__ import annotations

from typing import Any
import importlib.util
import tempfile

from agent_training.uve.bundle import load_manifest_bundle


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        bundle = load_manifest_bundle()
        if not bundle.get("manifest_bundle_sha256"):
            raise RuntimeError("missing bundle hash")
        catalog = bundle["_loaded"]["config/uve/uve_adapter_catalog.json"]
        for spec in catalog["adapters"].values():
            if importlib.util.find_spec(spec["module"]) is None:
                raise RuntimeError(f"adapter module unavailable: {spec['module']}")
        from agent_training.uve.bundle import resolve_log_dir
        with tempfile.TemporaryFile(dir=resolve_log_dir()) as stream:
            stream.write(b"uve-self-check")
            stream.flush()
        from agent_training.uve.engine_state import write_engine_state

        write_engine_state(engine_degraded=False, probe_run_id=ctx.get("probe_run_id"))
        return {"outcome_token": "engine_ok", "verdict": "passed"}
    except Exception as exc:
        from agent_training.uve.engine_state import write_engine_state

        write_engine_state(engine_degraded=True, probe_run_id=ctx.get("probe_run_id"))
        return {
            "verdict": "not_verified",
            "fault_origin": "engine_fault",
            "failure_class": "engine_degraded",
            "error": str(exc),
        }
