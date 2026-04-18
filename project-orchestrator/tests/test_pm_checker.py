#!/usr/bin/env python3
"""Tests for pm-checker.py compliance checker."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pm-checker.py"
ORCHESTRATOR = Path(__file__).resolve().parent.parent / "scripts" / "orchestrator.py"

# We need a temporary workspace with PROJECTS.yaml for testing
def make_temp_workspace(projects_yaml_content, project_files=None):
    """Create a temporary workspace directory with PROJECTS.yaml and optional files."""
    tmpdir = tempfile.mkdtemp(prefix="pm-checker-test-")
    
    # Write PROJECTS.yaml
    projects_path = Path(tmpdir) / "PROJECTS.yaml"
    projects_path.write_text(projects_yaml_content)
    
    # Write project files
    if project_files:
        projects_dir = Path(tmpdir) / "projects"
        projects_dir.mkdir(exist_ok=True)
        for name, content in project_files.items():
            (projects_dir / name).write_text(content)
    
    return tmpdir


def run_checker(workspace, project_name, verbose=False):
    """Run pm-checker.py and return parsed JSON output."""
    cmd = [sys.executable, str(SCRIPT), "check", project_name]
    if verbose:
        cmd.append("--verbose")
    
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = workspace
    
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    
    try:
        return json.loads(result.stdout), result.returncode
    except json.JSONDecodeError:
        return {"error": result.stdout + result.stderr}, result.returncode


def run_orchestrator(workspace, *args):
    """Run orchestrator.py and return parsed JSON output."""
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = workspace

    result = subprocess.run(
        [sys.executable, str(ORCHESTRATOR), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    try:
        return json.loads(result.stdout), result.returncode
    except json.JSONDecodeError:
        return {"error": result.stdout + result.stderr}, result.returncode


def _normalize_dynamic_issue_fields(value):
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key in {"stale_seconds", "message"} and value.get("code") == "child_task_stale":
                continue
            normalized[key] = _normalize_dynamic_issue_fields(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_dynamic_issue_fields(item) for item in value]
    return value


def test_project_not_found():
    """Test checker with non-existent project."""
    ws = make_temp_workspace("projects: {}")
    output, code = run_checker(ws, "nonexistent")
    assert code == 1, f"Expected exit code 1, got {code}"
    assert output.get("ok") == False
    print("  PASS: project not found")


def test_missing_summary():
    """Test checker flags missing summary file."""
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
"""
    ws = make_temp_workspace(yaml_content)
    # Don't create the summary file
    output, code = run_checker(ws, "test-proj")
    
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "SUMMARY_FILE_MISSING" in codes, f"Expected SUMMARY_FILE_MISSING, got {codes}"
    print("  PASS: missing summary detected")


def test_missing_linear_project():
    """Test checker flags missing Linear project ID."""
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    summary: projects/test-proj.md
    state_history:
      - state: BRIEF
        entered_at: "2026-01-01T00:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n## Brief\nTest brief."})
    output, code = run_checker(ws, "test-proj")
    
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "LINEAR_PROJECT_MISSING" in codes, f"Expected LINEAR_PROJECT_MISSING, got {codes}"
    print("  PASS: missing linear project detected")


def test_stale_state():
    """Test checker flags state that's been active too long."""
    yaml_content = """projects:
  test-proj:
    state: INTAKE
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2025-01-01T00:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n"})
    output, code = run_checker(ws, "test-proj")
    
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "STATE_STALE" in codes, f"Expected STATE_STALE, got {codes}"
    print("  PASS: stale state detected")


def test_missing_review_in_approval_gate():
    """Test checker flags missing inter-agent review when required."""
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2025-01-01T00:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n## Brief\nTest brief."})
    output, code = run_checker(ws, "test-proj")
    
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "REVIEW_NOT_STARTED" in codes, f"Expected REVIEW_NOT_STARTED, got {codes}"
    print("  PASS: missing review detected in approval gate")


def test_missing_artifact_issue():
    """Test checker flags missing artifact issue in approval gate state."""
    yaml_content = """projects:
  test-proj:
    state: PLAN
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
      - state: PLAN
        entered_at: "2025-01-01T00:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nTest plan with MET-1234."
    })
    output, code = run_checker(ws, "test-proj")
    
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "ARTIFACT_ISSUE_MISSING" in codes, f"Expected ARTIFACT_ISSUE_MISSING, got {codes}"
    print("  PASS: missing artifact issue detected")


