from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_training.manifest_loader import load_json_ref, ManifestError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Hypothesis:
    hypothesis_id: str
    statement: str
    status: str
    superseded_by: str | None = None


@dataclass
class Attempt:
    attempt_id: str
    action_summary: str
    tool_trace_ref: str
    outcome: str


@dataclass
class Evidence:
    evidence_id: str
    type: str
    artifact_hashes: list[str]
    verifier_ref: str
    pass_fail: bool


@dataclass
class Repair:
    repair_id: str
    artifact_ref: str


@dataclass
class Verification:
    gate_ids: list[str]
    pass_fail: bool


@dataclass
class Resolution:
    status: str
    reason: str | None = None
    triggering_evidence_id: str | None = None


@dataclass
class IssueRecord:
    issue_id: str
    project_id: str
    schema_version: str
    created_at: str
    updated_at: str
    observation: str
    repro_steps: list[str]
    artifact_hashes: list[str]
    hypotheses: list[Hypothesis] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    verification: Verification | None = None
    resolution: Resolution = field(default_factory=lambda: Resolution(status="open"))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class IssueStore:
    def __init__(self, storage_dir: Path, contract_ref: str = "config/contracts/issue_contract.json"):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.contract = load_json_ref(contract_ref)
        self.audit_path = storage_dir / "issue_audit.jsonl"

    def _issue_path(self, issue_id: str) -> Path:
        return self.storage_dir / f"{issue_id}.json"

    def _append_audit(self, event: dict[str, Any]) -> None:
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")

    def create_issue(
        self,
        project_id: str,
        observation: str,
        repro_steps: list[str],
        artifact_hashes: list[str] | None = None,
    ) -> IssueRecord:
        issue_id = str(uuid.uuid4())
        now = _utc_now()
        record = IssueRecord(
            issue_id=issue_id,
            project_id=project_id,
            schema_version=self.contract["schema_version"],
            created_at=now,
            updated_at=now,
            observation=observation,
            repro_steps=repro_steps,
            artifact_hashes=artifact_hashes or [],
        )
        self._save(record)
        self._append_audit({"event": "issue_created", "issue_id": issue_id, "at": now})
        return record

    def _save(self, record: IssueRecord) -> None:
        path = self._issue_path(record.issue_id)
        path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        record.updated_at = _utc_now()

    def load(self, issue_id: str) -> IssueRecord:
        path = self._issue_path(issue_id)
        if not path.is_file():
            raise ManifestError(f"issue not found: {issue_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return self._from_dict(data)

    def _from_dict(self, data: dict[str, Any]) -> IssueRecord:
        hyps = [Hypothesis(**h) for h in data.get("hypotheses", [])]
        attempts = [Attempt(**a) for a in data.get("attempts", [])]
        evidence = [Evidence(**e) for e in data.get("evidence", [])]
        repairs = [Repair(**r) for r in data.get("repairs", [])]
        ver = data.get("verification")
        verification = Verification(**ver) if ver else None
        res = data.get("resolution") or {"status": "open"}
        resolution = Resolution(**res)
        return IssueRecord(
            issue_id=data["issue_id"],
            project_id=data["project_id"],
            schema_version=data["schema_version"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            observation=data["observation"],
            repro_steps=data["repro_steps"],
            artifact_hashes=data.get("artifact_hashes", []),
            hypotheses=hyps,
            attempts=attempts,
            evidence=evidence,
            repairs=repairs,
            verification=verification,
            resolution=resolution,
        )

    def append_hypothesis(self, issue_id: str, statement: str) -> Hypothesis:
        record = self.load(issue_id)
        h = Hypothesis(hypothesis_id=str(uuid.uuid4()), statement=statement, status="open")
        record.hypotheses.append(h)
        self._save(record)
        self._append_audit({"event": "hypothesis_added", "issue_id": issue_id, "hypothesis_id": h.hypothesis_id})
        return h

    def append_attempt(
        self, issue_id: str, action_summary: str, tool_trace_ref: str, outcome: str
    ) -> Attempt:
        record = self.load(issue_id)
        a = Attempt(
            attempt_id=str(uuid.uuid4()),
            action_summary=action_summary,
            tool_trace_ref=tool_trace_ref,
            outcome=outcome,
        )
        record.attempts.append(a)
        self._save(record)
        return a

    def append_evidence(
        self,
        issue_id: str,
        evidence_type: str,
        artifact_hashes: list[str],
        verifier_ref: str,
        pass_fail: bool,
    ) -> Evidence:
        record = self.load(issue_id)
        e = Evidence(
            evidence_id=str(uuid.uuid4()),
            type=evidence_type,
            artifact_hashes=artifact_hashes,
            verifier_ref=verifier_ref,
            pass_fail=pass_fail,
        )
        record.evidence.append(e)
        self._save(record)
        return e

    def add_repair(self, issue_id: str, artifact_ref: str) -> Repair:
        record = self.load(issue_id)
        r = Repair(repair_id=str(uuid.uuid4()), artifact_ref=artifact_ref)
        record.repairs.append(r)
        self._save(record)
        return r

    def close_issue(self, issue_id: str, gate_ids: list[str], task_registry_ref: str) -> IssueRecord:
        registry = load_json_ref(task_registry_ref)
        required = registry.get("issue_close_required_verifier_ids", [])
        record = self.load(issue_id)
        passed = {e.verifier_ref for e in record.evidence if e.pass_fail}
        for req in required:
            if req not in passed:
                raise ManifestError(
                    f"cannot close issue {issue_id}: missing passing verifier {req}"
                )
        for gid in set(gate_ids) | set(required):
            spec = registry.get("verifiers", {}).get(gid, {})
            if spec.get("kind") == "uve_gate_receipt_ref":
                from agent_training.lab.verifiers import run_verifier
                if not run_verifier(gid, task_registry_ref, seed=0):
                    raise ManifestError(f"cannot close issue: invalid UVE gate receipt {gid}")
            if gid in required and gid not in passed:
                raise ManifestError(f"verification gate failed: {gid}")
        record.verification = Verification(gate_ids=gate_ids, pass_fail=True)
        record.resolution = Resolution(status="closed", reason="verifier_backed")
        self._save(record)
        self._append_audit({"event": "issue_closed", "issue_id": issue_id})
        return record

    def reopen_issue(self, issue_id: str, reason: str, triggering_evidence_id: str) -> IssueRecord:
        record = self.load(issue_id)
        record.resolution = Resolution(
            status="reopened",
            reason=reason,
            triggering_evidence_id=triggering_evidence_id,
        )
        self._save(record)
        self._append_audit({"event": "issue_reopened", "issue_id": issue_id})
        return record

    def export_trajectories_jsonl(self, out_path: Path) -> str:
        from agent_training.content_hash import content_hash

        lines: list[dict[str, Any]] = []
        for path in sorted(self.storage_dir.glob("*.json")):
            if path.name == "issue_audit.jsonl":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            lines.append({"type": "issue_trajectory", "issue": data})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for row in lines:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        return content_hash(lines)
