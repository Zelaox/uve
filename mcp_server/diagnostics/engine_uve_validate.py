"""Run AI Lab engine UVE contract suite (manifest-driven verify scenarios)."""

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


def _resolve_binary(
    *, env_key: str, relative_candidates: list[str], base: Path
) -> Path:
    override = os.environ.get(env_key, "").strip()
    if override:
        path = Path(override).resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"{env_key} does not point to a file: {path}")
    for relative in relative_candidates:
        path = (base / relative).resolve()
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"no binary found for {env_key}; tried: {relative_candidates}"
    )


def _run(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def load_engine_binding(lab_root: Path) -> dict[str, Any]:
    return _load_json(lab_root / "config" / "lab_uve_engine_binding.json")


def resolve_engine_root(lab_root: Path, binding: dict[str, Any]) -> Path:
    env_key = str(binding.get("engine_root_env_key") or "UVE_ENGINE_ROOT")
    override = os.environ.get(env_key, "").strip()
    if override:
        return Path(override).resolve()
    rel = str(binding.get("engine_root_relative_from_lab") or "engine")
    return (lab_root / rel).resolve()


def _resolve_scenario_ids(engine_root: Path, engine_contracts: dict[str, Any]) -> list[str]:
    scenarios: list[str] = []
    ref_rel = str(engine_contracts.get("scenario_list_ref_relative_from_engine") or "").strip()
    field = str(engine_contracts.get("scenario_list_json_field") or "full_regression_verify")
    if ref_rel:
        ref_path = (engine_root / ref_rel).resolve()
        payload = _load_json(ref_path)
        listed = payload.get(field)
        if not isinstance(listed, list):
            raise KeyError(f"{field} missing or not a list in {ref_path}")
        scenarios.extend(str(item) for item in listed)

    extras = engine_contracts.get("extra_scenarios") or []
    for item in extras:
        name = str(item)
        if name not in scenarios:
            scenarios.append(name)
    if not scenarios:
        raise ValueError("no verify scenarios resolved from engine verification manifest")
    return scenarios


def run_engine_uve_validation(
    lab_root: Optional[Path] = None,
    *,
    timeout_per_command: int = 600,
) -> dict[str, Any]:
    lab_root = (lab_root or _repo_root()).resolve()
    binding = load_engine_binding(lab_root)
    engine_root = resolve_engine_root(lab_root, binding)
    manifest_rel = str(
        binding.get("verification_manifest_relative_from_engine")
        or "config/uve/verification_manifest.json"
    )
    manifest = _load_json(engine_root / manifest_rel)
    engine = manifest["engine_contracts"]
    runner = _resolve_binary(
        env_key=str(engine["test_runner_env_key"]),
        relative_candidates=[str(p) for p in engine["test_runner_candidates_relative_from_lab"]],
        base=lab_root,
    )
    seed = int(engine.get("verify_seed", 1))
    scenarios = _resolve_scenario_ids(engine_root, engine)

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
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
                "engine_root": str(engine_root),
                "engine_runner": str(runner),
            }

    return {
        "ok": True,
        "results": results,
        "engine_root": str(engine_root),
        "engine_runner": str(runner),
        "scenario_count": len(scenarios),
        "outcome_token": str(binding.get("outcome_token") or "ENGINE_UVE_VALID"),
    }


def _maybe_attach_triage_session_report(
    lab_root: Path, binding: dict[str, Any], argv: list[str]
) -> dict[str, Any] | None:
    if "--no-triage-report" in argv:
        return None
    want = "--triage-report" in argv or bool(binding.get("validate_attach_triage_session_on_success"))
    if not want:
        return None
    script_rel = str(
        binding.get("validate_triage_session_script_relative_from_lab")
        or "scripts/debug/triage_engine_session.py"
    )
    script = lab_root / script_rel
    proc = subprocess.run(
        [sys.executable, str(script), "--runtime-triage", "--write-report"],
        cwd=str(lab_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-500:],
            "stdout_tail": (proc.stdout or "")[-500:],
        }
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "parse_error": True, "stdout_tail": (proc.stdout or "")[-500:]}
    return {"ok": True, "triage_session_report_path": payload.get("report_path"), "summary": payload}


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    report_json = "--json" in argv
    expansion_advisory = "--expansion-advisory" in argv
    expansion_self_test = "--expansion-self-test" in argv
    expansion_evolve = "--expansion-evolve" in argv
    lab_root = _repo_root()
    binding = load_engine_binding(lab_root)
    try:
        summary = run_engine_uve_validation()
    except (FileNotFoundError, KeyError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if report_json:
        print(json.dumps(summary, indent=2))
    if not summary.get("ok"):
        failed = summary.get("failed_at") or {}
        print(
            f"ENGINE_UVE_FAIL {failed.get('kind')}:{failed.get('id')}",
            file=sys.stderr,
        )
        return 1
    token = str(summary.get("outcome_token") or "ENGINE_UVE_VALID")
    print(token)
    exit_code = 0
    expansion_bundle: dict[str, Any] = {}
    if expansion_advisory or expansion_self_test or expansion_evolve:
        from mcp_server.diagnostics.uve_test_expansion_advisor import run_test_expansion_advisory

        advisory = run_test_expansion_advisory(lab_root)
        expansion_bundle["expansion_advisory"] = advisory
        if report_json:
            print(json.dumps({"expansion_advisory": advisory}, indent=2))
        if expansion_evolve:
            from mcp_server.diagnostics.uve_test_expansion_evolution import run_evolution_generation

            evolve = run_evolution_generation(lab_root)
            expansion_bundle["expansion_evolve"] = evolve
            if report_json:
                print(json.dumps({"expansion_evolve": evolve}, indent=2))
        if expansion_self_test:
            from mcp_server.diagnostics.uve_expansion_self_test import run_expansion_self_tests

            self_report = run_expansion_self_tests(lab_root)
            expansion_bundle["expansion_self_test"] = self_report
            if report_json:
                print(json.dumps({"expansion_self_test": self_report}, indent=2))
            if not self_report.get("ok"):
                print("EXPANSION_SELF_TEST_FAIL", file=sys.stderr)
                exit_code = 3
    triage_attach = _maybe_attach_triage_session_report(lab_root, binding, argv)
    if triage_attach is not None:
        summary["triage_session_attach"] = triage_attach
        if report_json:
            print(json.dumps({"triage_session_attach": triage_attach}, indent=2))
        if not triage_attach.get("ok"):
            print("TRIAGE_SESSION_ATTACH_FAIL", file=sys.stderr)
            exit_code = max(exit_code, 4)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