def test_review_receipts_suppress_missing_review_file_violation():
    """Structured receipts should suppress REVIEW_NOT_STARTED when the gate is already satisfied."""
    yaml_content = """projects:
  test-proj:
    state: REVIEW
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
        artifact_issue_id: fake-artifact-1
        project_update_id: fake-update-1
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-01-01T00:03:00Z"
      - state: REVIEW
        entered_at: "2025-01-01T00:00:00Z"
        artifact_issue_id: fake-artifact-3
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234.\n## Review\nReviewed build slice."
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws

    for role in ("producer", "critic", "pm"):
        subprocess.run([
            sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
            "--kind", "child",
            "--role", role,
            "--status", "approved",
            "--note", f"{role} signed off on REVIEW artifact"
        ], check=True, capture_output=True, text=True, env=env, timeout=30)

    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
        "--kind", "pm_session",
        "--role", "pm",
        "--status", "active",
        "--session-label", "pm-review-final"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj", verbose=True)
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "REVIEW_NOT_STARTED" not in codes, f"Unexpected REVIEW_NOT_STARTED, got {codes}"
    assert output.get("state_info", {}).get("inter_agent_review", {}).get("gate_satisfied") is True, output
    print("  PASS: satisfied review receipts suppress missing review file false positive")



def test_ship_missing_sections():
    """Test checker flags missing required sections in SHIP state."""
    yaml_content = """projects:
  test-proj:
    state: SHIP
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: SHIP
        entered_at: "2026-01-01T00:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan."
    })
    output, code = run_checker(ws, "test-proj")
    
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert any("SHIP_MISSING" in c for c in codes), f"Expected SHIP_MISSING_*, got {codes}"
    print("  PASS: missing SHIP sections detected")


def test_compliant_build():
    """Test checker returns compliant for a well-configured BUILD project (without Linear API)."""
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: patch
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-03-29T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-03-29T00:01:00Z"
        artifact_issue_id: fake-artifact-1
        project_update_id: fake-update-1
      - state: PLAN
        entered_at: "2026-03-29T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-03-29T09:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234."
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--task-id", "build-stage-owner",
        "--owner", "pm",
        "--status", "active",
        "--label", "PM BUILD stage owner",
        "--heartbeat-at", "2026-03-29T09:15:00Z",
        "--current-step", "Driving BUILD forward",
        "--summary", "Active PM relay owns BUILD",
        "--session-label", "pm-test-proj-build"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    output, code = run_checker(ws, "test-proj")
    
    # Should only have MINOR violations (Linear API unreachable with fake ID)
    violations = output.get("violations", [])
    blocking = [v for v in violations if v["severity"] == "BLOCKING"]
    significant = [v for v in violations if v["severity"] == "SIGNIFICANT"]
    
    # No blocking or significant (Linear check will be MINOR since fake ID)
    assert len(blocking) == 0, f"Unexpected blocking violations: {blocking}"
    # significant could include BUILD_ALL_BACKLOG if Linear returns data, but with fake ID it won't
    print(f"  PASS: BUILD state compliant (blocking={len(blocking)}, significant={len(significant)})")


def test_review_loop_decision_missing_violation_detected():
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest brief.",
        "test-proj-review-brief-round4.md": "# Legacy round 4\n",
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "backfill-review-loop", "test-proj"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj")
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "REVIEW_LOOP_DECISION_MISSING" in codes, f"Expected REVIEW_LOOP_DECISION_MISSING, got {codes}"
    print("  PASS: review loop decision-missing violation detected")


def test_freeze_artifact_missing_violation_detected():
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n## Brief\nTest brief."})
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-review-loop", "test-proj",
        "--current-round", "3", "--max-rounds", "3", "--freeze-required",
        "--decision", "FREEZE_AND_ESCALATE",
        "--checkpoint-summary", "Round cap hit.",
        "--checkpoint-file", "projects/test-proj-review-brief-round3.md",
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj")
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "REVIEW_LOOP_FREEZE_ARTIFACT_MISSING" in codes, f"Expected REVIEW_LOOP_FREEZE_ARTIFACT_MISSING, got {codes}"
    print("  PASS: freeze artifact missing violation detected")



