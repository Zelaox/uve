from __future__ import annotations

import socket
from typing import Any

from agent_training.manifest_loader import load_json_ref


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    profile = ctx["profile"]
    port_ref = profile.get("dev_server_port_ref")
    if not port_ref:
        return {
            "verdict": "not_verified",
            "fault_origin": "unknown",
            "failure_class": "process_down",
            "reason": "no dev_server_port_ref in profile",
        }
    port_doc = load_json_ref(port_ref)
    host = port_doc["host"]
    port = int(port_doc["port"])
    try:
        with socket.create_connection((host, port), timeout=port_doc["connect_timeout_seconds"]):
            return {"outcome_token": "process_listening", "verdict": "passed", "host": host, "port": port}
    except OSError as exc:
        if isinstance(exc, PermissionError):
            return {"verdict": "not_verified", "fault_origin": "engine_fault", "failure_class": "adapter_error", "error": str(exc)}
        return {
            "verdict": "failed",
            "fault_origin": "subject_fault",
            "failure_class": "process_down",
            "error": str(exc),
            "host": host,
            "port": port,
        }
