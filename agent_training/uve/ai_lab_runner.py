from __future__ import annotations

import importlib
import os
import sys
import threading
from pathlib import Path
from typing import Any

from agent_training.manifest_loader import ManifestError, load_json_ref

_import_lock = threading.RLock()


def run_training_quality(profile: dict[str, Any]) -> dict[str, Any]:
    """Invoke AI Lab's authoritative runner API, preserving its tier semantics."""
    roots = load_json_ref("config/uve/ai_lab_roots.json")
    lab = Path(os.environ[roots["ai_lab_root_env"]]).resolve()
    config_ref = profile.get("ai_lab_run_ref") or os.environ.get(roots["run_config_env"])
    if not config_ref:
        raise ManifestError("AI Lab runner requires ai_lab_run_ref or run config environment")
    spec = load_json_ref(config_ref)

    def within(relative: str) -> Path:
        path = (lab / relative).resolve()
        path.relative_to(lab)
        return path

    kwargs = {
        "root": lab, "run_path": within(spec["run_path_relative"]),
        "checkpoint_dir": within(spec["checkpoint_relative"]),
        "training_state": load_json_ref(str(within(spec["training_state_relative"]))),
        "runtime_contract": load_json_ref(str(within(spec["runtime_contract_relative"]))),
        "training_stage": spec["training_stage"], "tiers": spec["tiers"],
        "export_profile": spec.get("export_profile", ""),
        "receipt_scope": spec["receipt_scope"],
    }
    with _import_lock:
        sys.path.insert(0, str(lab))
        try:
            module = importlib.import_module(roots["runner_module"])
            if not Path(module.__file__).resolve().is_relative_to(lab):
                raise ManifestError("AI Lab runner was imported from another checkout")
            return getattr(module, roots["runner_callable"])(**kwargs)
        finally:
            sys.path.remove(str(lab))