def test_stage_boundary_drift_violation_detected():
    yaml_content = """projects:
  test-proj:
    state: PLAN
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
"""
    ws = make_temp_workspace(yaml_content, {"test-proj.md": "# test-proj\n## Plan\nTest plan."})
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-review-loop", "test-proj",
        "--state", "PLAN",
        "--current-round", "1", "--max-rounds", "3",
        "--checkpoint-summary", "Critic drifted into implementation details.",
        "--checkpoint-file", "projects/test-proj-review-plan-round1.md",
        "--unresolved-issues-json", '[{"title":"Rename helper functions in workflow runner","severity":"SIGNIFICANT"}]'
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj")
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "REVIEW_STAGE_BOUNDARY_DRIFT" in codes, f"Expected REVIEW_STAGE_BOUNDARY_DRIFT, got {codes}"
    print("  PASS: stage boundary drift violation detected")


def test_child_task_backfill_recommended_violation_detected():
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-04-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-04-01T00:01:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n"
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
        "--kind", "child",
        "--role", "producer",
        "--status", "approved"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj")
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "CHILD_TASK_BACKFILL_RECOMMENDED" in codes, f"Expected CHILD_TASK_BACKFILL_RECOMMENDED, got {codes}"
    print("  PASS: child task backfill recommendation detected")


def test_child_task_stale_violation_detected():
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: BUILD
        entered_at: "2026-04-01T00:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234.\n## Verified API Schemas\n- none\n"
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--task-id", "worker-1",
        "--status", "active",
        "--heartbeat-at", "2026-04-01T00:00:00Z",
        "--summary", "Waiting on stalled subagent"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj")
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "CHILD_TASK_STALE" in codes, f"Expected CHILD_TASK_STALE, got {codes}"
    print("  PASS: child task stale violation detected")


def test_child_task_attention_required_violation_detected():
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: BUILD
        entered_at: "2026-04-01T00:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234.\n## Verified API Schemas\n- none\n"
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--task-id", "worker-1",
        "--status", "blocked",
        "--heartbeat-at", "2099-04-01T00:00:00Z",
        "--blocked-reason", "Waiting on PM decision",
        "--attention-required",
        "--summary", "Blocked on explicit PM input"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj")
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "CHILD_TASK_ATTENTION_REQUIRED" in codes, f"Expected CHILD_TASK_ATTENTION_REQUIRED, got {codes}"
    print("  PASS: child task attention-required violation detected")


def test_pm_owner_missing_violation_detected():
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
        artifact_issue_id: fake-artifact-1
        project_update_id: fake-update-1
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-01-01T00:03:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234."
    })
    output, code = run_checker(ws, "test-proj", verbose=True)
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "PM_OWNER_MISSING" in codes, f"Expected PM_OWNER_MISSING, got {codes}"
    assert output.get("state_info", {}).get("pm_continuity", {}).get("stage_owner_task_count") == 0, output
    print("  PASS: PM owner missing violation detected")


def test_pm_owner_terminal_without_stage_exit_detected():
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
        artifact_issue_id: fake-artifact-1
        project_update_id: fake-update-1
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-01-01T00:03:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234."
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--task-id", "build-stage-owner",
        "--owner", "pm",
        "--status", "completed",
        "--label", "PM BUILD stage owner",
        "--heartbeat-at", "2026-01-01T00:20:00Z",
        "--current-step", "Exited early",
        "--summary", "PM run finished before build completion",
        "--session-label", "pm-test-proj-build"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj", verbose=True)
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "PM_OWNER_ENDED_BEFORE_STAGE_EXIT" in codes, f"Expected PM_OWNER_ENDED_BEFORE_STAGE_EXIT, got {codes}"
    assert output.get("state_info", {}).get("pm_continuity", {}).get("stage_owner_task_count") == 1, output
    print("  PASS: terminal PM owner without stage exit detected")


