"""Run Ruined Catacombs headless + engine UVE contract suite (manifest-driven)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"missing manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_catacombs_binding(lab_root: Path) -> dict[str, Any]:
    return _load_json(lab_root / "config" / "lab_uve_catacombs_binding.json")


def resolve_catacombs_root(lab_root: Path, binding: dict[str, Any]) -> Path:
    env_key = str(binding.get("catacombs_root_env_key") or "UVE_CATACOMBS_ROOT")
    override = os.environ.get(env_key, "").strip()
    if override:
        return Path(override).resolve()
    rel = str(binding.get("catacombs_root_relative_from_lab") or "")
    return (lab_root / rel).resolve()


def _first_existing(candidates: list[Path]) -> Optional[Path]:
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _resolve_binary(
    *,
    env_key: str,
    relative_candidates: list[str],
    base: Path,
) -> Path:
    override = os.environ.get(env_key, "").strip()
    if override:
        path = Path(override).resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"{env_key} does not point to a file: {path}")
    resolved = _first_existing([(base / rel).resolve() for rel in relative_candidates])
    if resolved is None:
        raise FileNotFoundError(
            f"no binary found for {env_key}; tried: {relative_candidates}"
        )
    return resolved


def _run(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_catacombs_uve_validation(
    lab_root: Optional[Path] = None,
    *,
    timeout_per_command: int = 300,
) -> dict[str, Any]:
    lab_root = (lab_root or _repo_root()).resolve()
    binding = load_catacombs_binding(lab_root)
    catacombs_root = resolve_catacombs_root(lab_root, binding)
    manifest_rel = str(
        binding.get("verification_manifest_relative_from_catacombs")
        or "config/uve/verification_manifest.json"
    )
    manifest = _load_json(catacombs_root / manifest_rel)

    headless = manifest["headless_contracts"]
    work_dir = (catacombs_root / str(headless["working_directory_relative"])).resolve()
    verify = _resolve_binary(
        env_key=str(headless["verify_binary_env_key"]),
        relative_candidates=[str(p) for p in headless["verify_candidates_relative"]],
        base=catacombs_root,
    )

    engine = manifest["engine_contracts"]
    runner = _resolve_binary(
        env_key=str(engine["test_runner_env_key"]),
        relative_candidates=[str(p) for p in engine["test_runner_candidates_relative_from_lab"]],
        base=lab_root,
    )
    seed = int(engine.get("verify_seed", 1))

    results: list[dict[str, Any]] = []

    for flag in headless["cli_flags"]:
        proc = _run([str(verify), flag], cwd=work_dir, timeout=timeout_per_command)
        ok = proc.returncode == 0 and "VERIFY_OK" in (proc.stdout or "")
        row = {
            "kind": "headless_contract",
            "id": flag,
            "ok": ok,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-400:],
            "stderr_tail": (proc.stderr or "")[-400:],
        }
        results.append(row)
        if not ok:
            return {
                "ok": False,
                "failed_at": row,
                "results": results,
                "catacombs_root": str(catacombs_root),
                "verify_binary": str(verify),
                "engine_runner": str(runner),
            }

    for scenario in engine["scenarios"]:
        proc = _run(
            [str(runner), "--verify-scenario", str(scenario), "--seed", str(seed)],
            cwd=lab_root,
            timeout=timeout_per_command,
        )
        ok = proc.returncode == 0 and "VERIFY_OK" in (proc.stdout or "")
        row = {
            "kind": "engine_scenario",
            "id": scenario,
            "ok": ok,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-400:],
            "stderr_tail": (proc.stderr or "")[-400:],
        }
        results.append(row)
        if not ok:
            return {
                "ok": False,
                "failed_at": row,
                "results": results,
                "catacombs_root": str(catacombs_root),
                "verify_binary": str(verify),
                "engine_runner": str(runner),
            }

    return {
        "ok": True,
        "results": results,
        "catacombs_root": str(catacombs_root),
        "verify_binary": str(verify),
        "engine_runner": str(runner),
        "outcome_token": str(binding.get("outcome_token") or "CATACOMBS_UVE_VALID"),
    }


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    report_json = "--json" in argv
    try:
        summary = run_catacombs_uve_validation()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if report_json:
        print(json.dumps(summary, indent=2))
    if not summary.get("ok"):
        failed = summary.get("failed_at") or {}
        print(
            f"CATACOMBS_UVE_FAIL {failed.get('kind')}:{failed.get('id')}",
            file=sys.stderr,
        )
        return 1
    token = str(summary.get("outcome_token") or "CATACOMBS_UVE_VALID")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
