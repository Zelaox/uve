"""Run a behavioral regression through UVE in three isolated fixture workspaces."""

from pathlib import Path
from tempfile import mkdtemp

from agent_training.manifest_loader import pipeline_root
from agent_training.uve.bundle import resolve_log_dir, write_json_atomic
from agent_training.uve.orchestrator import run_probe


def main() -> int:
    engine = run_probe("config/uve/profiles/emulator_workstation_smoke.json", "engine_self_check")
    if engine["verdict"] != "passed":
        print(f"Engine self-check {engine['verdict']} ({engine['probe_run_id']})")
        return 2

    example_store = resolve_log_dir() / "examples"
    example_store.mkdir(exist_ok=True)
    run_dir = Path(mkdtemp(prefix="command-", dir=example_store))
    check = Path(__file__).with_name("display_name_check.py").read_text(encoding="utf-8")
    print(f"Fixture workspaces and profiles: {run_dir}")
    matched = True
    for phase, token_index, expected in (
        ("original", 0, "passed"),
        ("regression", 1, "failed"),
        ("restored", 0, "passed"),
    ):
        workspace = run_dir / phase
        workspace.mkdir()
        (workspace / "app.py").write_text(
            f"def given_name(display_name):\n    return display_name.split()[{token_index}]\n",
            encoding="utf-8",
        )
        (workspace / "check.py").write_text(check, encoding="utf-8")
        command = workspace / "command.json"
        write_json_atomic(command, {
            "schema_version": "uve_command_probe.v1",
            "argv": ["{python}", "-B", "check.py"],
            "timeout_seconds": 10,
            "expected_returncode": 0,
            "expected_stdout": "display name check passed",
        })
        profile = workspace / "profile.json"
        write_json_atomic(profile, {
            "schema_version": "uve_project_profile.v1",
            "project_id": "display_name_example",
            "environment": "example",
            "workspace_root_relative": workspace.relative_to(pipeline_root()).as_posix(),
            "identity": {"watch_globs": ["*.py"], "ignore_globs": []},
            "command_probe_refs": {"command_check": str(command)},
            "verification_probe_ids": ["command_check"],
            "gate_ids": ["command_check"],
        })
        receipt = run_probe(str(profile), "command_check")
        print(f"{phase}: {receipt['verdict']} ({receipt['probe_run_id']})")
        matched = matched and receipt["verdict"] == expected
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
