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

import orchestrator as orchestrator_lib

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
            action="Check 1Password or env LINEAR_TOKEN"
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
    readiness = orchestrator_lib.evaluate_transition_readiness(load_state_machine(), name, project, state)
    child_precondition = next((p for p in readiness.get("preconditions", []) if p.get("type") == "child_receipts"), None)
    pm_session_precondition = next((p for p in readiness.get("preconditions", []) if p.get("type") == "pm_session_receipt"), None)

    if child_precondition:
        missing_roles = child_precondition.get("missing_roles", [])
        stale_roles = child_precondition.get("stale_roles", {})
        if missing_roles:
            entered_at = _get_state_entered_at(project, state)
            if entered_at:
                elapsed = now_utc() - entered_at
                if elapsed > timedelta(minutes=10):
                    violations.append(Violation(
                        "SIGNIFICANT", "REVIEW_RECEIPTS_MISSING",
                        f"In {state} for {int(elapsed.total_seconds() / 60)}min but structured review receipts are missing for: {', '.join(missing_roles)}.",
                        action=f"Record child receipts for roles: {', '.join(missing_roles)}"
                    ))
        if stale_roles:
            violations.append(Violation(
                "BLOCKING", "REVIEW_RECEIPTS_STALE",
                f"Structured review receipts are stale for roles: {', '.join(sorted(stale_roles.keys()))}. Artifact changed after sign-off.",
                action="Re-run review and record fresh child receipts"
            ))

    if pm_session_precondition and not pm_session_precondition.get("satisfied"):
        violations.append(Violation(
            "SIGNIFICANT", "PM_SESSION_RECEIPT_MISSING",
            "No current PM coordination receipt is bound to this stage artifact.",
            action="Record a pm_session receipt for the active PM session"
        ))

    stage = state.lower()
    review_pattern = f"{name}-review-{stage}-"
    review_files = []
    if PROJECTS_DIR.exists():
        for f in PROJECTS_DIR.iterdir():
            if f.name.startswith(review_pattern) and f.suffix == ".md":
                review_files.append(f)

    if not review_files:
        child_receipts_satisfied = bool(child_precondition and child_precondition.get("satisfied"))
        pm_session_satisfied = bool(pm_session_precondition and pm_session_precondition.get("satisfied"))

        if child_receipts_satisfied and pm_session_satisfied:
            return violations

        entered_at = _get_state_entered_at(project, state)
        if entered_at:
            elapsed = now_utc() - entered_at
            if elapsed > timedelta(minutes=10):
                violations.append(Violation(
                    "SIGNIFICANT", "REVIEW_NOT_STARTED",
                    f"In {state} for {int(elapsed.total_seconds() / 60)}min but no inter-agent review file exists. Producer and critic should have been spawned.",
                    action=f"Spawn producer ({state_info.get('producer_role')}) and critic ({state_info.get('critic_role')})"
                ))
        return violations

    latest = sorted(review_files)[-1]
    content = latest.read_text()

    if "NEEDS_REVISION" in content or "NEEDS_FIXES" in content:
        if "## Producer's Responses" in content:
            responses_section = content.split("## Producer's Responses")[1]
            if "[To be filled" in responses_section or responses_section.strip() == "":
                violations.append(Violation(
                    "SIGNIFICANT", "REVIEW_STALLED_PRODUCER",
                    "Critic returned NEEDS_FIXES but producer hasn't responded yet.",
                    action="Address critic's issues and update the review file"
                ))

    return violations