def test_operator_approval_only_remaining_suppresses_pm_owner_violation():
    yaml_content = """projects:
  test-proj:
    state: REVIEW
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
        artifact_issue_id: fake-artifact-1
        project_update_id: fake-update-1
      - state: PLAN
        entered_at: "2026-01-01T00:02:00Z"
        artifact_issue_id: fake-artifact-2
        project_update_id: fake-update-2
      - state: BUILD
        entered_at: "2026-01-01T00:03:00Z"
      - state: REVIEW
        entered_at: "2026-01-01T00:20:00Z"
        artifact_issue_id: fake-artifact-3
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234.\n## Review\nReviewed build slice."
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    for role in ("producer", "critic", "pm"):
        subprocess.run([
            sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
            "--kind", "child",
            "--role", role,
            "--status", "approved",
            "--note", f"{role} signed off on REVIEW artifact"
        ], check=True, capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
        "--kind", "pm_session",
        "--role", "pm",
        "--status", "completed",
        "--session-label", "pm-test-proj-review"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
        "--kind", "artifact",
        "--artifact", "pr_with_review",
        "--status", "verified",
        "--note", "PR-with-review evidence verified before operator approval"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    output, code = run_checker(ws, "test-proj", verbose=True)
    violations = output.get("violations", [])
    codes = [v["code"] for v in violations]
    assert "PM_OWNER_MISSING" not in codes, f"Unexpected PM_OWNER_MISSING, got {codes}"
    assert "PM_OWNER_ENDED_BEFORE_STAGE_EXIT" not in codes, f"Unexpected PM_OWNER_ENDED_BEFORE_STAGE_EXIT, got {codes}"
    assert output.get("state_info", {}).get("pm_continuity", {}).get("operator_approval_only_remaining") is True, output
    print("  PASS: operator-approval-only state suppresses PM owner violation")


def test_verbose_includes_state_info():
    """Test that --verbose includes extra state information."""
    yaml_content = """projects:
  test-proj:
    state: BUILD
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: BUILD
        entered_at: "2026-03-29T09:00:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest.\n## Plan\nPlan with MET-1234."
    })
    output, code = run_checker(ws, "test-proj", verbose=True)
    
    assert "state_info" in output, "Verbose output should include state_info"
    assert "time_in_state_minutes" in output["state_info"], "Should include time_in_state_minutes"
    print("  PASS: verbose includes state info")


def test_verbose_includes_review_loop_and_child_watchdog_state():
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest brief."
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-review-checkpoint", "test-proj",
        "--current-round", "3", "--max-rounds", "3",
        "--summary", "Round cap reached.",
        "--decision", "FREEZE_AND_ESCALATE",
        "--freeze-required",
        "--output", "projects/test-proj-review-checkpoint-brief-round3.md"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--task-id", "worker-1",
        "--status", "blocked",
        "--heartbeat-at", "2026-01-01T00:01:00Z",
        "--blocked-reason", "Waiting on PM decision",
        "--summary", "Blocked on explicit PM input"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    status_output, code = run_orchestrator(ws, "status", "test-proj", "--verbose")
    assert code == 0, status_output
    review_output, code = run_orchestrator(ws, "review-status", "test-proj")
    assert code == 0, review_output
    watchdog_output, code = run_orchestrator(ws, "child-task-watchdog", "test-proj")
    assert code == 0, watchdog_output
    output, code = run_checker(ws, "test-proj", verbose=True)
    state_info = output.get("state_info", {})
    review_loop = state_info.get("review_loop")
    child_watchdog = state_info.get("child_task_watchdog")

    assert review_loop, f"Expected review_loop state in verbose output, got {state_info}"
    assert review_loop["current_round"] == 3, review_loop
    assert review_loop["freeze_required"] is True, review_loop
    assert review_loop["decision"] == "FREEZE_AND_ESCALATE", review_loop

    assert child_watchdog, f"Expected child_task_watchdog state in verbose output, got {state_info}"
    assert child_watchdog["should_alert"] is True, child_watchdog
    assert child_watchdog["summary"]["blocked"] == 1, child_watchdog
    assert child_watchdog["exceptions"][0]["code"] == "child_task_stale", child_watchdog
    assert child_watchdog["stale_after_minutes"] == watchdog_output["stale_after_minutes"], state_info
    assert child_watchdog["exceptions_only"] == watchdog_output["exceptions_only"], state_info
    assert child_watchdog["should_alert"] == watchdog_output["should_alert"], state_info
    assert child_watchdog["summary"] == watchdog_output["summary"], state_info
    assert child_watchdog["child_tasks_path"] == watchdog_output["child_tasks_path"], state_info
    assert _normalize_dynamic_issue_fields(child_watchdog["exceptions"]) == _normalize_dynamic_issue_fields(watchdog_output["exceptions"]), state_info
    inter_agent_review = state_info.get("inter_agent_review")
    assert inter_agent_review, f"Expected inter_agent_review state in verbose output, got {state_info}"
    assert inter_agent_review["required"] is True, inter_agent_review
    assert review_output["review_files"] == [], review_output
    assert review_output["review_loop"]["checkpoint"]["file"] == "projects/test-proj-review-checkpoint-brief-round3.md", review_output
    assert review_loop == status_output["review_loop"], state_info
    assert review_loop == review_output["review_loop"], state_info
    assert inter_agent_review["required"] == status_output["inter_agent_review"]["required"], state_info
    assert inter_agent_review["review_files"] == status_output["inter_agent_review"]["review_files"], state_info
    assert inter_agent_review["review_files"] == review_output["review_files"], inter_agent_review
    assert _normalize_dynamic_issue_fields(inter_agent_review["issues"]) == _normalize_dynamic_issue_fields(status_output["inter_agent_review"]["issues"]), state_info
    print("  PASS: verbose includes review-loop and child watchdog state")


