#!/usr/bin/env python3
"""
Project Manager Compliance Checker

Validates that a project is following all orchestrator requirements for its
current state. Returns a structured JSON report with violations, warnings,
and recommended actions.

Usage:
  python3 pm-checker.py check <project-name>
  python3 pm-checker.py check <project-name> --verbose

Exit codes: 0 = compliant, 1 = error, 2 = violations found
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent

# Reuse workspace detection from orchestrator
def _find_workspace_dir():
    env_ws = os.environ.get("OPENCLAW_WORKSPACE")
    if env_ws:
        return Path(env_ws)
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "PROJECTS.yaml").exists():
            return candidate
    for candidate in [SCRIPT_DIR.parent.parent, Path.home() / ".openclaw" / "workspace"]:
        if (candidate / "PROJECTS.yaml").exists():
            return candidate
    return SCRIPT_DIR.parent.parent

WORKSPACE_DIR = _find_workspace_dir()
PROJECTS_YAML_PATH = WORKSPACE_DIR / "PROJECTS.yaml"
PROJECTS_DIR = WORKSPACE_DIR / "projects"

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

def load_yaml(path):
    with open(path, "r") as f:
        if HAS_YAML:
            return yaml.safe_load(f)
        else:
            return json.load(f)

def load_state_machine():
    return load_yaml(SKILL_DIR / "references" / "state-machine.yaml")

def load_projects():
    return load_yaml(PROJECTS_YAML_PATH)

def now_utc():
    return datetime.now(timezone.utc)

def parse_iso(ts):
    """Parse ISO timestamp, handling various formats."""
    if not ts:
        return None
    try:
        # Handle Z suffix
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


# --- Compliance checks ---

class Violation:
    """A process violation found by the checker."""
    def __init__(self, severity, code, message, action=None, auto_fixable=False):
        self.severity = severity  # BLOCKING, SIGNIFICANT, MINOR
        self.code = code          # Machine-readable code
        self.message = message    # Human-readable description
        self.action = action      # Recommended action
        self.auto_fixable = auto_fixable  # Can PM fix without operator?

    def to_dict(self):
        d = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.action:
            d["action"] = self.action
        if self.auto_fixable:
            d["auto_fixable"] = True
        return d


def check_linear_sync(project, state_info, sm):
    """Verify Linear project status matches expected state."""
    violations = []
    linear_project_id = project.get("linear_project_id")

    if not linear_project_id:
        violations.append(Violation(
            "BLOCKING", "LINEAR_PROJECT_MISSING",
            "No linear_project_id in PROJECTS.yaml. Linear tracking is required.",
            action="Run orchestrator.py init or manually add linear_project_id",
            auto_fixable=False
        ))
        return violations

    expected_linear_state = state_info.get("linear_state", "Unknown")

    # Query Linear for actual status
    token = _get_linear_token()
    if not token:
        violations.append(Violation(
            "SIGNIFICANT", "LINEAR_TOKEN_UNAVAILABLE",
            "Cannot verify Linear sync - no token available.",
            action="Set LINEAR_TOKEN or LINEAR_API_TOKEN"
        ))
        return violations

    try:
        import urllib.request
        query = """query($id: String!) {
            project(id: $id) { status { name } }
        }"""
        req = urllib.request.Request(
            'https://api.linear.app/graphql',
            data=json.dumps({"query": query, "variables": {"id": linear_project_id}}).encode(),
            headers={'Authorization': token, 'Content-Type': 'application/json'}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        actual_status = resp.get("data", {}).get("project", {}).get("status", {}).get("name")

        # Map expected framework state to Linear project status
        STATUS_MAP = {
            "Backlog": "Backlog",
            "Todo": "Planned",
            "In Progress": "In Progress",
            "In Dev": "In Progress",
            "Review": "In Progress",
            "In Prod": "Completed",
            "Done": "Completed",
        }
        expected_status = STATUS_MAP.get(expected_linear_state, "In Progress")

        if actual_status and actual_status != expected_status:
            violations.append(Violation(
                "BLOCKING", "LINEAR_STATUS_MISMATCH",
                f"Linear project status is '{actual_status}' but should be '{expected_status}' "
                f"(framework state: {project.get('state')}, expected linear_state: {expected_linear_state})",
                action=f"Sync Linear status to '{expected_status}'",
                auto_fixable=True
            ))
    except Exception as e:
        violations.append(Violation(
            "MINOR", "LINEAR_CHECK_FAILED",
            f"Could not query Linear API: {e}"
        ))

    return violations


def check_artifact_issues(project, state):
    """Verify artifact issues are created/closed as expected."""
    violations = []
    APPROVAL_GATE_STATES = {"BRIEF", "DESIGN", "ARCHITECTURE", "PLAN", "REVIEW"}

    if state not in APPROVAL_GATE_STATES:
        return violations

    # Check if current state has an artifact issue
    history = project.get("state_history", [])
    current_entry = None
    for entry in reversed(history):
        if entry.get("state") == state:
            current_entry = entry
            break

    if current_entry and not current_entry.get("artifact_issue_id"):
        violations.append(Violation(
            "BLOCKING", "ARTIFACT_ISSUE_MISSING",
            f"No artifact issue created in Linear for {state} stage. "
            f"The transition command should have created it automatically.",
            action=f"Create artifact issue manually: [{project.get('_name', 'project')}] {state} artifact",
            auto_fixable=True
        ))

    # Check previous approval gate states have closed artifact issues
    prev_states_seen = set()
    for entry in history:
        s = entry.get("state")
        if s in APPROVAL_GATE_STATES and s != state:
            prev_states_seen.add(s)
            if entry.get("artifact_issue_id") and not entry.get("project_update_id"):
                # Has artifact issue but no project update = wasn't closed properly
                violations.append(Violation(
                    "SIGNIFICANT", "ARTIFACT_NOT_CLOSED",
                    f"Artifact issue for {s} stage (id: {entry['artifact_issue_id']}) "
                    f"was not closed with a project update on transition.",
                    action=f"Close artifact issue and post project update for {s}",
                    auto_fixable=True
                ))

    return violations


def check_inter_agent_review(project, state, state_info):
    """Verify inter-agent review is progressing correctly."""
    violations = []

    if not state_info.get("inter_agent_review"):
        return violations

    name = project.get("_name", "")
    stage = state.lower()
    review_pattern = f"{name}-review-{stage}-"

    review_files = []
    if PROJECTS_DIR.exists():
        for f in PROJECTS_DIR.iterdir():
            if f.name.startswith(review_pattern) and f.suffix == ".md":
                review_files.append(f)

    if not review_files:
        # Check how long we've been in this state
        entered_at = _get_state_entered_at(project, state)
        if entered_at:
            elapsed = now_utc() - entered_at
            if elapsed > timedelta(minutes=10):
                violations.append(Violation(
                    "SIGNIFICANT", "REVIEW_NOT_STARTED",
                    f"In {state} for {int(elapsed.total_seconds() / 60)}min but no inter-agent "
                    f"review file exists. Producer and critic should have been spawned.",
                    action=f"Spawn producer ({state_info.get('producer_role')}) and critic ({state_info.get('critic_role')})"
                ))
        return violations

    # Check latest review file for stalled reviews
    latest = sorted(review_files)[-1]
    content = latest.read_text()

    if "NEEDS_REVISION" in content or "NEEDS_FIXES" in content:
        # Check if producer has responded
        if "## Producer's Responses" in content:
            responses_section = content.split("## Producer's Responses")[1]
            if "[To be filled" in responses_section or responses_section.strip() == "":
                violations.append(Violation(
                    "SIGNIFICANT", "REVIEW_STALLED_PRODUCER",
                    f"Critic returned NEEDS_FIXES but producer hasn't responded yet.",
                    action="Address critic's issues and update the review file"
                ))

    return violations


def check_build_progress(project, state):
    """Verify BUILD state is progressing - Linear issues being worked."""
    violations = []

    if state != "BUILD":
        return violations

    linear_project_id = project.get("linear_project_id")
    if not linear_project_id:
        return violations

    token = _get_linear_token()
    if not token:
        return violations

    try:
        import urllib.request
        # Get all issues in the project
        query = """query($projectId: String!) {
            project(id: $projectId) {
                issues(first: 50) {
                    nodes { id identifier title state { name } updatedAt }
                }
            }
        }"""
        req = urllib.request.Request(
            'https://api.linear.app/graphql',
            data=json.dumps({"query": query, "variables": {"projectId": linear_project_id}}).encode(),
            headers={'Authorization': token, 'Content-Type': 'application/json'}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        issues = resp.get("data", {}).get("project", {}).get("issues", {}).get("nodes", [])

        if not issues:
            violations.append(Violation(
                "BLOCKING", "BUILD_NO_ISSUES",
                "In BUILD state but no Linear issues found in the project.",
                action="Create Linear issues from the plan"
            ))
            return violations

        # Check for issues stuck in wrong states
        stale_threshold = now_utc() - timedelta(hours=4)
        total = len(issues)
        done_count = sum(1 for i in issues if i.get("state", {}).get("name") in ("Done", "Canceled"))
        backlog_count = sum(1 for i in issues if i.get("state", {}).get("name") == "Backlog")

        if backlog_count == total:
            violations.append(Violation(
                "SIGNIFICANT", "BUILD_ALL_BACKLOG",
                f"All {total} issues still in Backlog. None have been started.",
                action="Move first issue to 'In Progress' and begin work"
            ))

        # Check for issues stuck "In Progress" / "In Dev" too long
        for issue in issues:
            issue_state = issue.get("state", {}).get("name", "")
            updated = parse_iso(issue.get("updatedAt"))
            if issue_state in ("In Progress", "In Dev", "Todo") and updated and updated < stale_threshold:
                hours_stale = int((now_utc() - updated).total_seconds() / 3600)
                violations.append(Violation(
                    "MINOR", "BUILD_ISSUE_STALE",
                    f"Issue {issue.get('identifier')} '{issue.get('title')}' in '{issue_state}' "
                    f"not updated for {hours_stale}h.",
                    action=f"Check progress on {issue.get('identifier')}"
                ))

    except Exception as e:
        violations.append(Violation(
            "MINOR", "BUILD_CHECK_FAILED",
            f"Could not check BUILD progress via Linear API: {e}"
        ))

    return violations


def check_ship_requirements(project, state):
    """Verify SHIP state requirements before CLOSED transition."""
    violations = []

    if state != "SHIP":
        return violations

    summary_path = project.get("summary")
    if not summary_path:
        return violations

    full_path = WORKSPACE_DIR / summary_path
    if not full_path.exists():
        return violations

    content = full_path.read_text()

    required_sections = {
        "## Verified API Schemas": "API schema verification",
        "## Human E2E Test Report": "Human E2E testing",
    }

    for section, desc in required_sections.items():
        if section not in content:
            violations.append(Violation(
                "BLOCKING", f"SHIP_MISSING_{desc.upper().replace(' ', '_')}",
                f"Missing '{section}' in project summary. {desc} is mandatory before CLOSED.",
                action=f"Complete {desc} and add section to project summary"
            ))

    return violations


def check_summary_completeness(project, state, sm):
    """Check that project summary file has required sections for current state."""
    violations = []
    summary_path = project.get("summary")

    if not summary_path:
        violations.append(Violation(
            "BLOCKING", "SUMMARY_MISSING",
            "No summary file path in PROJECTS.yaml.",
            action="Set summary path in PROJECTS.yaml"
        ))
        return violations

    full_path = WORKSPACE_DIR / summary_path
    if not full_path.exists():
        violations.append(Violation(
            "BLOCKING", "SUMMARY_FILE_MISSING",
            f"Summary file does not exist: {summary_path}",
            action=f"Create {summary_path}"
        ))
        return violations

    content = full_path.read_text()

    # Check sections expected for states we've passed through
    SECTION_CHECKS = {
        "BRIEF": "## Brief",
        "ARCHITECTURE": "## Architecture",
        "PLAN": "## Plan",
    }

    history_states = {e.get("state") for e in project.get("state_history", [])}
    for past_state, section in SECTION_CHECKS.items():
        if past_state in history_states and section not in content:
            violations.append(Violation(
                "SIGNIFICANT", f"MISSING_SECTION_{past_state}",
                f"Project passed through {past_state} but '{section}' missing from summary.",
                action=f"Add {section} section to {summary_path}"
            ))

    return violations


def check_stale_state(project, state):
    """Check if project has been stuck in a state too long."""
    violations = []
    entered_at = _get_state_entered_at(project, state)

    if not entered_at:
        return violations

    elapsed = now_utc() - entered_at

    # Different thresholds per state type
    STALE_THRESHOLDS = {
        "INTAKE": timedelta(hours=1),
        "BRIEF": timedelta(hours=24),
        "DESIGN": timedelta(hours=24),
        "ARCHITECTURE": timedelta(hours=24),
        "PLAN": timedelta(hours=24),
        "BUILD": timedelta(days=7),
        "REVIEW": timedelta(hours=48),
        "SHIP": timedelta(days=3),
    }

    threshold = STALE_THRESHOLDS.get(state, timedelta(days=7))
    if elapsed > threshold:
        hours = int(elapsed.total_seconds() / 3600)
        violations.append(Violation(
            "SIGNIFICANT", "STATE_STALE",
            f"Project has been in {state} for {hours}h (threshold: {int(threshold.total_seconds() / 3600)}h).",
            action=f"Review progress and either advance or document why it's blocked"
        ))

    return violations


# --- Helpers ---

def _get_state_entered_at(project, state):
    """Get the timestamp when the project entered the given state."""
    for entry in reversed(project.get("state_history", [])):
        if entry.get("state") == state:
            return parse_iso(entry.get("entered_at"))
    return None


def _get_linear_token():
    """Get Linear API token from env or /tmp/.linear_token."""
    token = os.environ.get("LINEAR_TOKEN") or os.environ.get("LINEAR_API_TOKEN")
    if token:
        return token
    try:
        with open("/tmp/.linear_token") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError):
        pass
    return None


def _read_file_safe(path):
    """Read a file and return contents or empty string."""
    try:
        with open(path) as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError):
        return ""


# --- Main command ---

def cmd_check(args):
    """Run all compliance checks for a project."""
    sm = load_state_machine()
    projects = load_projects()

    name = args.name
    prjs = projects.get("projects", {})
    if name not in prjs:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    project = prjs[name]
    project["_name"] = name  # convenience
    state = project.get("state", "UNKNOWN")
    state_info = sm["states"].get(state)

    if not state_info:
        print(json.dumps({"ok": False, "error": f"Unknown state: {state}"}))
        return 1

    # Run all checks
    all_violations = []

    all_violations.extend(check_linear_sync(project, state_info, sm))
    all_violations.extend(check_artifact_issues(project, state))
    all_violations.extend(check_inter_agent_review(project, state, state_info))
    all_violations.extend(check_build_progress(project, state))
    all_violations.extend(check_ship_requirements(project, state))
    all_violations.extend(check_summary_completeness(project, state, sm))
    all_violations.extend(check_stale_state(project, state))

    # Categorize
    blocking = [v for v in all_violations if v.severity == "BLOCKING"]
    significant = [v for v in all_violations if v.severity == "SIGNIFICANT"]
    minor = [v for v in all_violations if v.severity == "MINOR"]
    auto_fixable = [v for v in all_violations if v.auto_fixable]

    compliant = len(blocking) == 0 and len(significant) == 0

    result = {
        "ok": True,
        "project": name,
        "state": state,
        "compliant": compliant,
        "summary": {
            "blocking": len(blocking),
            "significant": len(significant),
            "minor": len(minor),
            "auto_fixable": len(auto_fixable),
            "total": len(all_violations)
        },
        "violations": [v.to_dict() for v in all_violations],
    }

    if args.verbose:
        result["state_info"] = {
            "actor": state_info.get("actor"),
            "approval_gate": state_info.get("approval_gate", False),
            "exit_criteria": state_info.get("exit_criteria", []),
            "inter_agent_review": state_info.get("inter_agent_review", False),
        }
        entered = _get_state_entered_at(project, state)
        if entered:
            elapsed = now_utc() - entered
            result["state_info"]["time_in_state_minutes"] = int(elapsed.total_seconds() / 60)

    print(json.dumps(result, indent=2))
    return 0 if compliant else 2


def main():
    parser = argparse.ArgumentParser(description="Project Manager Compliance Checker")
    subparsers = parser.add_subparsers(dest="command")

    p_check = subparsers.add_parser("check", help="Run compliance checks")
    p_check.add_argument("name", help="Project name")
    p_check.add_argument("--verbose", "-v", action="store_true", help="Include extra state info")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "check":
        return cmd_check(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