def check_review_loop_state(project, state, state_info):
    violations = []
    if not state_info.get("inter_agent_review"):
        return violations

    project_name = project.get("_name", "")
    review_loop = orchestrator_lib.summarize_review_loop_state(project_name, state)
    if not review_loop.get("present"):
        return violations

    for issue in review_loop.get("issues", []):
        code = issue.get("code")
        if code == "round_cap_exceeded":
            violations.append(Violation(
                "BLOCKING", "REVIEW_LOOP_EXCEEDED",
                issue.get("message", "Review loop exceeded round cap without override."),
                action="Record FREEZE_AND_ESCALATE/APPROVE/CANCEL or an explicit override in review-loop state"
            ))
        elif code == "freeze_decision_required":
            violations.append(Violation(
                "SIGNIFICANT", "REVIEW_LOOP_DECISION_MISSING",
                issue.get("message", "Review loop cap reached without a recorded decision."),
                action="Record the mandatory post-cap decision in review-loop state"
            ))
        elif code == "checkpoint_missing":
            violations.append(Violation(
                "SIGNIFICANT", "REVIEW_LOOP_CHECKPOINT_MISSING",
                issue.get("message", "Operator checkpoint missing from review-loop state."),
                action="Record the latest operator checkpoint summary/file in review-loop state"
            ))
        elif code == "freeze_artifact_missing":
            violations.append(Violation(
                "BLOCKING", "REVIEW_LOOP_FREEZE_ARTIFACT_MISSING",
                issue.get("message", "Frozen-cap path is missing its structured freeze artifact."),
                action="Record the freeze artifact with rationale, unresolved issues, risks, and carry-forward items"
            ))
        elif code in {"freeze_rationale_missing", "freeze_unresolved_issues_invalid", "freeze_accepted_risks_invalid", "freeze_carry_forward_invalid"}:
            violations.append(Violation(
                "SIGNIFICANT", f"REVIEW_LOOP_{code.upper()}",
                issue.get("message", "Freeze artifact is incomplete or invalid."),
                action="Repair the freeze artifact payload for the frozen-cap path"
            ))
        elif code == "stage_boundary_drift":
            examples = issue.get("flagged_examples", [])
            example_suffix = f" Examples: {' | '.join(examples)}" if examples else ""
            violations.append(Violation(
                "SIGNIFICANT", "REVIEW_STAGE_BOUNDARY_DRIFT",
                issue.get("message", "Review loop drifted into implementation-detail critique outside the current stage boundary.") + example_suffix,
                action="Refocus the current stage review on scope, sequencing, success criteria, and risks, then carry code-level follow-ups into BUILD"
            ))
    return violations


def check_child_task_health(project, state):
    """Verify durable child-task heartbeat state is healthy when present."""
    violations = []
    name = project.get("_name", "")
    child_tasks = orchestrator_lib.summarize_child_tasks(name, state)
    if child_tasks.get("migration", {}).get("needed"):
        inferred_count = len(child_tasks.get("tasks", []))
        violations.append(Violation(
            "MINOR", "CHILD_TASK_BACKFILL_RECOMMENDED",
            f"Child-task state is still inferred from legacy receipts for {inferred_count} task(s). Persist the ledger before relying on watchdog automation.",
            action=f"Run `python3 scripts/orchestrator.py backfill-child-tasks {name} --state {state}` to persist canonical child-task state"
        ))
    if not child_tasks.get("present"):
        return violations

    for issue in child_tasks.get("issues", []):
        code = issue.get("code")
        task_id = issue.get("task_id")
        if code == "child_task_stale":
            violations.append(Violation(
                "SIGNIFICANT", "CHILD_TASK_STALE",
                issue.get("message", f"Child task '{task_id}' heartbeat is stale."),
                action=f"Refresh the heartbeat or resolve stalled child task '{task_id}'"
            ))
        elif code == "child_task_heartbeat_missing":
            violations.append(Violation(
                "SIGNIFICANT", "CHILD_TASK_HEARTBEAT_MISSING",
                issue.get("message", f"Child task '{task_id}' is missing heartbeat state."),
                action=f"Record heartbeat state for child task '{task_id}'"
            ))
        elif code == "child_task_attention_required":
            violations.append(Violation(
                "BLOCKING", "CHILD_TASK_ATTENTION_REQUIRED",
                issue.get("message", f"Child task '{task_id}' requires attention."),
                action=f"Resolve blocker or explicitly complete/cancel child task '{task_id}'"
            ))
    return violations


def _is_operator_approval_only_remaining(project, state, sm):
    state_info = sm["states"].get(state, {})
    if not state_info.get("approval_gate"):
        return False

    name = project.get("_name", "")
    readiness = orchestrator_lib.evaluate_transition_readiness(sm, name, project, state)
    operator_pending = False
    non_operator_unsatisfied = []

    for precondition in readiness.get("preconditions", []):
        satisfied = bool(precondition.get("satisfied"))
        if precondition.get("type") == "approval_receipt":
            operator_pending = not satisfied
            continue
        if not satisfied:
            non_operator_unsatisfied.append(precondition.get("type"))

    return operator_pending and not non_operator_unsatisfied


def _is_stage_owner_task(task, project_name, state):
    owner = str(task.get("owner") or "").strip().lower()
    task_id = str(task.get("task_id") or "").strip().lower()
    label = str(task.get("label") or "").strip().lower()
    session_label = str(task.get("session_label") or "").strip().lower()
    state_token = str(state or "").strip().lower()
    project_token = str(project_name or "").strip().lower()

    if owner not in {"pm", "pa"}:
        return False
    if "stage-owner" in task_id or "stage owner" in label:
        return True
    if session_label and session_label.startswith(f"pm-{project_token}-{state_token}"):
        return True
    return False