def test_verbose_shared_contract_matches_orchestrator_surfaces():
    yaml_content = """projects:
  test-proj:
    state: BRIEF
    tier: feature
    linear_project_id: fake-id
    summary: projects/test-proj.md
    state_history:
      - state: INTAKE
        entered_at: "2026-01-01T00:00:00Z"
      - state: BRIEF
        entered_at: "2026-01-01T00:01:00Z"
"""
    ws = make_temp_workspace(yaml_content, {
        "test-proj.md": "# test-proj\n## Brief\nTest brief."
    })
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = ws
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-review-checkpoint", "test-proj",
        "--current-round", "3", "--max-rounds", "3",
        "--summary", "Round cap reached.",
        "--decision", "FREEZE_AND_ESCALATE",
        "--freeze-required",
        "--output", "projects/test-proj-review-checkpoint-brief-round3.md"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-freeze-artifact", "test-proj",
        "--summary", "Freeze at cap with explicit carry-forward items.",
        "--rationale", "PM accepted carry-forward into PLAN.",
        "--checkpoint-file", "projects/test-proj-review-checkpoint-brief-round3.md",
        "--unresolved-issues-json", '[{"title":"Sequence rollout before cleanup completion","severity":"SIGNIFICANT"}]',
        "--accepted-risks-json", '["Sequencing risk accepted for PLAN handoff"]',
        "--carry-forward-json", '["Confirm rollout order during PLAN"]'
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-child-task", "test-proj",
        "--task-id", "worker-1",
        "--status", "blocked",
        "--heartbeat-at", "2026-01-01T00:01:00Z",
        "--blocked-reason", "Waiting on PM decision",
        "--summary", "Blocked on explicit PM input"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
        "--kind", "child",
        "--role", "pm",
        "--status", "approved",
        "--note", "PM accepts frozen-cap handoff",
        "--review-file", "projects/test-proj-review-checkpoint-brief-round3.md"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([
        sys.executable, str(ORCHESTRATOR), "record-receipt", "test-proj",
        "--kind", "pm_session",
        "--role", "pm",
        "--status", "active",
        "--session-label", "pm-brief-frozen-cap"
    ], check=True, capture_output=True, text=True, env=env, timeout=30)

    status_output, code = run_orchestrator(ws, "status", "test-proj", "--verbose")
    assert code == 0, status_output
    validate_output, code = run_orchestrator(ws, "validate", "test-proj")
    assert code == 2, validate_output
    review_output, code = run_orchestrator(ws, "review-status", "test-proj")
    assert code == 0, review_output
    watchdog_output, code = run_orchestrator(ws, "child-task-watchdog", "test-proj")
    assert code == 0, watchdog_output
    checker_output, code = run_checker(ws, "test-proj", verbose=True)
    assert "state_info" in checker_output, checker_output
    state_info = checker_output["state_info"]

    assert state_info["review_loop"] == status_output["review_loop"], state_info
    assert state_info["review_loop"] == validate_output["review_loop"], state_info
    assert state_info["review_loop"] == review_output["review_loop"], state_info
    assert state_info["child_task_watchdog"]["stale_after_minutes"] == watchdog_output["stale_after_minutes"], state_info
    assert state_info["child_task_watchdog"]["exceptions_only"] == watchdog_output["exceptions_only"], state_info
    assert state_info["child_task_watchdog"]["should_alert"] == watchdog_output["should_alert"], state_info
    assert state_info["child_task_watchdog"]["summary"] == watchdog_output["summary"], state_info
    assert state_info["child_task_watchdog"]["child_tasks_path"] == watchdog_output["child_tasks_path"], state_info
    assert _normalize_dynamic_issue_fields(state_info["child_task_watchdog"]["exceptions"]) == _normalize_dynamic_issue_fields(watchdog_output["exceptions"]), state_info
    assert review_output["review_files"] == ["projects/test-proj-review-checkpoint-brief-round3.md"], review_output
    assert review_output["review_loop"]["checkpoint"]["file"] == "projects/test-proj-review-checkpoint-brief-round3.md", review_output

    review_contract = state_info["inter_agent_review"]
    assert review_contract["required"] == review_output["inter_agent_review_required"], review_contract
    assert review_contract["producer_role"] == review_output["producer_role"], review_contract
    assert review_contract["critic_role"] == review_output["critic_role"], review_contract
    assert review_contract["pm_signoff_required"] == review_output["pm_signoff_required"], review_contract
    assert review_contract["review_files"] == review_output["review_files"], review_contract
    assert review_contract["signed_off"] == review_output["signed_off"], review_contract
    assert review_contract["gate_satisfied"] == review_output["gate_satisfied"], review_contract
    assert review_contract["full_signoff_complete"] == review_output["full_signoff_complete"], review_contract
    assert review_contract["frozen_cap_waiver_active"] == review_output["frozen_cap_waiver_active"], review_contract
    assert review_contract["waived_roles"] == review_output["waived_roles"], review_contract
    assert review_contract["pm_signed_off"] == review_output["pm_signed_off"], review_contract
    assert review_contract["child_receipts"] == review_output["child_receipts"], review_contract
    assert review_contract["pm_session_receipt"] == review_output["pm_session_receipt"], review_contract
    assert _normalize_dynamic_issue_fields(review_contract["issues"]) == _normalize_dynamic_issue_fields(status_output["inter_agent_review"]["issues"]), review_contract
    assert _normalize_dynamic_issue_fields(review_contract["issues"]) == _normalize_dynamic_issue_fields(validate_output["inter_agent_review"]["issues"]), review_contract
    print("  PASS: pm-checker verbose shared contract matches orchestrator surfaces")


def main():
    print("Running pm-checker tests...\n")
    
    tests = [
        test_project_not_found,
        test_missing_summary,
        test_missing_linear_project,
        test_stale_state,
        test_missing_review_in_approval_gate,
        test_missing_artifact_issue,
        test_review_receipts_suppress_missing_review_file_violation,
        test_ship_missing_sections,
        test_compliant_build,
        test_review_loop_decision_missing_violation_detected,
        test_freeze_artifact_missing_violation_detected,
        test_stage_boundary_drift_violation_detected,
        test_child_task_backfill_recommended_violation_detected,
        test_child_task_stale_violation_detected,
        test_child_task_attention_required_violation_detected,
        test_pm_owner_missing_violation_detected,
        test_pm_owner_terminal_without_stage_exit_detected,
        test_operator_approval_only_remaining_suppresses_pm_owner_violation,
        test_verbose_includes_state_info,
        test_verbose_includes_review_loop_and_child_watchdog_state,
        test_verbose_shared_contract_matches_orchestrator_surfaces,
    ]
    
    passed = 0
    failed = 0
    
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
    
    print(f"\nResults: {passed} passed, {failed} failed, {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
