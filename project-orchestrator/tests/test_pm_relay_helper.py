#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "scripts" / "pm-relay-helper.py"
ORCHESTRATOR = Path(__file__).resolve().parent.parent / "scripts" / "orchestrator.py"


def make_temp_workspace(projects_yaml_content, project_files=None):
    tmpdir = tempfile.mkdtemp(prefix="pm-relay-helper-test-")
    (Path(tmpdir) / "PROJECTS.yaml").write_text(projects_yaml_content)
    if project_files:
        projects_dir = Path(tmpdir) / "projects"
        projects_dir.mkdir(exist_ok=True)
        for name, content in project_files.items():
            (projects_dir / name).write_text(content)
    return tmpdir


def run_helper(workspace, *args):
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = workspace
    result = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=env, timeout=30)
    return json.loads(result.stdout), result.returncode


def run_orchestrator(workspace, *args):
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = workspace
    result = subprocess.run([sys.executable, str(ORCHESTRATOR), *args], capture_output=True, text=True, env=env, timeout=30)
    return json.loads(result.stdout), result.returncode


def test_check_reports_missing_pm_owner():
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-01-01T00:03:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n## Plan\nPlan."})
    output, code = run_helper(ws, "check", "test-proj")
    assert code == 0, output
    assert output["should_respawn"] is True, output
    assert output["violations"][0]["code"] == "PM_OWNER_MISSING", output
    print("  PASS: helper flags missing PM owner")


def test_check_suppresses_when_active_pm_owner_exists():
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-01-01T00:03:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n## Plan\nPlan."})
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--task-id", "build-stage-owner",
        "--owner", "pm",
        "--status", "active",
        "--label", "PM BUILD stage owner",
        "--heartbeat-at", "2099-01-01T00:20:00Z",
        "--session-label", "pm-test-proj-build"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    output, code = run_helper(ws, "check", "test-proj")
    assert code == 0, output
    assert output["should_respawn"] is False, output
    assert output["reason"] == "active_pm_owner_present", output
    print("  PASS: helper suppresses respawn when active PM exists")


def test_check_reports_real_blocker_instead_of_active_owner_for_blocked_stage_owner():
    yaml_content = """projects:
  test-proj:
    state: SHIP
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: REVIEW
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: SHIP
        entered_at: "2026-01-01T00:03:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n## Ship\nShip."})
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--state", "SHIP",
        "--task-id", "pm-stage-owner-ship",
        "--owner", "pm",
        "--status", "blocked",
        "--label", "PM SHIP stage owner",
        "--heartbeat-at", "2026-01-01T00:20:00Z",
        "--attention-required",
        "--blocked-reason", "Human E2E still missing",
        "--session-label", "pm-test-proj-ship"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    output, code = run_helper(ws, "check", "test-proj")
    assert code == 0, output
    assert output["should_respawn"] is False, output
    assert output["reason"] == "real_blocker_present", output
    assert "Human E2E still missing" in output["blocked_reason"], output
    print("  PASS: helper surfaces real blocker instead of claiming active PM owner")


def test_sweep_returns_exit_2_when_any_project_needs_respawn():
    yaml_content = """projects:
  one:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/one.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-01-01T00:03:00Z"
  two:
    state: CLOSED
    tier: feature
    linear_project_id: fake-id
    summary: projects/two.md
    state_history:
      - state: CLOSED
        entered_at: "2026-01-01T00:03:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"one.md": "# one\n## Plan\nPlan.", "two.md": "# two"})
    output, code = run_helper(ws, "sweep")
    assert code == 2, output
    assert output["respawn_needed"] == 1, output
    print("  PASS: helper sweep exits 2 when respawn is needed")


def main():
    print("Running pm-relay-helper tests...\n")
    tests = [
        test_check_reports_missing_pm_owner,
        test_check_suppresses_when_active_pm_owner_exists,
        test_check_reports_real_blocker_instead_of_active_owner_for_blocked_stage_owner,
        test_sweep_returns_exit_2_when_any_project_needs_respawn,
    ]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test.__name__}: {e}")
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed, {passed+failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
