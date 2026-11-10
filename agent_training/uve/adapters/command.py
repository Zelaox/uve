from __future__ import annotations

import subprocess
import sys
from typing import Any

from agent_training.manifest_loader import load_json_ref
from agent_training.uve.bundle import load_engine_manifest


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    ref = ctx["profile"].get("command_probe_refs", {}).get(probe_id)
    if not ref:
        return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "no command manifest"}
    spec = load_json_ref(ref)
    values = {"python": sys.executable, "workspace_root": str(ctx["workspace_root"])}
    if spec.get("argument_values_ref"):
        values.update(load_json_ref(spec["argument_values_ref"]))
    missing = [field for field in spec.get("required_argument_fields", []) if not values.get(field)]
    if missing:
        return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "missing runtime bindings", "missing_fields": missing}
    argv = [arg.format_map(values) for arg in spec["argv"]]
    io = load_json_ref(load_engine_manifest()["io_policy_ref"])
    encoding = spec.get("encoding", io.get("command_output_encoding", "utf-8"))
    proc = subprocess.run(argv, cwd=ctx["workspace_root"], capture_output=True,
                          timeout=spec["timeout_seconds"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        stdout, stderr = proc.stdout.decode(encoding), proc.stderr.decode(encoding)
    except (UnicodeError, LookupError):
        return {"verdict": "not_verified", "fault_origin": "engine_fault", "failure_class": "adapter_error",
                "reason": "command output does not match its declared encoding", "encoding": encoding}
    passed = proc.returncode == spec["expected_returncode"] and spec.get("expected_stdout", "") in stdout
    return {"verdict": "passed" if passed else "failed", "fault_origin": "none" if passed else "subject_fault",
            "failure_class": None if passed else "assertion_mismatch", "returncode": proc.returncode,
            "stdout": stdout, "stderr": stderr}
