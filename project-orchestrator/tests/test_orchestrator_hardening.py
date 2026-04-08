#!/usr/bin/env python3
"""Hardening tests for project-orchestrator.py."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ORCHESTRATOR = Path(__file__).resolve().parent.parent / "scripts" / "orchestrator.py"


def make_temp_workspace(project_name, project_data, summary_files=None):
    tmpdir = Path(tempfile.mkdtemp(prefix="orchestrator-hardening-"))
    (tmpdir / "projects").mkdir(exist_ok=True)
    (tmpdir / "PROJECTS.yaml").write_text(json.dumps({"projects": {project_name: project_data}}, indent=2))
    for rel_path, content in (summary_files or {}).items():
        full_path = tmpdir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
    return tmpdir


def run_orchestrator(workspace, *args, expect_json=True):
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = str(workspace)
    cmd = [sys.executable, str(ORCHESTRATOR), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if expect_json:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"Invalid JSON output for {cmd}: {result.stdout}\n{result.stderr}") from exc
        return payload, result.returncode
    return result.stdout, result.returncode


def load_projects_yaml(workspace):
    content = (Path(workspace) / "PROJECTS.yaml").read_text()
    if yaml:
        return yaml.safe_load(content)
    return json.loads(content)


def brief_project(project_name):
    return {
        "state": "BRIEF",
        "tier": "patch",
        "summary": f"projects/{project_name}.md",
        "linear_project_id": None,
        "state_history": [
            {"state": "INTAKE", "entered_at": "2026-04-01T00:00:00Z"},
            {"state": "BRIEF", "entered_at": "2026-04-01T00:01:00Z"},
        ],
    }


def build_project(project_name):
    return {
        "state": "BUILD",
        "tier": "patch",
        "summary": f"projects/{project_name}.md",
        "linear_project_id": None,
        "state_history": [
            {"state": "INTAKE", "entered_at": "2026-04-01T00:00:00Z"},
            {"state": "BRIEF", "entered_at": "2026-04-01T00:01:00Z"},
            {"state": "PLAN", "entered_at": "2026-04-01T00:02:00Z"},
            {"state": "BUILD", "entered_at": "2026-04-01T00:03:00Z"},
        ],
    }


def design_project(project_name):
    return {
        "state": "DESIGN",
        "tier": "feature",
        "summary": f"projects/{project_name}.md",
        "linear_project_id": None,
        "state_history": [
            {"state": "INTAKE", "entered_at": "2026-04-01T00:00:00Z"},
            {"state": "BRIEF", "entered_at": "2026-04-01T00:01:00Z"},
            {"state": "DESIGN", "entered_at": "2026-04-01T00:02:00Z"},
        ],
    }


def write_design_bundle(project_name, spec_payload):
    spec_dir = Path("/tmp") / f"{project_name}-design"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "wireframes").mkdir(exist_ok=True)
    (spec_dir / "design-spec.json").write_text(json.dumps(spec_payload, indent=2))
    (spec_dir / "wireframes" / "screen-a.svg").write_text("<svg><text>screen-a</text></svg>")
    return spec_dir


def record_brief_transition_receipts(workspace, project_name):
    steps = [
        ("record-receipt", project_name, "--kind", "child", "--role", "producer", "--review-file", f"projects/{project_name}-review-brief-round1.md"),
        ("record-receipt", project_name, "--kind", "child", "--role", "critic", "--review-file", f"projects/{project_name}-review-brief-round1.md"),
        ("record-receipt", project_name, "--kind", "child", "--role", "pm", "--review-file", f"projects/{project_name}-review-brief-round1.md"),
        ("record-receipt", project_name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-1"),
        ("record-receipt", project_name, "--kind", "approval", "--role", "operator"),
    ]
    for step in steps:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload


def test_transition_blocked_when_approval_receipt_missing():
    name = "missing-approval"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "producer"),
        ("record-receipt", name, "--kind", "child", "--role", "critic"),
        ("record-receipt", name, "--kind", "child", "--role", "pm"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-1"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    assert payload["ok"] is False
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert "approval_receipt" in failed, failed
    print("  PASS: transition blocked for missing approval receipt")


def test_transition_blocks_stale_approval_and_child_receipts():
    name = "stale-approval"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nOriginal brief text.\n"},
    )

    record_brief_transition_receipts(workspace, name)

    summary_path = Path(workspace) / "projects" / f"{name}.md"
    summary_path.write_text(f"# {name}\n\n## Brief\nOriginal brief text changed after approval.\n")

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert failed["approval_receipt"]["stale_receipt"], failed
    assert failed["child_receipts"]["stale_roles"], failed
    print("  PASS: stale approval and child receipts rejected")


def test_design_receipts_bind_to_design_bundle_and_go_stale_on_spec_change():
    name = "design-stale-approval"
    workspace = make_temp_workspace(
        name,
        design_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Design\nDesign summary only.\n"},
    )
    write_design_bundle(name, {"screens": [{"screen_id": "welcome", "title": "Welcome"}]})

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "producer"),
        ("record-receipt", name, "--kind", "child", "--role", "critic"),
        ("record-receipt", name, "--kind", "child", "--role", "pm"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-design-1"),
        ("record-receipt", name, "--kind", "approval", "--role", "operator"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload
        if step[-1] == "operator":
            assert payload["receipt"]["artifact_hash"], payload

    approval_receipt, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, approval_receipt
    assert approval_receipt["artifact_subject"]["kind"] == "design_bundle", approval_receipt

    spec_path = Path("/tmp") / f"{name}-design" / "design-spec.json"
    spec_path.write_text(json.dumps({"screens": [{"screen_id": "welcome", "title": "Changed after sign-off"}]}, indent=2))

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert failed["approval_receipt"]["stale_receipt"], failed
    assert failed["child_receipts"]["stale_roles"], failed
    print("  PASS: DESIGN receipts bind to design bundle and stale spec changes are rejected")


def test_transition_blocked_when_child_receipts_missing():
    name = "missing-child-receipts"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    for step in [
        ("record-receipt", name, "--kind", "approval", "--role", "operator"),
        ("record-receipt", name, "--kind", "child", "--role", "producer"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-1"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert set(failed["child_receipts"]["missing_roles"]) == {"critic", "pm"}, failed
    print("  PASS: transition blocked for missing child receipts")


def test_transition_blocked_when_validation_missing():
    name = "missing-validation"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\nChecked.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-receipt", name,
        "--kind", "artifact",
        "--artifact", "test_results",
        "--metadata-json", '{"suite":"unit"}'
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "REVIEW")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert "validation_passed" in failed, failed
    assert "code_on_branch" in failed["validation_passed"]["missing"], failed
    print("  PASS: transition blocked for missing validation artifacts")


def test_transition_succeeds_with_current_structured_receipts():
    name = "brief-success"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    record_brief_transition_receipts(workspace, name)

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["new_state"] == "PLAN"
    assert "artifact_warnings" not in payload, payload
    assert all(item["satisfied"] for item in payload["transition_preconditions"]), payload
    print("  PASS: BRIEF -> PLAN succeeds with valid receipts")


def test_build_transition_succeeds_with_artifact_receipts():
    name = "build-success"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\nChecked.\n"},
    )

    for artifact, metadata in [
        ("code_on_branch", '{"branch":"feature/test"}'),
        ("test_results", '{"suite":"unit","status":"pass"}'),
    ]:
        payload, code = run_orchestrator(
            workspace,
            "record-receipt", name,
            "--kind", "artifact",
            "--artifact", artifact,
            "--metadata-json", metadata,
        )
        assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "transition", name, "REVIEW",
        "--actor-id", "operator-123",
        "--actor-role", "pa",
        "--session-id", "sess-123",
        "--request-id", "req-456",
        "--channel", "telegram",
    )
    assert code == 0, payload
    assert payload["new_state"] == "REVIEW"

    updated = load_projects_yaml(workspace)
    history = updated["projects"][name]["state_history"]
    new_entry = history[-1]
    assert new_entry["recorded_by"]["actor_id"] == "operator-123", new_entry
    assert new_entry["recorded_by"]["session_id"] == "sess-123", new_entry
    assert new_entry["recorded_by"]["request_id"] == "req-456", new_entry
    assert new_entry["recorded_by"]["channel"] == "telegram", new_entry
    print("  PASS: BUILD -> REVIEW succeeds and records real audit metadata")


def main():
    print("Running orchestrator hardening tests...\n")

    tests = [
        test_transition_blocked_when_approval_receipt_missing,
        test_transition_blocks_stale_approval_and_child_receipts,
        test_design_receipts_bind_to_design_bundle_and_go_stale_on_spec_change,
        test_transition_blocked_when_child_receipts_missing,
        test_transition_blocked_when_validation_missing,
        test_transition_succeeds_with_current_structured_receipts,
        test_build_transition_succeeds_with_artifact_receipts,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {test.__name__}: {exc}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed, {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
