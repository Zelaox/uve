from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import ManifestError, load_json_ref
from agent_training.uve.bundle import load_manifest_bundle, resolve_log_dir, write_json_atomic
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.leases import LeaseManager
from agent_training.uve.orchestrator import run_probe


class ParentProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.handle = None
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel = kernel
            self.handle = kernel.OpenProcess(0x00100000, False, pid)

    def alive(self) -> bool:
        if os.name == "nt":
            return bool(self.handle) and self.kernel.WaitForSingleObject(self.handle, 0) == 258
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _render(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(values)
    if isinstance(value, list):
        return [_render(v, values) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, values) for k, v in value.items() if not k.startswith("_")}
    return value


def _set_field(doc: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        doc = doc.setdefault(key, {})
    doc[keys[-1]] = value


def _write_changed(path: Path, doc: dict) -> None:
    if path.is_file():
        try:
            if json.loads(path.read_text(encoding="utf-8")) == doc:
                return
        except (OSError, ValueError):
            pass
    write_json_atomic(path, doc)


def materialize_runtime_profile(manifest_ref: str, context_path: Path, parent_pid: int) -> str:
    spec = load_json_ref(manifest_ref)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    if context.get("owner_pid") != parent_pid:
        raise ManifestError("workstation context belongs to a different process")
    base = load_json_ref(spec["profile_ref"])
    values = {k: v for k, v in context.items() if isinstance(v, (str, int, float))}
    directory = resolve_log_dir() / spec["generated_relative"] / str(parent_pid)
    directory.mkdir(parents=True, exist_ok=True)
    for target in spec["target_templates"]:
        path = (directory / target["filename"]).resolve()
        if path.parent != directory.resolve():
            raise ManifestError("generated target must be a single filename")
        values[target["binding_id"]] = str(path)
    for target in spec["target_templates"]:
        _write_changed(Path(values[target["binding_id"]]), _render(target["document"], values))
    profile = _render(base, values)
    profile["runtime_context_ref"] = str(context_path.resolve())
    profile["runtime_manifest_ref"] = manifest_ref
    for target in spec["target_templates"]:
        _set_field(profile, target["profile_field"], values[target["binding_id"]])
    profile_path = directory / spec["profile_filename"]
    _write_changed(profile_path, profile)
    return str(profile_path)


def run_runtime_cycle(manifest_ref: str, profile_ref: str) -> dict[str, Any]:
    spec = load_json_ref(manifest_ref)
    bundle = load_manifest_bundle()
    catalog = bundle["_loaded"]["config/uve/uve_probe_catalog.json"]["probes"]
    gates = bundle["_loaded"]["config/uve/uve_gate_manifest.json"]["gates"]
    for tier in spec["tiers"]:
        if not tier["probe_ids"] or set(gates[tier["gate_id"]]["required_probe_ids"]) != set(tier["probe_ids"]):
            raise ManifestError("runtime tier and gate must declare the same probes")
        for pid in tier["probe_ids"]:
            probe = catalog[pid]
            if probe["adapter_id"] not in spec["allowed_adapter_ids"] or probe["tier"] != tier["tier_id"]:
                raise ManifestError("runtime probe adapter or tier is not allowed by startup policy")
    results = []
    for tier in spec["tiers"]:
        receipts = [run_probe(profile_ref, pid, role_id=spec["runner_role_id"]) for pid in tier["probe_ids"]]
        gate = evaluate_gate(tier["gate_id"], receipts,
                             manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
                             gate_evaluator_ref=spec["gate_evaluator_ref"])
        write_json_atomic(resolve_log_dir() / "gates" / f"{gate['trust_chain_ref']}.json", gate)
        results.append({"tier_id": tier["tier_id"], "gate": gate,
                        "gate_receipt_path": str(resolve_log_dir() / "gates" / f"{gate['trust_chain_ref']}.json"),
                        "probes": [{"probe_id": p["probe"]["probe_id"], "probe_run_id": p["probe_run_id"],
                                    "verdict": p["verdict"], "fault_origin": p["fault_origin"],
                                    "failure_class": p.get("failure_class"),
                                    "observation": p["observation"],
                                    "receipt_path": str(resolve_log_dir() / "probes" / f"{p['probe_run_id']}.json")}
                                   for p in receipts]})
    return {"tiers": results}


def serve_runtime(manifest_ref: str, context_path: Path, parent_pid: int, stop_path: Path,
                  status_path: Path, *, parent_alive: Callable[[], bool] | None = None,
                  max_cycles: int | None = None) -> dict[str, Any]:
    spec = load_json_ref(manifest_ref)
    if spec["poll_interval_seconds"] <= 0 or spec["verification_interval_seconds"] <= 0:
        raise ManifestError("runtime intervals must be positive")
    parent = ParentProcess(parent_pid)
    alive = parent_alive or parent.alive
    leases = LeaseManager()
    resource = spec["lease_resource_kind"]
    key = os.path.normcase(str(context_path.resolve()))
    token = leases.acquire(resource, key, str(parent_pid))
    if token is None:
        parent.close()
        return {"state": "not_verified", "reason": "workstation verification runtime already leased"}
    status: dict[str, Any] = {"schema_version": "uve_runtime_status.v1", "state": "starting",
                              "owner_pid": parent_pid, "worker_pid": os.getpid(), "tiers": []}
    canonical_status = resolve_log_dir() / spec["status_relative"]
    application_cache: dict[str, Any] = {}
    completed = 0
    previous_context = None
    last_run = float("-inf")
    try:
        while alive() and not stop_path.exists():
            try:
                context_hash = content_hash(json.loads(context_path.read_text(encoding="utf-8")))
                if context_hash != previous_context or time.monotonic() - last_run >= spec["verification_interval_seconds"]:
                    status.update(state="verifying", summary="App=not_verified - checking", updated_at=time.time(), tiers=[], application={"decision":"not_verified", "reason":"verification in progress"})
                    status.pop("deep_flows", None)
                    _write_changed(status_path, status)
                    _write_changed(canonical_status, status)
                    profile_ref = materialize_runtime_profile(manifest_ref, context_path, parent_pid)
                    result = run_runtime_cycle(manifest_ref, profile_ref)
                    if spec.get("application"):
                        from agent_training.uve.application_runtime import materialize_application_profile, run_application_cycle
                        application_ref = materialize_application_profile(spec, context_path, parent_pid)
                        result["application"] = run_application_cycle(spec, application_ref, application_cache) if application_ref else {
                            "decision":"not_verified", "reason":"selected project has no application verification contract"}
                    if content_hash(json.loads(context_path.read_text(encoding="utf-8"))) != context_hash:
                        status.update(state="not_verified", updated_at=time.time(), tiers=[], application={"decision":"not_verified"}, error="selected context changed during verification")
                        _write_changed(status_path, status)
                        _write_changed(canonical_status, status)
                        continue
                    status.update(result, state="running", updated_at=time.time(), context_sha256=context_hash)
                    status.pop("error", None)
                    previous_context = context_hash
                    last_run = time.monotonic()
                    completed += 1
                if status.get("state") == "running" and status.get("application", {}).get("decision") == "approved":
                    from agent_training.uve.gate_evaluator import validate_stored_gate
                    if not validate_stored_gate(status["application"]["gate"]):
                        status["application"] = {"decision":"not_verified", "reason":"application code or context changed"}
                        last_run = float("-inf")
                if status.get("state") == "running":
                    status["summary"] = " | ".join([tier["tier_id"] + "=" + tier["gate"]["decision"] for tier in status.get("tiers", [])] + ["App=" + status.get("application", {}).get("decision", "not_verified")])
                    if spec.get("deep_flow_status_project_ids"):
                        from agent_training.uve.flow_status import collect_deep_status

                        try:
                            deep = [item for item in collect_deep_status(load_manifest_bundle()["manifest_bundle_sha256"])
                                    if item["project_id"] in spec["deep_flow_status_project_ids"]]
                        except Exception:
                            deep = []
                        status["deep_flows"] = deep
                        for tier_id in sorted({item["tier"] for item in deep}):
                            decisions = {item["gate_decision"] for item in deep if item["tier"] == tier_id}
                            decision = "rejected" if "rejected" in decisions else "approved" if decisions == {"approved"} else "not_verified"
                            status["summary"] += f" | Flow{tier_id}={decision}"
                else:
                    status["summary"] = "App=not_verified"
                status["updated_at"] = time.time()
                _write_changed(status_path, status)
                _write_changed(canonical_status, status)
            except Exception as exc:
                status.update(state="not_verified", error=f"{type(exc).__name__}: {exc}", updated_at=time.time(), tiers=[], application={"decision":"not_verified"})
                _write_changed(status_path, status)
                _write_changed(canonical_status, status)
            if max_cycles is not None and completed >= max_cycles:
                break
            time.sleep(spec["poll_interval_seconds"])
    finally:
        try:
            status.update(state="stopped", summary="App=not_verified", updated_at=time.time(), application={"decision":"not_verified", "reason":"verification worker stopped"})
            _write_changed(status_path, status)
            _write_changed(canonical_status, status)
        finally:
            try:
                leases.release(resource, key, token)
            finally:
                parent.close()
    return status
