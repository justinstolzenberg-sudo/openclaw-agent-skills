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
    output, code = run_checker(ws, "test-proj")
    
    # Should only have MINOR violations (Linear API unreachable with fake ID)
    violations = output.get("violations", [])
    blocking = [v for v in violations if v["severity"] == "BLOCKING"]
    significant = [v for v in violations if v["severity"] == "SIGNIFICANT"]
    
    # No blocking or significant (Linear check will be MINOR since fake ID)
    assert len(blocking) == 0, f"Unexpected blocking violations: {blocking}"
    # significant could include BUILD_ALL_BACKLOG if Linear returns data, but with fake ID it won't
    print(f"  PASS: BUILD state compliant (blocking={len(blocking)}, significant={len(significant)})")


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


def main():
    print("Running pm-checker tests...\n")
    
    tests = [
        test_project_not_found,
        test_missing_summary,
        test_missing_linear_project,
        test_stale_state,
        test_missing_review_in_approval_gate,
        test_missing_artifact_issue,
        test_ship_missing_sections,
        test_compliant_build,
        test_verbose_includes_state_info,
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