def _latest_task_timestamp(task):
    for field in ("updated_at", "heartbeat_at", "started_at"):
        parsed = parse_iso(task.get(field))
        if parsed:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def check_pm_continuity(project, state, sm):
    """Require durable PM ownership until the real state stopping condition is reached."""
    violations = []
    if state in {"CLOSED", "CANCELED"}:
        return violations

    entered_at = _get_state_entered_at(project, state)
    if entered_at and (now_utc() - entered_at) <= timedelta(minutes=10):
        return violations

    if _is_operator_approval_only_remaining(project, state, sm):
        return violations

    name = project.get("_name", "")
    child_tasks = orchestrator_lib.summarize_child_tasks(name, state)
    stage_owner_tasks = [
        task for task in child_tasks.get("tasks", [])
        if _is_stage_owner_task(task, name, state)
    ]

    for task in stage_owner_tasks:
        status = str(task.get("status") or "").strip().lower()
        if status in orchestrator_lib.CHILD_TASK_ACTIVE_STATUSES:
            return violations
        if task.get("attention_required") or task.get("blocked_reason") or status in {"blocked", "needs_attention"}:
            return violations

    if stage_owner_tasks:
        latest = max(stage_owner_tasks, key=_latest_task_timestamp)
        violations.append(Violation(
            "BLOCKING", "PM_OWNER_ENDED_BEFORE_STAGE_EXIT",
            f"Stage-owner PM task '{latest.get('task_id')}' is '{latest.get('status')}' but the project is still in active state {state} without reaching a real stopping condition.",
            action="Immediately spawn the successor PM owner or transition the stage if it is genuinely complete"
        ))
        return violations

    violations.append(Violation(
        "BLOCKING", "PM_OWNER_MISSING",
        f"No active stage-owner PM task is recorded for active state {state}. Durable PM ownership is required until the stage exits, only operator approval remains, or a real blocker is escalated.",
        action="Record or spawn a stage-owner PM task and add relay/watchdog rehydration if only one-shot PM runs are available"
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
    """Get Linear API token from env, file, or 1Password."""
    token = os.environ.get("LINEAR_TOKEN") or os.environ.get("LINEAR_API_TOKEN")
    if token:
        return token
    try:
        with open("/tmp/.linear_token") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError):
        pass
    # Try 1Password SDK injector
    try:
        inject_script = Path.home() / "op-config-injector" / "inject.mjs"
        if inject_script.exists():
            result = subprocess.run(
                ["node", str(inject_script), "op://OpenClaw-Justin-PA/linear/credential"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": _read_file_safe("/etc/openclaw/op-token")}
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
    except Exception:
        pass
    # Fallback to op CLI
    try:
        env = os.environ.copy()
        op_token = _read_file_safe("/etc/openclaw/op-token")
        if op_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = op_token
        result = subprocess.run(
            ["op", "read", "op://OpenClaw-Justin-PA/linear/credential"],
            capture_output=True, text=True, timeout=10, env=env
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
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
    all_violations.extend(check_review_loop_state(project, state, state_info))
    all_violations.extend(check_child_task_health(project, state))
    all_violations.extend(check_pm_continuity(project, state, sm))
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
        readiness = orchestrator_lib.evaluate_transition_readiness(sm, name, project, state)
        reporting_contract = orchestrator_lib.build_shared_reporting_contract(
            sm,
            name,
            project,
            state,
            readiness=readiness,
        )
        result["state_info"] = {
            "actor": state_info.get("actor"),
            "approval_gate": state_info.get("approval_gate", False),
            "exit_criteria": state_info.get("exit_criteria", []),
            "inter_agent_review_required": state_info.get("inter_agent_review", False),
        }
        entered = _get_state_entered_at(project, state)
        if entered:
            elapsed = now_utc() - entered
            result["state_info"]["time_in_state_minutes"] = int(elapsed.total_seconds() / 60)

        result["state_info"]["review_loop"] = reporting_contract["review_loop"]
        result["state_info"]["child_task_watchdog"] = reporting_contract["child_task_watchdog"]
        result["state_info"]["inter_agent_review"] = reporting_contract["inter_agent_review"]
        pm_child_tasks = orchestrator_lib.summarize_child_tasks(name, state)
        result["state_info"]["pm_continuity"] = {
            "operator_approval_only_remaining": _is_operator_approval_only_remaining(project, state, sm),
            "child_tasks_present": pm_child_tasks.get("present", False),
            "stage_owner_task_count": sum(1 for task in pm_child_tasks.get("tasks", []) if _is_stage_owner_task(task, name, state)),
        }

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
