#!/usr/bin/env python3
"""
Project Orchestrator - Deterministic state machine engine.

Commands:
  init <name> --tier <patch|feature|project>  Initialize a new project
  status <name> [--verbose]                   Show current state + next steps
  transition <name> <target-state>            Validate and execute transition
  validate <name>                             Check artifacts and transition preconditions
  plan <name>                                 Show state history + next steps
  review-status <name>                        Show structured review receipt status
  record-review-loop <name> ...               Persist canonical review-loop state
  record-review-loop-decision <name> ...      Persist a post-cap decision on the canonical review-loop state
  backfill-review-loop <name> ...             Persist review-loop state from existing review files
  record-review-checkpoint <name> ...         Write an operator checkpoint and sync loop state
  record-freeze-artifact <name> ...           Persist frozen-cap carry-forward metadata
  record-child-task <name> ...                Persist durable child-task heartbeat/state
  backfill-child-tasks <name> ...             Persist canonical child-task JSON from receipts
  child-task-status <name>                    Show durable child-task status
  child-task-watchdog <name>                  Report alert-worthy child-task exceptions
  record-receipt <name> ...                   Persist structured approval/coordination receipts

Exit codes: 0 = success, 1 = invalid transition, 2 = validation/precondition failure
"""

import argparse
import copy
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
STATE_MACHINE_PATH = SKILL_DIR / "references" / "state-machine.yaml"


def _find_workspace_dir():
    """Workspace detection: env var > walk up looking for PROJECTS.yaml > common fallback."""
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

APPROVAL_GATE_STATES = {"BRIEF", "DESIGN", "ARCHITECTURE", "PLAN", "REVIEW"}
SECTION_MAP = {
    "BRIEF": "## Brief",
    "DESIGN": "## Design",
    "ARCHITECTURE": "## Architecture",
    "PLAN": "## Plan",
    "REVIEW": "## Review",
}
SECTION_TITLE_MAP = {
    "BRIEF": "Brief",
    "DESIGN": "Design",
    "ARCHITECTURE": "Architecture",
    "PLAN": "Plan",
    "REVIEW": "Review",
    "SHIP": "Ship",
    "CLOSED": "Retrospective",
}
PA_OPS_BOT_ID = "dd106e48-fa9d-42d3-af73-9298a3089219"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_ACCEPTED_STATUSES = {"approved", "verified", "completed", "passed", "done", "merged", "active"}
RECEIPT_REJECTED_STATUSES = {"rejected", "failed", "needs_fixes", "needs_revision", "stale"}
REVIEW_LOOP_DECISIONS = {"FREEZE_AND_ESCALATE", "APPROVE", "CANCEL"}
CHILD_TASK_ACTIVE_STATUSES = {"queued", "running", "active", "waiting", "blocked", "needs_attention"}
CHILD_TASK_TERMINAL_STATUSES = {"completed", "canceled", "failed"}
STAGE_BOUNDARY_IMPLEMENTATION_KEYWORDS = {
    "rename", "variable", "function", "functions", "method", "methods", "class", "classes",
    "module", "modules", "script", "scripts", "file", "files", "path", "paths", "flag", "flags",
    "refactor", "refactoring", "helper", "helpers", "parameter", "parameters", "json field",
    "sql query", "regex", "command", "cli", "test fixture", "line ", "code-level", "code style",
}
STAGE_BOUNDARY_ALLOWED_KEYWORDS = {
    "scope", "success criteria", "task", "tasks", "sequencing", "sequence", "estimate", "estimates",
    "milestone", "milestones", "dependency", "dependencies", "risk", "risks", "assumption", "assumptions",
    "acceptance criteria", "rollout", "cutover", "owner", "owners", "staffing", "timeline", "checkpoint",
    "artifact", "artifacts", "review loop", "operator", "plan", "brief",
}
AUDIT_ENV_MAP = {
    "actor_id": ["OPENCLAW_ACTOR_ID", "OPENCLAW_USER_ID", "OPENCLAW_MEMBER_ID", "USER"],
    "actor_role": ["OPENCLAW_ACTOR_ROLE", "OPENCLAW_ROLE"],
    "session_id": ["OPENCLAW_SESSION_ID", "SESSION_ID"],
    "request_id": ["OPENCLAW_REQUEST_ID", "REQUEST_ID"],
    "channel": ["OPENCLAW_CHANNEL", "CHANNEL"],
    "source": ["OPENCLAW_SOURCE"],
}


def load_yaml(path):
    with open(path, "r") as f:
        if HAS_YAML:
            return yaml.safe_load(f)
        return json.load(f)


def save_yaml(path, data):
    with open(path, "w") as f:
        if HAS_YAML:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        else:
            json.dump(data, f, indent=2)


def load_state_machine():
    return load_yaml(STATE_MACHINE_PATH)


def load_projects():
    return load_yaml(PROJECTS_YAML_PATH)


def save_projects(data):
    save_yaml(PROJECTS_YAML_PATH, data)


def get_project(projects, name):
    prjs = projects.get("projects", {})
    if name not in prjs:
        return None
    return prjs[name]


def get_valid_transitions(sm, tier, current_state):
    tier_states = sm["tiers"].get(tier, {}).get("states", [])
    transitions = []
    for t in sm["transitions"]:
        if t["from"] == current_state and t["to"] in tier_states:
            transitions.append({
                "to": t["to"],
                "condition": t["condition"],
                "preconditions": t.get("preconditions", []),
            })
    return transitions


def get_transition_definition(sm, tier, current_state, target_state):
    for transition in get_valid_transitions(sm, tier, current_state):
        if transition["to"] == target_state:
            return transition
    return None


def get_state_info(sm, state_name):
    return sm["states"].get(state_name)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8"))


def slugify(text):
    safe = []
    for ch in str(text or "receipt"):
        safe.append(ch.lower() if ch.isalnum() else "-")
    collapsed = "".join(safe)
    while "--" in collapsed:
        collapsed = collapsed.replace("--", "-")
    return collapsed.strip("-") or "receipt"


def try_relative_to_workspace(path):
    try:
        return str(Path(path).resolve().relative_to(WORKSPACE_DIR.resolve()))
    except Exception:
        return str(path)


def project_summary_path(project):
    summary_path = project.get("summary")
    if not summary_path:
        return None
    return WORKSPACE_DIR / summary_path


def extract_section(summary_path, section_header):
    """Extract a markdown section by its `## Header`. Returns the full section or None."""
    full_path = WORKSPACE_DIR / summary_path
    if not full_path.exists():
        return None
    text = full_path.read_text()
    start = text.find(section_header)
    if start == -1:
        return None
    rest = text[start + len(section_header):]
    next_header = rest.find("\n## ")
    if next_header == -1:
        return text[start:]
    return text[start:start + len(section_header) + next_header]


def check_summary_section(project, section_name):
    summary_path = project.get("summary")
    if not summary_path:
        return False
    return extract_section(summary_path, f"## {section_name}") is not None


def find_design_spec_path(project_name):
    search_paths = [
        Path(f"/tmp/{project_name}-design/design-spec.json"),
        Path(f"/tmp/{project_name.replace('-', '_')}-design/design-spec.json"),
        Path(f"/tmp/design-{project_name}/design-spec.json"),
    ]
    projects_dir = WORKSPACE_DIR / "projects"
    if projects_dir.exists():
        for candidate in glob.glob(str(projects_dir / f"{project_name}*design*.json")):
            search_paths.append(Path(candidate))
    for candidate in glob.glob("/tmp/*design*/design-spec.json"):
        search_paths.append(Path(candidate))

    for path in search_paths:
        if path.exists():
            return path
    return None


def build_state_artifact_subject(project_name, project, state_name):
    """Return the canonical artifact subject for approvals/review receipts in the current state."""
    if state_name == "DESIGN":
        spec_path = find_design_spec_path(project_name)
        if not spec_path:
            return None
        bundle = {
            "spec": {
                "path": str(spec_path),
                "sha256": sha256_bytes(spec_path.read_bytes()),
            },
            "wireframes": [],
        }
        wireframe_dir = spec_path.parent / "wireframes"
        if wireframe_dir.exists():
            for svg in sorted(wireframe_dir.glob("*.svg")):
                bundle["wireframes"].append({
                    "path": str(svg),
                    "sha256": sha256_bytes(svg.read_bytes()),
                })
        subject_hash = sha256_text(canonical_json(bundle))
        return {
            "kind": "design_bundle",
            "state": state_name,
            "path": str(spec_path),
            "hash": subject_hash,
            "inputs": bundle,
        }

    if state_name in SECTION_MAP:
        summary_path = project.get("summary")
        if not summary_path:
            return None
        section = extract_section(summary_path, SECTION_MAP[state_name])
        if not section:
            return None
        full_summary_path = WORKSPACE_DIR / summary_path
        return {
            "kind": "summary_section",
            "state": state_name,
            "path": summary_path,
            "section": SECTION_TITLE_MAP.get(state_name, state_name.title()),
            "hash": sha256_text(section),
            "bytes": len(section.encode("utf-8")),
            "source_file": try_relative_to_workspace(full_summary_path),
        }

    return None


def get_project_runtime_dir(project_name):
    return PROJECTS_DIR / ".orchestrator" / project_name


def get_receipts_root(project_name):
    return get_project_runtime_dir(project_name) / "receipts"


def get_review_loop_state_path(project_name, state_name):
    return get_project_runtime_dir(project_name) / "review-loops" / f"{state_name.lower()}.json"


def get_freeze_artifact_path(project_name, state_name):
    return get_project_runtime_dir(project_name) / "freeze-artifacts" / f"{state_name.lower()}.json"


def get_freeze_artifact_markdown_path(project_name, state_name):
    return PROJECTS_DIR / f"{project_name}-freeze-artifact-{state_name.lower()}.md"


def get_child_tasks_dir(project_name, state_name):
    return get_project_runtime_dir(project_name) / "child-tasks" / state_name.lower()


def get_child_task_path(project_name, state_name, task_id):
    return get_child_tasks_dir(project_name, state_name) / f"{slugify(task_id)}.json"


def get_review_checkpoint_path(project_name, state_name, current_round):
    round_number = max(1, _coerce_int(current_round, 1))
    return PROJECTS_DIR / f"{project_name}-review-checkpoint-{state_name.lower()}-round{round_number}.md"


def find_review_round_files(project_name, state_name):
    state_slug = (state_name or "").strip().lower()
    patterns = [
        PROJECTS_DIR / f"{project_name}-review-{state_slug}-round*.md",
        PROJECTS_DIR / f"{project_name}-review-checkpoint-{state_slug}-round*.md",
    ]
    results = []
    seen = set()
    for pattern in patterns:
        for path_str in glob.glob(str(pattern)):
            path = Path(path_str)
            match = re.search(r"round(\d+)\.md$", path.name)
            if not match:
                continue
            round_number = int(match.group(1))
            key = (round_number, str(path))
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "round": round_number,
                "path": try_relative_to_workspace(path),
                "kind": "checkpoint" if "review-checkpoint" in path.name else "review",
            })
    return sorted(results, key=lambda item: (item["round"], item["path"]))


def load_json_file(path):
    with open(path, "r") as f:
        return json.load(f)


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_review_loop_state(project_name, state_name):
    path = get_review_loop_state_path(project_name, state_name)
    if not path.exists():
        return None
    try:
        data = load_json_file(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data["_path"] = try_relative_to_workspace(path)
    return data


def persist_review_loop_state(project_name, state_name, payload):
    path = get_review_loop_state_path(project_name, state_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    persisted = copy.deepcopy(payload)
    persisted["_path"] = try_relative_to_workspace(path)
    return persisted


def load_freeze_artifact(project_name, state_name):
    path = get_freeze_artifact_path(project_name, state_name)
    if not path.exists():
        return None
    try:
        data = load_json_file(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data["_path"] = try_relative_to_workspace(path)
    return data


def persist_freeze_artifact(project_name, state_name, payload):
    path = get_freeze_artifact_path(project_name, state_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    persisted = copy.deepcopy(payload)
    persisted["_path"] = try_relative_to_workspace(path)
    return persisted


def load_child_task(project_name, state_name, task_id):
    path = get_child_task_path(project_name, state_name, task_id)
    if not path.exists():
        return None
    try:
        data = load_json_file(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data["_path"] = try_relative_to_workspace(path)
    return data


def persist_child_task(project_name, state_name, task_id, payload):
    path = get_child_task_path(project_name, state_name, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    persisted = copy.deepcopy(payload)
    persisted["_path"] = try_relative_to_workspace(path)
    return persisted


def load_child_tasks(project_name, state_name):
    tasks_dir = get_child_tasks_dir(project_name, state_name)
    if not tasks_dir.exists():
        return []
    tasks = []
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = load_json_file(path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        data["_path"] = try_relative_to_workspace(path)
        tasks.append(data)
    return tasks


def child_task_ledger_exists(project_name, state_name):
    tasks_dir = get_child_tasks_dir(project_name, state_name)
    if not tasks_dir.exists():
        return False
    return any(path.is_file() for path in tasks_dir.glob("*.json"))


def _parse_datetime_or_none(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _normalize_child_task_item(task, now):
    status = str(task.get("status") or "active").strip().lower()
    heartbeat_at = _parse_datetime_or_none(task.get("heartbeat_at"))
    blocked_reason = task.get("blocked_reason")
    attention_required = bool(task.get("attention_required"))
    stale_seconds = None
    if heartbeat_at:
        stale_seconds = max(0, int((now - heartbeat_at).total_seconds()))

    item = {
        "task_id": task.get("task_id"),
        "label": task.get("label"),
        "owner": task.get("owner"),
        "kind": task.get("kind"),
        "status": status,
        "current_step": task.get("current_step"),
        "summary": task.get("summary"),
        "started_at": task.get("started_at"),
        "heartbeat_at": task.get("heartbeat_at"),
        "updated_at": task.get("updated_at"),
        "attention_required": attention_required,
        "blocked_reason": blocked_reason,
        "session_label": task.get("session_label"),
        "path": task.get("_path"),
        "metadata": task.get("metadata", {}),
    }
    if stale_seconds is not None:
        item["stale_seconds"] = stale_seconds
    return item, status, heartbeat_at, stale_seconds, blocked_reason, attention_required


def _child_task_status_from_receipt(receipt):
    status = str(receipt.get("status") or "").strip().lower()
    if status in {"canceled", "cancelled"}:
        return "canceled"
    if status in RECEIPT_REJECTED_STATUSES:
        return "failed"
    return "completed"


def infer_child_tasks_from_receipts(project_name, state_name):
    items = []
    receipts = []

    latest_child_by_role = {}
    for receipt in find_receipts(project_name, state=state_name, kind="child"):
        role = str(receipt.get("role") or "").strip().lower()
        if not role or role in latest_child_by_role:
            continue
        latest_child_by_role[role] = receipt

    for role in sorted(latest_child_by_role):
        receipt = latest_child_by_role[role]
        receipt_summary = summarize_receipt(receipt)
        metadata = dict(receipt_summary.get("metadata") or {})
        metadata.update({
            "inferred_from_receipt": True,
            "receipt_kind": "child",
            "receipt_role": role,
            "receipt_status": receipt_summary.get("status"),
            "receipt_id": receipt_summary.get("id"),
            "receipt_path": receipt_summary.get("path"),
        })
        items.append({
            "project": project_name,
            "state": state_name,
            "task_id": f"receipt-{role}",
            "label": f"{role} review receipt",
            "kind": "receipt_backfill",
            "owner": role,
            "status": _child_task_status_from_receipt(receipt),
            "current_step": "Receipt recorded",
            "summary": metadata.get("note") or f"{role} receipt recorded with status '{receipt_summary.get('status')}'.",
            "started_at": receipt_summary.get("recorded_at"),
            "heartbeat_at": receipt_summary.get("recorded_at"),
            "blocked_reason": None,
            "attention_required": False,
            "session_label": metadata.get("session_label"),
            "updated_at": receipt_summary.get("recorded_at"),
            "metadata": metadata,
            "_path": receipt_summary.get("path"),
        })
        receipts.append(receipt_summary)

    latest_pm_session = None
    for receipt in find_receipts(project_name, state=state_name, kind="pm_session"):
        latest_pm_session = receipt
        break
    if latest_pm_session:
        receipt_summary = summarize_receipt(latest_pm_session)
        metadata = dict(receipt_summary.get("metadata") or {})
        session_label = metadata.get("session_label") or "pm-session"
        metadata.update({
            "inferred_from_receipt": True,
            "receipt_kind": "pm_session",
            "receipt_role": receipt_summary.get("role"),
            "receipt_status": receipt_summary.get("status"),
            "receipt_id": receipt_summary.get("id"),
            "receipt_path": receipt_summary.get("path"),
        })
        items.append({
            "project": project_name,
            "state": state_name,
            "task_id": f"pm-session-{slugify(session_label)}",
            "label": f"PM session {session_label}",
            "kind": "receipt_backfill",
            "owner": receipt_summary.get("role") or "pm",
            "status": _child_task_status_from_receipt(latest_pm_session),
            "current_step": "PM session receipt recorded",
            "summary": metadata.get("note") or f"PM session receipt recorded with status '{receipt_summary.get('status')}'.",
            "started_at": receipt_summary.get("recorded_at"),
            "heartbeat_at": receipt_summary.get("recorded_at"),
            "blocked_reason": None,
            "attention_required": False,
            "session_label": session_label,
            "updated_at": receipt_summary.get("recorded_at"),
            "metadata": metadata,
            "_path": receipt_summary.get("path"),
        })
        receipts.append(receipt_summary)

    return items, receipts


def _summarize_child_task_items(project_name, state_name, tasks, stale_after_minutes, source, migration=None):
    stale_threshold_seconds = max(1, _coerce_int(stale_after_minutes, 15)) * 60
    now = datetime.now(timezone.utc)
    items = []
    issues = []
    migration_info = migration or {"needed": False, "source": None}

    for task in tasks:
        item, status, heartbeat_at, stale_seconds, blocked_reason, attention_required = _normalize_child_task_item(task, now)
        items.append(item)

        if status in CHILD_TASK_TERMINAL_STATUSES:
            continue
        if not heartbeat_at:
            issues.append({
                "code": "child_task_heartbeat_missing",
                "severity": "significant",
                "task_id": task.get("task_id"),
                "message": f"Child task '{task.get('task_id')}' has no heartbeat_at timestamp.",
            })
            continue
        if stale_seconds is not None and stale_seconds > stale_threshold_seconds:
            issues.append({
                "code": "child_task_stale",
                "severity": "significant",
                "task_id": task.get("task_id"),
                "message": f"Child task '{task.get('task_id')}' heartbeat is stale ({stale_seconds}s old).",
                "stale_seconds": stale_seconds,
            })
        if attention_required or status in {"blocked", "needs_attention"}:
            issues.append({
                "code": "child_task_attention_required",
                "severity": "blocking" if blocked_reason else "significant",
                "task_id": task.get("task_id"),
                "message": blocked_reason or f"Child task '{task.get('task_id')}' requires attention.",
            })

    active_count = sum(1 for item in items if item["status"] in CHILD_TASK_ACTIVE_STATUSES)
    blocked_count = sum(1 for item in items if item["status"] in {"blocked", "needs_attention"} or item.get("attention_required"))
    stale_count = sum(1 for issue in issues if issue.get("code") == "child_task_stale")
    heartbeat_missing_count = sum(1 for issue in issues if issue.get("code") == "child_task_heartbeat_missing")
    next_actions = []
    if migration_info.get("needed"):
        next_actions.append("backfill-child-tasks")

    return {
        "present": bool(items),
        "source": source,
        "path": try_relative_to_workspace(get_child_tasks_dir(project_name, state_name)),
        "stale_after_minutes": max(1, _coerce_int(stale_after_minutes, 15)),
        "tasks": items,
        "summary": {
            "total": len(items),
            "active": active_count,
            "blocked": blocked_count,
            "stale": stale_count,
            "heartbeat_missing": heartbeat_missing_count,
        },
        "migration": migration_info,
        "next_actions": next_actions,
        "issues": issues,
        "valid": len(issues) == 0,
    }


def summarize_child_tasks(project_name, state_name, stale_after_minutes=15):
    if child_task_ledger_exists(project_name, state_name):
        return _summarize_child_task_items(
            project_name,
            state_name,
            load_child_tasks(project_name, state_name),
            stale_after_minutes,
            source="persisted",
        )

    inferred_tasks, inferred_receipts = infer_child_tasks_from_receipts(project_name, state_name)
    migration = {
        "needed": bool(inferred_tasks),
        "source": "receipts" if inferred_tasks else None,
        "receipts": inferred_receipts,
    }
    source = "inferred_from_receipts" if inferred_tasks else "none"
    return _summarize_child_task_items(
        project_name,
        state_name,
        inferred_tasks,
        stale_after_minutes,
        source=source,
        migration=migration,
    )


def summarize_child_task_watchdog(child_tasks, exception_only=False):
    exceptions = []
    for issue in child_tasks.get("issues", []):
        severity = str(issue.get("severity") or "significant").lower()
        if exception_only and severity == "minor":
            continue
        exceptions.append(issue)
    return {
        "stale_after_minutes": child_tasks.get("stale_after_minutes"),
        "exceptions_only": bool(exception_only),
        "should_alert": len(exceptions) > 0,
        "summary": child_tasks.get("summary", {}),
        "exceptions": exceptions,
        "child_tasks_path": child_tasks.get("path"),
    }


def _review_issue_texts(items):
    texts = []
    if not isinstance(items, list):
        return texts
    for item in items:
        if isinstance(item, dict):
            parts = []
            for key in ["title", "summary", "issue", "message", "name", "detail", "details", "rationale"]:
                value = item.get(key)
                if value:
                    parts.append(str(value))
            if parts:
                texts.append(" ".join(parts))
                continue
            texts.append(canonical_json(item))
            continue
        if item:
            texts.append(str(item))
    return texts


def detect_stage_boundary_drift(state_name, unresolved_issues):
    state = (state_name or "").upper()
    if state not in {"BRIEF", "PLAN"}:
        return None

    flagged_examples = []
    for text in _review_issue_texts(unresolved_issues):
        lowered = text.lower()
        if not any(keyword in lowered for keyword in STAGE_BOUNDARY_IMPLEMENTATION_KEYWORDS):
            continue
        if any(keyword in lowered for keyword in STAGE_BOUNDARY_ALLOWED_KEYWORDS):
            continue
        flagged_examples.append(text.strip())

    if not flagged_examples:
        return None

    stage_label = "brief" if state == "BRIEF" else "plan"
    message = (
        f"{state} review loop is carrying implementation-detail critique that should not dominate {stage_label}-stage review. "
        f"Keep {stage_label} feedback focused on scope, sequencing, success criteria, and risks; move code-level follow-ups into BUILD carry-forward items."
    )
    return {
        "code": "stage_boundary_drift",
        "message": message,
        "flagged_examples": flagged_examples[:3],
        "count": len(flagged_examples),
    }


def summarize_review_loop_state(project_name, state_name):
    raw = load_review_loop_state(project_name, state_name) or {}
    freeze_artifact = load_freeze_artifact(project_name, state_name) or {}
    inferred_review_files = find_review_round_files(project_name, state_name)
    inferred_round = max((item.get("round", 0) for item in inferred_review_files), default=0)
    latest_review_file = inferred_review_files[-1]["path"] if inferred_review_files else None
    source = "persisted"
    migration = {
        "needed": False,
        "source": None,
        "review_files": inferred_review_files,
    }
    if not raw and inferred_review_files:
        source = "inferred_from_review_files"
        migration = {
            "needed": True,
            "source": "review_files",
            "review_files": inferred_review_files,
        }
        raw = {
            "project": project_name,
            "state": state_name,
            "current_round": inferred_round,
            "max_rounds": max(3, inferred_round),
            "checkpoint": {
                "summary": f"Backfilled from existing {state_name} review artifacts.",
                "file": latest_review_file,
                "recorded_at": None,
            },
            "unresolved_issues": [],
            "accepted_risks": [],
            "carry_forward_items": [],
            "updated_at": None,
            "audit": {
                "source": "backfill_preview",
            },
        }
    max_rounds = max(1, _coerce_int(raw.get("max_rounds"), 3))
    current_round = max(0, _coerce_int(raw.get("current_round"), 0))
    override = raw.get("override") if isinstance(raw.get("override"), dict) else {}
    override_active = bool(override.get("active"))
    decision = raw.get("decision")
    if isinstance(decision, str):
        decision = decision.strip().upper() or None
    else:
        decision = None
    freeze_required = bool(raw.get("freeze_required")) or (current_round >= max_rounds and not override_active)
    checkpoint = raw.get("checkpoint") if isinstance(raw.get("checkpoint"), dict) else {}
    checkpoint_present = bool(checkpoint and (checkpoint.get("file") or checkpoint.get("summary")))
    checkpoint_required = current_round > 0 and not checkpoint_present
    another_round_permitted = bool(override_active or current_round < max_rounds)
    decision_required = bool(freeze_required and decision not in REVIEW_LOOP_DECISIONS and not override_active)
    issues = []
    if current_round > max_rounds and not override_active:
        issues.append({
            "code": "round_cap_exceeded",
            "message": f"Current round {current_round} exceeds max rounds {max_rounds} without override.",
        })
    if freeze_required and decision not in REVIEW_LOOP_DECISIONS:
        issues.append({
            "code": "freeze_decision_required",
            "message": "Round cap reached. Record FREEZE_AND_ESCALATE, APPROVE, or CANCEL (or an explicit override).",
        })
    if freeze_required and decision in {"FREEZE_AND_ESCALATE", "APPROVE"}:
        if not freeze_artifact:
            issues.append({
                "code": "freeze_artifact_missing",
                "message": "Frozen-cap path requires a structured freeze artifact with rationale, unresolved issues, risks, and carry-forward items.",
            })
        else:
            if not freeze_artifact.get("rationale"):
                issues.append({
                    "code": "freeze_rationale_missing",
                    "message": "Freeze artifact must record the rationale for freezing at the round cap.",
                })
            if not isinstance(freeze_artifact.get("unresolved_issues"), list):
                issues.append({
                    "code": "freeze_unresolved_issues_invalid",
                    "message": "Freeze artifact unresolved_issues must be a JSON array.",
                })
            if not isinstance(freeze_artifact.get("accepted_risks"), list):
                issues.append({
                    "code": "freeze_accepted_risks_invalid",
                    "message": "Freeze artifact accepted_risks must be a JSON array.",
                })
            if not isinstance(freeze_artifact.get("carry_forward_items"), list):
                issues.append({
                    "code": "freeze_carry_forward_invalid",
                    "message": "Freeze artifact carry_forward_items must be a JSON array.",
                })
    if current_round > 0 and not checkpoint_present:
        issues.append({
            "code": "checkpoint_missing",
            "message": "A review loop checkpoint should be recorded after a failed critic round.",
        })
    stage_boundary = detect_stage_boundary_drift(state_name, raw.get("unresolved_issues", []))
    if stage_boundary:
        issues.append(stage_boundary)
    next_actions = []
    if checkpoint_required:
        next_actions.append("record-review-checkpoint")
    if decision_required:
        next_actions.append("record-review-loop-decision")
    freeze_artifact_required = bool(
        freeze_required
        and decision in {"FREEZE_AND_ESCALATE", "APPROVE"}
        and not freeze_artifact
    )
    if freeze_artifact_required:
        next_actions.append("record-freeze-artifact")
    return {
        "present": bool(raw),
        "source": source,
        "path": raw.get("_path") if raw else try_relative_to_workspace(get_review_loop_state_path(project_name, state_name)),
        "current_round": current_round,
        "max_rounds": max_rounds,
        "remaining_rounds": max(max_rounds - current_round, 0),
        "at_round_cap": bool(current_round >= max_rounds and not override_active),
        "another_round_permitted": another_round_permitted,
        "override": override,
        "override_active": override_active,
        "decision": decision,
        "decision_required": decision_required,
        "decision_options": sorted(REVIEW_LOOP_DECISIONS) if freeze_required and not override_active else [],
        "freeze_required": freeze_required,
        "checkpoint_required": checkpoint_required,
        "mode": "frozen_cap" if freeze_required and decision in {"FREEZE_AND_ESCALATE", "APPROVE"} else "normal",
        "checkpoint": checkpoint,
        "unresolved_issues": raw.get("unresolved_issues", []),
        "accepted_risks": raw.get("accepted_risks", []),
        "carry_forward_items": raw.get("carry_forward_items", []),
        "freeze_artifact": {
            "present": bool(freeze_artifact),
            "path": freeze_artifact.get("_path") if freeze_artifact else try_relative_to_workspace(get_freeze_artifact_path(project_name, state_name)),
            "markdown_file": freeze_artifact.get("markdown_file") if freeze_artifact else try_relative_to_workspace(get_freeze_artifact_markdown_path(project_name, state_name)),
            "summary": freeze_artifact.get("summary"),
            "rationale": freeze_artifact.get("rationale"),
            "unresolved_issues": freeze_artifact.get("unresolved_issues", []),
            "accepted_risks": freeze_artifact.get("accepted_risks", []),
            "carry_forward_items": freeze_artifact.get("carry_forward_items", []),
            "checkpoint_file": freeze_artifact.get("checkpoint_file"),
            "updated_at": freeze_artifact.get("updated_at"),
            "audit": freeze_artifact.get("audit", {}),
        },
        "stage_boundary": stage_boundary,
        "note": raw.get("note"),
        "updated_at": raw.get("updated_at"),
        "audit": raw.get("audit", {}),
        "migration": migration,
        "issues": issues,
        "next_actions": next_actions,
        "valid": len(issues) == 0,
    }


def validate_review_loop_write(current_round, max_rounds, override_active, decision=None):
    issues = []
    if current_round > max_rounds and not override_active:
        issues.append({
            "code": "round_cap_write_blocked",
            "message": f"Current round {current_round} exceeds max rounds {max_rounds}. Record an explicit override before persisting round {current_round}.",
        })
    if isinstance(decision, str) and decision and decision not in REVIEW_LOOP_DECISIONS:
        issues.append({
            "code": "invalid_review_loop_decision",
            "message": f"Decision '{decision}' is invalid. Use one of: {', '.join(sorted(REVIEW_LOOP_DECISIONS))}.",
        })
    return issues


def summarize_inter_agent_review(state_info, preconditions):
    child_precondition = next((p for p in preconditions if p["type"] == "child_receipts"), None)
    pm_session_precondition = next((p for p in preconditions if p["type"] == "pm_session_receipt"), None)

    review_files = []
    matched_roles = child_precondition.get("matched_roles", {}) if child_precondition else {}
    for receipt in matched_roles.values():
        metadata = receipt.get("metadata", {}) or {}
        review_file = metadata.get("review_file")
        if review_file and review_file not in review_files:
            review_files.append(review_file)

    waived_roles = child_precondition.get("waived_roles", []) if child_precondition else []
    gate_satisfied = bool(child_precondition and child_precondition.get("satisfied"))
    full_signoff_complete = bool(
        child_precondition
        and len(child_precondition.get("missing_roles", [])) == 0
        and len(waived_roles) == 0
    )
    frozen_cap_waiver_active = bool(child_precondition and child_precondition.get("waiver_reason") == "frozen_cap_path")

    return {
        "required": state_info.get("inter_agent_review", False),
        "producer_role": state_info.get("producer_role"),
        "critic_role": state_info.get("critic_role"),
        "pm_signoff_required": state_info.get("pm_signoff_required", False),
        "review_files": review_files,
        "signed_off": full_signoff_complete,
        "gate_satisfied": gate_satisfied,
        "full_signoff_complete": full_signoff_complete,
        "frozen_cap_waiver_active": frozen_cap_waiver_active,
        "waived_roles": waived_roles,
        "pm_signed_off": bool(child_precondition and "pm" in child_precondition.get("matched_roles", {})),
        "issues": [
            p for p in preconditions if not p.get("satisfied")
        ],
        "child_receipts": child_precondition,
        "pm_session_receipt": pm_session_precondition,
    }


def build_shared_reporting_contract(sm, project_name, project, state_name, stale_after_minutes=15, readiness=None):
    state_info = get_state_info(sm, state_name) or {}
    use_readiness_summaries = readiness is not None and stale_after_minutes == 15
    review_loop = readiness.get("review_loop") if use_readiness_summaries else summarize_review_loop_state(project_name, state_name)
    child_tasks = readiness.get("child_tasks") if use_readiness_summaries else summarize_child_tasks(
        project_name, state_name, stale_after_minutes=stale_after_minutes
    )
    contract = {
        "review_loop": review_loop,
        "child_tasks": child_tasks,
        "child_task_watchdog": summarize_child_task_watchdog(child_tasks),
    }
    if readiness is None:
        return contract

    inter_agent_review = summarize_inter_agent_review(state_info, readiness.get("preconditions", []))
    contract.update({
        "artifact_subject": readiness.get("artifact_subject"),
        "transition_preconditions": readiness.get("preconditions", []),
        "inter_agent_review": inter_agent_review,
        "child_receipts": inter_agent_review.get("child_receipts"),
        "pm_session_receipt": inter_agent_review.get("pm_session_receipt"),
    })
    return contract


def _format_review_checkpoint_items(items):
    if not isinstance(items, list) or not items:
        return "- none"

    lines = []
    for item in items:
        if isinstance(item, dict):
            title = (
                item.get("title")
                or item.get("summary")
                or item.get("issue")
                or item.get("message")
                or item.get("name")
                or canonical_json(item)
            )
            tags = []
            for key in ["severity", "level", "owner", "status", "stage"]:
                value = item.get(key)
                if value:
                    tags.append(f"{key}={value}")
            lines.append(f"- {title}" + (f" ({'; '.join(tags)})" if tags else ""))
            continue
        lines.append(f"- {item}")
    return "\n".join(lines)


def render_freeze_artifact_markdown(
    project_name,
    state_name,
    summary,
    rationale,
    checkpoint_file,
    unresolved_issues,
    accepted_risks,
    carry_forward_items,
    recorded_at,
):
    checkpoint_line = checkpoint_file or "none recorded"
    return f"""# {project_name} {state_name} freeze artifact

Recorded: {recorded_at}

## Summary

{summary or 'No summary recorded.'}

## Rationale

{rationale or 'No rationale recorded.'}

## Linked checkpoint

- {checkpoint_line}

## Unresolved issues

{_format_review_checkpoint_items(unresolved_issues)}

## Accepted risks

{_format_review_checkpoint_items(accepted_risks)}

## Carry-forward items

{_format_review_checkpoint_items(carry_forward_items)}
"""


def render_review_checkpoint_markdown(
    project_name,
    state_name,
    current_round,
    max_rounds,
    summary,
    unresolved_issues,
    accepted_risks,
    carry_forward_items,
    producer_response_status,
    another_round_permitted,
    freeze_required,
    decision,
    override_active,
    override_reason,
    note,
    recorded_at,
):
    next_action = "Another critic round is still allowed." if another_round_permitted else "Do not start another critic round without override or a freeze/cancel decision."
    freeze_line = "Yes" if freeze_required else "No"
    override_line = "Yes" if override_active else "No"
    decision_line = decision or "pending"
    note_block = f"\n## Notes\n\n{note}\n" if note else ""
    return f"""# {project_name} {state_name} review checkpoint, round {current_round}

Recorded: {recorded_at}

## Summary

{summary}

## Status

- State: {state_name}
- Current round: {current_round}
- Max rounds: {max_rounds}
- Producer response status: {producer_response_status}
- Another round permitted: {'yes' if another_round_permitted else 'no'}
- Freeze required now: {freeze_line}
- Recorded decision: {decision_line}
- Override active: {override_line}
{f'- Override reason: {override_reason}' if override_reason else ''}

## Unresolved issues

{_format_review_checkpoint_items(unresolved_issues)}

## Accepted risks

{_format_review_checkpoint_items(accepted_risks)}

## Carry-forward items

{_format_review_checkpoint_items(carry_forward_items)}

## Next action

- {next_action}
- If presented to the operator, include the unresolved issues by severity and whether freeze is now mandatory.
{note_block}""".replace("\n\n\n", "\n\n")


def list_receipts(project_name, state=None):
    root = get_receipts_root(project_name)
    if state:
        root = root / state
    if not root.exists():
        return []

    receipts = []
    for path in sorted(root.rglob("*.json")):
        try:
            data = load_json_file(path)
            data["_path"] = try_relative_to_workspace(path)
            receipts.append(data)
        except Exception:
            continue
    receipts.sort(key=lambda item: (item.get("recorded_at", ""), item.get("_path", "")), reverse=True)
    return receipts


def find_receipts(project_name, state=None, kind=None, role=None, artifact_name=None):
    results = []
    for receipt in list_receipts(project_name, state=state):
        if kind and receipt.get("kind") != kind:
            continue
        if role and receipt.get("role") != role:
            continue
        if artifact_name and receipt.get("subject", {}).get("artifact") != artifact_name:
            continue
        results.append(receipt)
    return results


def summarize_receipt(receipt):
    if not receipt:
        return None
    return {
        "id": receipt.get("receipt_id"),
        "kind": receipt.get("kind"),
        "role": receipt.get("role"),
        "status": receipt.get("status"),
        "recorded_at": receipt.get("recorded_at"),
        "path": receipt.get("_path"),
        "artifact_hash": receipt.get("artifact", {}).get("hash"),
        "audit": receipt.get("audit", {}),
        "subject": receipt.get("subject", {}),
        "metadata": receipt.get("metadata", {}),
    }


def write_receipt(project_name, state, binding_hash, receipt_type, label, payload):
    root = get_receipts_root(project_name) / state / (binding_hash or "unbound")
    root.mkdir(parents=True, exist_ok=True)
    timestamp = now_iso().replace(":", "").replace("-", "")
    filename = f"{receipt_type}-{slugify(label)}-{timestamp}-{uuid.uuid4().hex[:8]}.json"
    path = root / filename
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    payload["_path"] = try_relative_to_workspace(path)
    return path


def receipt_status_matches(receipt, required_statuses=None):
    status = (receipt.get("status") or "").lower()
    if required_statuses:
        return status in {s.lower() for s in required_statuses}
    return status not in RECEIPT_REJECTED_STATUSES


def find_state_bound_receipt(project_name, state, kind, role, current_hash, required_statuses=None):
    matched = None
    stale = None
    for receipt in find_receipts(project_name, state=state, kind=kind, role=role):
        if not receipt_status_matches(receipt, required_statuses=required_statuses):
            continue
        receipt_hash = receipt.get("artifact", {}).get("hash")
        if current_hash and receipt_hash == current_hash:
            if not matched:
                matched = receipt
        elif receipt_hash and receipt_hash != current_hash and not stale:
            stale = receipt
    return matched, stale


def has_artifact_receipt(project_name, state, artifact_name):
    for receipt in find_receipts(project_name, state=state, kind="artifact", artifact_name=artifact_name):
        if receipt_status_matches(receipt):
            return True, receipt
    return False, None


def parse_metadata_json(raw_json):
    if not raw_json:
        return {}
    data = json.loads(raw_json)
    if not isinstance(data, dict):
        raise ValueError("metadata-json must decode to an object")
    return data


def capture_audit(args=None):
    audit = {
        "recorded_at": now_iso(),
        "source": "cli",
    }

    for field, env_names in AUDIT_ENV_MAP.items():
        for env_name in env_names:
            value = os.environ.get(env_name)
            if value:
                audit[field] = value
                break

    if args is not None:
        for field in ("actor_id", "actor_role", "session_id", "request_id", "channel", "source"):
            if hasattr(args, field):
                value = getattr(args, field)
                if value:
                    audit[field] = value

    if not audit.get("actor_id"):
        audit["actor_id"] = os.environ.get("USER", "unknown")
    return audit


def history_actor_value(audit, expected_actor=None):
    return audit.get("actor_role") or audit.get("actor_id") or expected_actor or "unknown"


def add_audit_args(parser):
    parser.add_argument("--actor-id", default=None, help="Actual caller/user id for audit history")
    parser.add_argument("--actor-role", default=None, help="Caller role for audit history")
    parser.add_argument("--session-id", default=None, help="OpenClaw session id for audit history")
    parser.add_argument("--request-id", default=None, help="Request id for audit history")
    parser.add_argument("--channel", default=None, help="Channel name for audit history")
    parser.add_argument("--source", default=None, help="Audit source tag")


# --- Artifact issue helpers ---


def _build_design_artifact_content(project_name):
    spec_path = find_design_spec_path(project_name)
    if not spec_path:
        return None

    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    screens = spec.get("screens", [])
    critiques = spec.get("design_notes", [])
    edge_cases = spec.get("edge_cases", [])
    user_stories = spec.get("user_stories", [])
    flow_map = spec.get("flow_map", [])
    metadata = spec.get("metadata", {})

    lines = [
        f"# DESIGN Artifact: {project_name}",
        "",
        f"**Model:** {metadata.get('model', 'unknown')}",
        f"**Generated:** {metadata.get('timestamp', 'unknown')}",
        f"**Steps:** {', '.join(metadata.get('steps_completed', []))}",
        "",
        f"## Screen Inventory ({len(screens)} screens)",
        "",
        "| # | Screen ID | Title | Components |",
        "|---|-----------|-------|------------|",
    ]
    for i, screen in enumerate(screens, 1):
        components = len(screen.get("components", []))
        lines.append(f"| {i} | `{screen.get('screen_id', '')}` | {screen.get('title', '')} | {components} |")

    lines += [
        "",
        f"## Design Critique ({len(critiques)} issues)",
        "",
        metadata.get("critique_summary", "")[:2000],
        "",
    ]

    by_severity = {}
    for critique in critiques:
        severity = critique.get("severity", "unknown")
        by_severity[severity] = by_severity.get(severity, 0) + 1
    for severity, count in sorted(by_severity.items()):
        lines.append(f"- **{severity}:** {count}")

    lines += [
        "",
        "## Flow & Edge Cases",
        f"- **Flow transitions:** {len(flow_map)}",
        f"- **User stories:** {len(user_stories)}",
        f"- **Edge cases:** {len(edge_cases)}",
    ]

    wireframe_dir = spec_path.parent / "wireframes"
    if wireframe_dir.exists():
        svgs = sorted(wireframe_dir.glob("*.svg"))
        if svgs:
            lines += ["", f"## Wireframes ({len(svgs)} rendered)"]
            for svg in svgs:
                lines.append(f"- `{svg.name}`")

    return "\n".join(lines)


def create_artifact_issue(project_name, state, summary_path, linear_project_id):
    section_header = SECTION_MAP.get(state)
    if not section_header or not summary_path or not linear_project_id:
        return None

    if state == "DESIGN":
        content = _build_design_artifact_content(project_name) or extract_section(summary_path, section_header)
    else:
        content = extract_section(summary_path, section_header)

    short_desc = f"Artifact for {state} stage. Full content posted as comment."
    title = f"[{project_name}] {state} artifact"
    linear_script = SKILL_DIR / "scripts" / "linear_integration.py"

    try:
        result = subprocess.run([
            sys.executable,
            str(linear_script),
            "create-issue",
            "--project-id", linear_project_id,
            "--title", title,
            "--description", short_desc,
            "--state", "In Progress",
            "--assignee", PA_OPS_BOT_ID,
        ], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            issue_id = data.get("issue_id") or data.get("identifier")
            if issue_id and content:
                try:
                    subprocess.run([
                        sys.executable,
                        str(linear_script),
                        "add-comment",
                        "--issue-id", issue_id,
                        "--body", content[:50000],
                    ], capture_output=True, text=True, timeout=30)
                except Exception:
                    pass
            return issue_id
    except Exception:
        pass
    return None


def close_artifact_and_post_update(project_name, state, summary_path, linear_project_id, artifact_issue_id):
    linear_script = SKILL_DIR / "scripts" / "linear_integration.py"
    result_info = {}

    if artifact_issue_id:
        try:
            subprocess.run([
                sys.executable,
                str(linear_script),
                "update-state",
                "--issue-id", artifact_issue_id,
                "--state", "Done",
            ], capture_output=True, text=True, timeout=30)
            result_info["issue_closed"] = True
        except Exception:
            result_info["issue_closed"] = False

    section_header = SECTION_MAP.get(state)
    if section_header and summary_path and linear_project_id:
        content = extract_section(summary_path, section_header)
        if content:
            short_desc = f"{state} stage approved. See latest project update for details."
            try:
                subprocess.run([
                    sys.executable,
                    str(linear_script),
                    "update-project-description",
                    "--project-id", linear_project_id,
                    "--body", short_desc[:255],
                ], capture_output=True, text=True, timeout=30)
            except Exception:
                pass

            update_body = f"# {state} Stage Approved\n\n{content}"
            try:
                response = subprocess.run([
                    sys.executable,
                    str(linear_script),
                    "post-project-update",
                    "--project-id", linear_project_id,
                    "--body", update_body[:50000],
                ], capture_output=True, text=True, timeout=30)
                if response.returncode == 0:
                    data = json.loads(response.stdout.strip())
                    result_info["project_update_id"] = data.get("update_id")
                else:
                    result_info["project_update_error"] = response.stderr.strip() or response.stdout.strip()
            except Exception as exc:
                result_info["project_update_error"] = str(exc)

    return result_info if result_info else None


# --- Artifact validation ---


def check_artifacts(sm, project, state_name, project_name=None):
    state_def = get_state_info(sm, state_name)
    if not state_def:
        return [], []

    artifacts = state_def.get("artifacts", [])
    found = []
    missing = []

    for artifact in artifacts:
        exists = False

        if artifact == "projects_yaml_entry":
            exists = True

        elif artifact == "linear_project":
            exists = bool(project.get("linear_project_id"))

        elif artifact == "brief_section_in_project_summary":
            exists = check_summary_section(project, "Brief")

        elif artifact == "architecture_section_in_project_summary":
            exists = check_summary_section(project, "Architecture")

        elif artifact == "plan_section":
            exists = check_summary_section(project, "Plan")

        elif artifact == "linear_issues":
            summary_path = project.get("summary")
            if summary_path:
                full_path = WORKSPACE_DIR / summary_path
                if full_path.exists():
                    content = full_path.read_text()
                    exists = "MET-" in content and "## Plan" in content

        elif artifact == "design_spec_json":
            exists = find_design_spec_path(project_name or project.get("_name", "")) is not None

        elif artifact == "wireframe_svgs":
            spec_path = find_design_spec_path(project_name or project.get("_name", ""))
            exists = bool(spec_path and (spec_path.parent / "wireframes").exists() and list((spec_path.parent / "wireframes").glob("*.svg")))

        elif artifact == "design_critique_report":
            exists = find_design_spec_path(project_name or project.get("_name", "")) is not None

        elif artifact in ("api_schemas_verified", "api_schema_section"):
            exists = check_summary_section(project, "Verified API Schemas")

        elif artifact in ("human_e2e_passed", "human_e2e_section"):
            exists = check_summary_section(project, "Human E2E Test Report")

        elif artifact == "review_summary":
            exists = check_summary_section(project, "Review")
            if not exists and project_name:
                exists, _ = has_artifact_receipt(project_name, state_name, artifact)

        elif artifact == "retrospective_note":
            exists = check_summary_section(project, "Retrospective")
            if not exists and project_name:
                exists, _ = has_artifact_receipt(project_name, state_name, artifact)

        elif artifact in {
            "code_on_branch",
            "test_results",
            "pr_with_review",
            "merged_pr",
            "test_verification_report",
            "linear_audit_report",
        }:
            if project_name:
                exists, _ = has_artifact_receipt(project_name, state_name, artifact)

        elif artifact == "updated_projects_yaml":
            exists = True

        if exists:
            found.append(artifact)
        else:
            missing.append(artifact)

    return found, missing


def get_state_transition_preconditions(sm, state_name, target_name=None):
    state_info = get_state_info(sm, state_name) or {}
    preconditions = list(state_info.get("transition_preconditions", []))
    if target_name:
        transition = get_transition_definition(sm, "", state_name, target_name)
        if transition and transition.get("preconditions"):
            preconditions.extend(transition.get("preconditions", []))
    return preconditions


def evaluate_transition_readiness(sm, project_name, project, state_name, target_name=None):
    state_info = get_state_info(sm, state_name) or {}
    found, missing = check_artifacts(sm, project, state_name, project_name=project_name)
    artifact_subject = build_state_artifact_subject(project_name, project, state_name)
    artifact_hash = artifact_subject.get("hash") if artifact_subject else None
    review_loop = summarize_review_loop_state(project_name, state_name)
    child_tasks = summarize_child_tasks(project_name, state_name)

    preconditions = state_info.get("transition_preconditions", [])
    results = []

    for precondition in preconditions:
        p_type = precondition.get("type") if isinstance(precondition, dict) else str(precondition)
        result = {"type": p_type, "satisfied": False}

        if p_type == "validation_passed":
            result.update({
                "satisfied": len(missing) == 0,
                "found": found,
                "missing": missing,
            })

        elif p_type == "approval_receipt":
            role = precondition.get("role", "operator")
            decision = precondition.get("decision", "approved")
            if isinstance(decision, list):
                required_statuses = decision
            else:
                required_statuses = [decision]
            if not artifact_hash:
                result.update({
                    "role": role,
                    "required_statuses": required_statuses,
                    "reason": "artifact_subject_missing",
                    "satisfied": False,
                })
            else:
                matched, stale = find_state_bound_receipt(
                    project_name, state_name, "approval", role, artifact_hash, required_statuses=required_statuses
                )
                result.update({
                    "role": role,
                    "required_statuses": required_statuses,
                    "matched_receipt": summarize_receipt(matched),
                    "stale_receipt": summarize_receipt(stale),
                    "satisfied": matched is not None,
                })

        elif p_type == "child_receipts":
            roles = list(precondition.get("roles", []))
            required_statuses = [precondition.get("decision", "approved")]
            review_loop_mode = review_loop.get("mode")
            frozen_cap_path = (
                state_info.get("inter_agent_review")
                and review_loop_mode == "frozen_cap"
                and review_loop.get("decision") in {"FREEZE_AND_ESCALATE", "APPROVE"}
                and review_loop.get("freeze_artifact", {}).get("present")
                and review_loop.get("valid")
            )
            required_roles = ["pm"] if frozen_cap_path and "pm" in roles else roles
            waived_roles = [role for role in roles if role not in required_roles]
            missing_roles = []
            stale_roles = {}
            matched_roles = {}
            optional_missing_roles = []
            optional_stale_roles = {}
            if not artifact_hash:
                missing_roles = required_roles
                optional_missing_roles = waived_roles
            else:
                for role in roles:
                    matched, stale = find_state_bound_receipt(
                        project_name, state_name, "child", role, artifact_hash, required_statuses=required_statuses
                    )
                    if matched:
                        matched_roles[role] = summarize_receipt(matched)
                    elif role in required_roles:
                        missing_roles.append(role)
                        if stale:
                            stale_roles[role] = summarize_receipt(stale)
                    else:
                        optional_missing_roles.append(role)
                        if stale:
                            optional_stale_roles[role] = summarize_receipt(stale)
            result.update({
                "roles": roles,
                "required_roles": required_roles,
                "waived_roles": waived_roles,
                "required_statuses": required_statuses,
                "matched_roles": matched_roles,
                "missing_roles": missing_roles,
                "stale_roles": stale_roles,
                "optional_missing_roles": optional_missing_roles,
                "optional_stale_roles": optional_stale_roles,
                "review_loop_mode": review_loop_mode,
                "waiver_reason": "frozen_cap_path" if frozen_cap_path else None,
                "satisfied": len(missing_roles) == 0,
            })

        elif p_type == "pm_session_receipt":
            role = precondition.get("role", "pm")
            required_statuses = precondition.get("statuses", ["active", "completed"])
            if not artifact_hash:
                result.update({
                    "role": role,
                    "required_statuses": required_statuses,
                    "reason": "artifact_subject_missing",
                    "satisfied": False,
                })
            else:
                matched, stale = find_state_bound_receipt(
                    project_name, state_name, "pm_session", role, artifact_hash, required_statuses=required_statuses
                )
                result.update({
                    "role": role,
                    "required_statuses": required_statuses,
                    "matched_receipt": summarize_receipt(matched),
                    "stale_receipt": summarize_receipt(stale),
                    "satisfied": matched is not None,
                })

        else:
            result.update({
                "reason": f"unknown_precondition_type:{p_type}",
                "satisfied": False,
            })

        results.append(result)

    if child_tasks.get("present"):
        results.append({
            "type": "child_task_health",
            "satisfied": child_tasks.get("valid", True),
            "summary": child_tasks.get("summary", {}),
            "issues": child_tasks.get("issues", []),
            "path": child_tasks.get("path"),
        })

    if target_name and target_name != "CANCELED":
        cancel_recorded = review_loop.get("present") and review_loop.get("decision") == "CANCEL"
        results.append({
            "type": "review_loop_decision",
            "satisfied": not cancel_recorded,
            "decision": review_loop.get("decision"),
            "state": state_name,
            "target": target_name,
            "action": "Transition the project to CANCELED instead of advancing to the next state" if cancel_recorded else None,
            "reason": "cancel_decision_recorded" if cancel_recorded else None,
        })

    valid = all(item.get("satisfied") for item in results) if results else len(missing) == 0
    return {
        "valid": valid,
        "artifacts": {
            "found": found,
            "missing": missing,
        },
        "artifact_subject": artifact_subject,
        "preconditions": results,
        "state": state_name,
        "target": target_name,
        "review_loop": review_loop,
        "child_tasks": child_tasks,
    }


def sync_linear_project_state(project_id, linear_state):
    """Sync Linear project status to match the framework state."""
    PROJECT_STATUS_MAP = {
        "Backlog": "Backlog",
        "Todo": "Planned",
        "In Progress": "In Progress",
        "In Dev": "In Progress",
        "Review": "In Progress",
        "In Prod": "Completed",
        "Done": "Completed",
        "Canceled": "Canceled",
    }
    project_status = PROJECT_STATUS_MAP.get(linear_state, "In Progress")

    try:
        token = os.environ.get("LINEAR_TOKEN") or os.environ.get("LINEAR_API_TOKEN")
        if not token:
            try:
                with open("/tmp/.linear_token") as f:
                    token = f.read().strip()
            except (FileNotFoundError, PermissionError):
                pass
        if not token:
            try:
                result = subprocess.run(
                    ["op", "read", "op://OpenClaw-Justin-PA/linear/credential"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    token = result.stdout.strip()
            except Exception:
                pass
        if not token:
            return {"synced": False, "error": "No LINEAR_TOKEN available"}

        import urllib.request

        query_statuses = '{ projectStatuses { nodes { id name } } }'
        req = urllib.request.Request(
            'https://api.linear.app/graphql',
            data=json.dumps({"query": query_statuses}).encode(),
            headers={'Authorization': token, 'Content-Type': 'application/json'}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        statuses = {
            item["name"]: item["id"]
            for item in resp.get("data", {}).get("projectStatuses", {}).get("nodes", [])
        }

        status_id = statuses.get(project_status)
        if not status_id:
            return {"synced": False, "error": f"Status '{project_status}' not found in Linear"}

        query_update = """mutation($id: String!, $statusId: String!) {
            projectUpdate(id: $id, input: { statusId: $statusId }) { success }
        }"""
        req = urllib.request.Request(
            'https://api.linear.app/graphql',
            data=json.dumps({"query": query_update, "variables": {"id": project_id, "statusId": status_id}}).encode(),
            headers={'Authorization': token, 'Content-Type': 'application/json'}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        success = resp.get("data", {}).get("projectUpdate", {}).get("success", False)
        return {"synced": success, "project_status": project_status, "linear_state": linear_state}

    except Exception as exc:
        return {"synced": False, "error": str(exc)}


# --- Commands ---


def cmd_init(args, sm, projects):
    name = args.name
    tier = args.tier

    if tier not in sm["tiers"]:
        print(json.dumps({
            "ok": False,
            "error": f"Invalid tier: {tier}. Must be one of: {list(sm['tiers'].keys())}"
        }))
        return 1

    prjs = projects.get("projects", {})
    if name in prjs:
        print(json.dumps({
            "ok": False,
            "error": f"Project '{name}' already exists in PROJECTS.yaml"
        }))
        return 1

    tier_def = sm["tiers"][tier]
    initial_state = tier_def["states"][0]
    audit = capture_audit(args)

    prjs[name] = {
        "repo": None,
        "path": None,
        "summary": f"projects/{name}.md",
        "description": "",
        "tags": [],
        "tier": tier,
        "state": initial_state,
        "state_history": [{
            "state": initial_state,
            "entered_at": now_iso(),
            "actor": history_actor_value(audit, expected_actor="pa"),
            "expected_actor": "pa",
            "recorded_by": audit,
        }],
        "linear_project_id": None,
    }
    projects["projects"] = prjs
    save_projects(projects)

    summary_path = PROJECTS_DIR / f"{name}.md"
    if not summary_path.exists():
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(f"# {name}\n\n**Tier:** {tier}\n**State:** {initial_state}\n")

    get_project_runtime_dir(name).mkdir(parents=True, exist_ok=True)

    linear_project_id = None
    linear_url = None
    linear_error = None
    linear_script = SKILL_DIR / "scripts" / "linear_integration.py"
    if linear_script.exists():
        try:
            display_name = args.display_name or name.replace("-", " ").title()
            command = [
                sys.executable, str(linear_script),
                "create-project",
                "--name", display_name,
                "--description", args.description or f"[{tier}] {name}",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout.strip())
                linear_project_id = data.get("project_id")
                linear_url = data.get("url")
                projects_reloaded = load_projects()
                projects_reloaded["projects"][name]["linear_project_id"] = linear_project_id
                save_projects(projects_reloaded)
            else:
                linear_error = result.stderr.strip() or result.stdout.strip()
        except Exception as exc:
            linear_error = str(exc)

    state_info = get_state_info(sm, initial_state)
    print(json.dumps({
        "ok": True,
        "project": name,
        "tier": tier,
        "tier_description": tier_def["description"],
        "state": initial_state,
        "states_in_tier": tier_def["states"],
        "linear_project_id": linear_project_id,
        "linear_url": linear_url,
        "linear_error": linear_error,
        "exit_criteria": state_info.get("exit_criteria", []),
        "next_steps": [
            f"Classify tier (already set to '{tier}')",
            "Update PROJECTS.yaml with repo, path, description, tags",
            f"Transition to {tier_def['states'][1]} when exit criteria met",
        ],
        "audit": audit,
    }, indent=2))
    return 0


def cmd_status(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = project.get("state", "UNKNOWN")
    tier = project.get("tier", "feature")
    state_info = get_state_info(sm, state)
    if not state_info:
        print(json.dumps({"ok": False, "error": f"Unknown state: {state}"}))
        return 1

    readiness = evaluate_transition_readiness(sm, name, project, state)
    valid_transitions = get_valid_transitions(sm, tier, state)
    payload = {
        "ok": True,
        "project": name,
        "tier": tier,
        "state": state,
        "linear_state": state_info.get("linear_state", "Unknown"),
        "actor": state_info.get("actor"),
        "approval_gate": state_info.get("approval_gate", False),
        "approval_by": state_info.get("approval_by"),
        "exit_criteria": state_info.get("exit_criteria", []),
        "artifacts": readiness["artifacts"],
        "transition_preconditions": readiness["preconditions"],
        "ready_to_transition": readiness["valid"],
        "artifact_subject": readiness["artifact_subject"],
        "valid_transitions": valid_transitions,
        "state_history": project.get("state_history", []),
    }

    if getattr(args, "verbose", False):
        contract = build_shared_reporting_contract(sm, name, project, state, readiness=readiness)
        payload["review_loop"] = contract["review_loop"]
        payload["child_tasks"] = contract["child_tasks"]
        payload["inter_agent_review"] = contract["inter_agent_review"]

    print(json.dumps(payload, indent=2))
    return 0


def cmd_validate(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = project.get("state", "UNKNOWN")
    tier = project.get("tier", "feature")
    state_info = get_state_info(sm, state)
    if not state_info:
        print(json.dumps({"ok": False, "error": f"Unknown state: {state}"}))
        return 1

    readiness = evaluate_transition_readiness(sm, name, project, state)
    contract = build_shared_reporting_contract(sm, name, project, state, readiness=readiness)
    review_loop = contract["review_loop"]
    child_tasks = contract["child_tasks"]
    overall_valid = readiness["valid"] and review_loop["valid"] and child_tasks["valid"]

    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "tier": tier,
        "valid": overall_valid,
        "exit_criteria": state_info.get("exit_criteria", []),
        "artifacts": readiness["artifacts"],
        "artifact_subject": readiness["artifact_subject"],
        "transition_preconditions": readiness["preconditions"],
        "review_loop": review_loop,
        "child_tasks": child_tasks,
        "inter_agent_review": contract["inter_agent_review"],
        "ready_to_transition": overall_valid,
    }, indent=2))
    return 0 if overall_valid else 2


def cmd_review_status(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = project.get("state", "UNKNOWN")
    state_info = get_state_info(sm, state)
    if not state_info:
        print(json.dumps({"ok": False, "error": f"Unknown state: {state}"}))
        return 1

    readiness = evaluate_transition_readiness(sm, name, project, state)
    contract = build_shared_reporting_contract(sm, name, project, state, readiness=readiness)

    result = {
        "ok": True,
        "project": name,
        "state": state,
        "inter_agent_review_required": state_info.get("inter_agent_review", False),
        "producer_role": state_info.get("producer_role"),
        "critic_role": state_info.get("critic_role"),
        "pm_signoff_required": state_info.get("pm_signoff_required", False),
        "review_files": contract["inter_agent_review"]["review_files"],
        "signed_off": contract["inter_agent_review"]["signed_off"],
        "gate_satisfied": contract["inter_agent_review"]["gate_satisfied"],
        "full_signoff_complete": contract["inter_agent_review"]["full_signoff_complete"],
        "frozen_cap_waiver_active": contract["inter_agent_review"]["frozen_cap_waiver_active"],
        "waived_roles": contract["inter_agent_review"]["waived_roles"],
        "pm_signed_off": contract["inter_agent_review"]["pm_signed_off"],
        "review_loop": contract["review_loop"],
        "child_tasks": contract["child_tasks"],
        "child_receipts": contract["child_receipts"],
        "pm_session_receipt": contract["pm_session_receipt"],
        "artifact_subject": contract["artifact_subject"],
    }
    print(json.dumps(result, indent=2))
    return 0


def cmd_record_review_loop(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    audit = capture_audit(args)
    existing = load_review_loop_state(name, state) or {}
    existing_override = existing.get("override", {}) if isinstance(existing.get("override"), dict) else {}
    current_round = args.current_round if args.current_round is not None else existing.get("current_round", 0)
    max_rounds = args.max_rounds if args.max_rounds is not None else existing.get("max_rounds", 3)
    override_active = bool(args.override) if args.override else bool(existing_override.get("active", False))
    decision = (args.decision or existing.get("decision") or None)
    if isinstance(decision, str):
        decision = decision.strip().upper() or None

    write_issues = validate_review_loop_write(
        current_round=current_round,
        max_rounds=max_rounds,
        override_active=override_active,
        decision=decision,
    )
    if write_issues:
        print(json.dumps({
            "ok": False,
            "project": name,
            "state": state,
            "errors": write_issues,
        }, indent=2))
        return 2

    payload = {
        "project": name,
        "state": state,
        "current_round": current_round,
        "max_rounds": max_rounds,
        "decision": decision,
        "freeze_required": bool(args.freeze_required) if args.freeze_required else bool(existing.get("freeze_required", False)),
        "checkpoint": {
            "summary": args.checkpoint_summary or existing.get("checkpoint", {}).get("summary"),
            "file": args.checkpoint_file or existing.get("checkpoint", {}).get("file"),
            "producer_response_status": existing.get("checkpoint", {}).get("producer_response_status"),
            "another_round_permitted": existing.get("checkpoint", {}).get("another_round_permitted"),
            "recorded_at": audit["recorded_at"],
        },
        "override": {
            "active": override_active,
            "reason": args.override_reason or existing_override.get("reason"),
            "recorded_at": audit["recorded_at"] if args.override or args.override_reason else existing_override.get("recorded_at"),
        },
        "unresolved_issues": json.loads(args.unresolved_issues_json) if args.unresolved_issues_json else existing.get("unresolved_issues", []),
        "accepted_risks": json.loads(args.accepted_risks_json) if args.accepted_risks_json else existing.get("accepted_risks", []),
        "carry_forward_items": json.loads(args.carry_forward_json) if args.carry_forward_json else existing.get("carry_forward_items", []),
        "note": args.note or existing.get("note"),
        "updated_at": audit["recorded_at"],
        "audit": audit,
    }
    payload = persist_review_loop_state(name, state, payload)
    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "review_loop": summarize_review_loop_state(name, state),
        "review_loop_path": payload.get("_path"),
    }, indent=2))
    return 0


def cmd_record_review_loop_decision(args, sm, projects):
    if not args.decision:
        print(json.dumps({
            "ok": False,
            "project": args.name,
            "error": "--decision is required for record-review-loop-decision",
        }, indent=2))
        return 2

    return cmd_record_review_loop(args, sm, projects)


def cmd_backfill_review_loop(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    inferred_files = find_review_round_files(name, state)
    if not inferred_files:
        print(json.dumps({
            "ok": False,
            "project": name,
            "state": state,
            "error": "No review round files found to backfill.",
        }, indent=2))
        return 1

    existing = load_review_loop_state(name, state) or {}
    audit = capture_audit(args)
    inferred_round = max((item.get("round", 0) for item in inferred_files), default=0)
    latest_review_file = inferred_files[-1]["path"]
    payload = {
        "project": name,
        "state": state,
        "current_round": args.current_round if args.current_round is not None else existing.get("current_round", inferred_round),
        "max_rounds": args.max_rounds if args.max_rounds is not None else existing.get("max_rounds", max(3, inferred_round)),
        "decision": existing.get("decision"),
        "freeze_required": bool(existing.get("freeze_required", False)),
        "checkpoint": {
            "summary": args.checkpoint_summary or existing.get("checkpoint", {}).get("summary") or f"Backfilled from existing {state} review artifacts.",
            "file": args.checkpoint_file or existing.get("checkpoint", {}).get("file") or latest_review_file,
            "producer_response_status": existing.get("checkpoint", {}).get("producer_response_status"),
            "another_round_permitted": existing.get("checkpoint", {}).get("another_round_permitted"),
            "recorded_at": audit["recorded_at"],
        },
        "override": existing.get("override") if isinstance(existing.get("override"), dict) else {"active": False, "reason": None, "recorded_at": None},
        "unresolved_issues": existing.get("unresolved_issues", []),
        "accepted_risks": existing.get("accepted_risks", []),
        "carry_forward_items": existing.get("carry_forward_items", []),
        "note": args.note or existing.get("note") or "Backfilled from legacy review round files.",
        "updated_at": audit["recorded_at"],
        "audit": {
            **audit,
            "backfill_source": "review_files",
            "backfill_review_files": inferred_files,
        },
    }
    persisted = persist_review_loop_state(name, state, payload)
    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "review_loop_path": persisted.get("_path"),
        "backfilled_from": inferred_files,
        "review_loop": summarize_review_loop_state(name, state),
    }, indent=2))
    return 0


def cmd_record_freeze_artifact(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    audit = capture_audit(args)
    existing = load_freeze_artifact(name, state) or {}
    markdown_path = Path(args.output) if getattr(args, "output", None) else Path(existing.get("markdown_file") or get_freeze_artifact_markdown_path(name, state))
    if not markdown_path.is_absolute():
        markdown_path = WORKSPACE_DIR / markdown_path
    markdown_path.parent.mkdir(parents=True, exist_ok=True)

    unresolved_issues = json.loads(args.unresolved_issues_json) if args.unresolved_issues_json else existing.get("unresolved_issues", [])
    accepted_risks = json.loads(args.accepted_risks_json) if args.accepted_risks_json else existing.get("accepted_risks", [])
    carry_forward_items = json.loads(args.carry_forward_json) if args.carry_forward_json else existing.get("carry_forward_items", [])
    summary = args.summary or existing.get("summary")
    rationale = args.rationale or existing.get("rationale")
    checkpoint_file = args.checkpoint_file or existing.get("checkpoint_file")

    markdown_path.write_text(render_freeze_artifact_markdown(
        project_name=name,
        state_name=state,
        summary=summary,
        rationale=rationale,
        checkpoint_file=checkpoint_file,
        unresolved_issues=unresolved_issues,
        accepted_risks=accepted_risks,
        carry_forward_items=carry_forward_items,
        recorded_at=audit["recorded_at"],
    ))

    payload = {
        "project": name,
        "state": state,
        "summary": summary,
        "rationale": rationale,
        "checkpoint_file": checkpoint_file,
        "markdown_file": try_relative_to_workspace(markdown_path),
        "unresolved_issues": unresolved_issues,
        "accepted_risks": accepted_risks,
        "carry_forward_items": carry_forward_items,
        "updated_at": audit["recorded_at"],
        "audit": audit,
    }
    payload = persist_freeze_artifact(name, state, payload)
    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "freeze_artifact_path": payload.get("_path"),
        "freeze_artifact_markdown": payload.get("markdown_file"),
        "review_loop": summarize_review_loop_state(name, state),
    }, indent=2))
    return 0


def cmd_record_review_checkpoint(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    audit = capture_audit(args)
    existing = load_review_loop_state(name, state) or {}
    existing_override = existing.get("override", {}) if isinstance(existing.get("override"), dict) else {}
    current_round = args.current_round if args.current_round is not None else existing.get("current_round", 1)
    max_rounds = args.max_rounds if args.max_rounds is not None else existing.get("max_rounds", 3)
    override_active = bool(args.override) if args.override else bool(existing_override.get("active", False))
    freeze_required = bool(args.freeze_required) if args.freeze_required else bool(existing.get("freeze_required", False) or (current_round >= max_rounds and not override_active))
    unresolved_issues = json.loads(args.unresolved_issues_json) if args.unresolved_issues_json else existing.get("unresolved_issues", [])
    accepted_risks = json.loads(args.accepted_risks_json) if args.accepted_risks_json else existing.get("accepted_risks", [])
    carry_forward_items = json.loads(args.carry_forward_json) if args.carry_forward_json else existing.get("carry_forward_items", [])
    decision = args.decision or existing.get("decision") or None
    if isinstance(decision, str):
        decision = decision.strip().upper() or None

    write_issues = validate_review_loop_write(
        current_round=current_round,
        max_rounds=max_rounds,
        override_active=override_active,
        decision=decision,
    )
    if write_issues:
        print(json.dumps({
            "ok": False,
            "project": name,
            "state": state,
            "errors": write_issues,
        }, indent=2))
        return 2

    producer_response_status = args.producer_response_status or existing.get("checkpoint", {}).get("producer_response_status") or "pending"
    summary = args.summary or existing.get("checkpoint", {}).get("summary") or f"Round {current_round} critic checkpoint recorded."
    output_path = Path(args.output) if args.output else get_review_checkpoint_path(name, state, current_round)
    if not output_path.is_absolute():
        output_path = WORKSPACE_DIR / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    another_round_permitted = current_round < max_rounds or override_active
    note = args.note or existing.get("note")
    checkpoint_markdown = render_review_checkpoint_markdown(
        project_name=name,
        state_name=state,
        current_round=current_round,
        max_rounds=max_rounds,
        summary=summary,
        unresolved_issues=unresolved_issues,
        accepted_risks=accepted_risks,
        carry_forward_items=carry_forward_items,
        producer_response_status=producer_response_status,
        another_round_permitted=another_round_permitted,
        freeze_required=freeze_required,
        decision=decision,
        override_active=override_active,
        override_reason=args.override_reason or existing_override.get("reason"),
        note=note,
        recorded_at=audit["recorded_at"],
    )
    output_path.write_text(checkpoint_markdown)

    payload = {
        "project": name,
        "state": state,
        "current_round": current_round,
        "max_rounds": max_rounds,
        "decision": decision,
        "freeze_required": freeze_required,
        "checkpoint": {
            "summary": summary,
            "file": try_relative_to_workspace(output_path),
            "producer_response_status": producer_response_status,
            "another_round_permitted": another_round_permitted,
            "recorded_at": audit["recorded_at"],
        },
        "override": {
            "active": override_active,
            "reason": args.override_reason or existing_override.get("reason"),
            "recorded_at": audit["recorded_at"] if args.override or args.override_reason else existing_override.get("recorded_at"),
        },
        "unresolved_issues": unresolved_issues,
        "accepted_risks": accepted_risks,
        "carry_forward_items": carry_forward_items,
        "note": note,
        "updated_at": audit["recorded_at"],
        "audit": audit,
    }
    payload = persist_review_loop_state(name, state, payload)
    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "checkpoint_file": try_relative_to_workspace(output_path),
        "review_loop": summarize_review_loop_state(name, state),
        "review_loop_path": payload.get("_path"),
    }, indent=2))
    return 0


def cmd_record_child_task(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    audit = capture_audit(args)
    task_id = args.task_id
    existing = load_child_task(name, state, task_id) or {}
    status = str(args.status or existing.get("status") or "active").strip().lower()
    heartbeat_at = args.heartbeat_at or audit["recorded_at"]
    payload = {
        "project": name,
        "state": state,
        "task_id": task_id,
        "label": args.label or existing.get("label"),
        "kind": args.kind or existing.get("kind") or "subagent",
        "owner": args.owner or existing.get("owner") or "pa",
        "status": status,
        "current_step": args.current_step or existing.get("current_step"),
        "summary": args.summary or existing.get("summary"),
        "started_at": args.started_at or existing.get("started_at") or audit["recorded_at"],
        "heartbeat_at": heartbeat_at,
        "blocked_reason": args.blocked_reason or existing.get("blocked_reason"),
        "attention_required": bool(args.attention_required) if args.attention_required else bool(existing.get("attention_required", False)),
        "session_label": args.session_label or existing.get("session_label"),
        "metadata": parse_metadata_json(args.metadata_json) if args.metadata_json else existing.get("metadata", {}),
        "updated_at": audit["recorded_at"],
        "audit": audit,
    }
    if status in CHILD_TASK_TERMINAL_STATUSES:
        payload["attention_required"] = False
        payload["blocked_reason"] = None
    payload = persist_child_task(name, state, task_id, payload)
    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "task_id": task_id,
        "child_task": payload,
        "child_tasks": summarize_child_tasks(name, state),
    }, indent=2))
    return 0


def cmd_child_task_status(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    contract = build_shared_reporting_contract(
        sm,
        name,
        project,
        state,
        stale_after_minutes=args.stale_after_minutes,
    )
    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "child_tasks": contract["child_tasks"],
    }, indent=2))
    return 0


def cmd_backfill_child_tasks(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    inferred_tasks, inferred_receipts = infer_child_tasks_from_receipts(name, state)
    if not inferred_tasks:
        print(json.dumps({
            "ok": False,
            "project": name,
            "state": state,
            "error": "No child or pm_session receipts found to backfill.",
        }, indent=2))
        return 1

    audit = capture_audit(args)
    persisted_tasks = []
    for task in inferred_tasks:
        task_id = task.get("task_id")
        metadata = dict(task.get("metadata") or {})
        metadata["backfill"] = {
            "source": "receipts",
            "recorded_at": audit["recorded_at"],
            "receipt_id": metadata.get("receipt_id"),
            "receipt_path": metadata.get("receipt_path"),
        }
        payload = {
            "project": name,
            "state": state,
            "task_id": task_id,
            "label": task.get("label"),
            "kind": task.get("kind"),
            "owner": task.get("owner"),
            "status": task.get("status"),
            "current_step": task.get("current_step"),
            "summary": task.get("summary"),
            "started_at": task.get("started_at"),
            "heartbeat_at": task.get("heartbeat_at"),
            "blocked_reason": task.get("blocked_reason"),
            "attention_required": bool(task.get("attention_required")),
            "session_label": task.get("session_label"),
            "metadata": metadata,
            "updated_at": audit["recorded_at"],
            "audit": {
                **audit,
                "backfill_source": "receipts",
            },
        }
        persisted_tasks.append(persist_child_task(name, state, task_id, payload))

    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "backfilled_count": len(persisted_tasks),
        "backfilled_from": inferred_receipts,
        "persisted_tasks": persisted_tasks,
        "child_tasks": summarize_child_tasks(name, state),
    }, indent=2))
    return 0


def cmd_child_task_watchdog(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state", "UNKNOWN")
    contract = build_shared_reporting_contract(
        sm,
        name,
        project,
        state,
        stale_after_minutes=args.stale_after_minutes,
    )
    child_tasks = contract["child_tasks"]
    watchdog = contract["child_task_watchdog"]
    exceptions = []
    for issue in watchdog.get("exceptions", []):
        severity = str(issue.get("severity") or "significant").lower()
        if args.exception_only and severity == "minor":
            continue
        exceptions.append(issue)

    payload = {
        "ok": True,
        "project": name,
        "state": state,
        "stale_after_minutes": args.stale_after_minutes,
        "exceptions_only": bool(args.exception_only),
        "child_tasks": child_tasks,
        "should_alert": len(exceptions) > 0,
        "summary": watchdog.get("summary", {}),
        "exceptions": exceptions,
        "child_tasks_path": child_tasks.get("path"),
    }
    print(json.dumps(payload, indent=2))
    if payload["should_alert"] and args.exit_nonzero_on_alert:
        return 2
    return 0


def cmd_transition(args, sm, projects):
    name = args.name
    target = args.target_state
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = project.get("state", "UNKNOWN")
    tier = project.get("tier", "feature")
    audit = capture_audit(args)

    if target not in sm["states"] and target != "CANCELED":
        print(json.dumps({
            "ok": False,
            "error": f"Unknown target state: {target}",
            "valid_states": list(sm["states"].keys())
        }))
        return 1

    if target == "CANCELED":
        history = list(project.get("state_history", []))
        history.append({
            "state": "CANCELED",
            "entered_at": now_iso(),
            "from_state": state,
            "actor": history_actor_value(audit, expected_actor="operator"),
            "expected_actor": "operator",
            "recorded_by": audit,
        })
        project["state"] = "CANCELED"
        project["state_history"] = history
        save_projects(projects)
        print(json.dumps({
            "ok": True,
            "project": name,
            "previous_state": state,
            "new_state": "CANCELED",
            "linear_state": "Canceled",
            "audit": audit,
        }, indent=2))
        return 0

    tier_states = sm["tiers"].get(tier, {}).get("states", [])
    if target not in tier_states:
        print(json.dumps({
            "ok": False,
            "error": f"State '{target}' is not valid for tier '{tier}'",
            "valid_states_for_tier": tier_states,
        }))
        return 1

    transition = get_transition_definition(sm, tier, state, target)
    if not transition:
        print(json.dumps({
            "ok": False,
            "error": f"No valid transition from '{state}' to '{target}'",
            "valid_transitions": get_valid_transitions(sm, tier, state),
        }))
        return 1

    readiness = evaluate_transition_readiness(sm, name, project, state, target_name=target)
    if not readiness["valid"]:
        print(json.dumps({
            "ok": False,
            "project": name,
            "previous_state": state,
            "attempted_state": target,
            "error": "Transition blocked. Current state failed deterministic preconditions.",
            "artifacts": readiness["artifacts"],
            "artifact_subject": readiness["artifact_subject"],
            "transition_preconditions": readiness["preconditions"],
            "review_loop": readiness.get("review_loop"),
            "child_tasks": readiness.get("child_tasks"),
        }, indent=2))
        return 2

    linear_project_id = project.get("linear_project_id")
    summary_path = project.get("summary")
    artifact_close_info = None

    if state in APPROVAL_GATE_STATES and linear_project_id:
        prev_artifact_issue_id = None
        for entry in reversed(project.get("state_history", [])):
            if entry.get("state") == state and entry.get("artifact_issue_id"):
                prev_artifact_issue_id = entry["artifact_issue_id"]
                break
        if prev_artifact_issue_id:
            artifact_close_info = close_artifact_and_post_update(
                name, state, summary_path, linear_project_id, prev_artifact_issue_id
            )
            if artifact_close_info and artifact_close_info.get("project_update_id"):
                for entry in reversed(project.get("state_history", [])):
                    if entry.get("state") == state:
                        entry["project_update_id"] = artifact_close_info["project_update_id"]
                        break

    previous_history = copy.deepcopy(project.get("state_history", []))
    previous_state = project["state"]
    project["state"] = target
    history = list(project.get("state_history", []))
    target_info = get_state_info(sm, target)
    new_history_entry = {
        "state": target,
        "entered_at": now_iso(),
        "from_state": state,
        "actor": history_actor_value(audit, expected_actor=target_info.get("actor", "unknown")),
        "expected_actor": target_info.get("actor", "unknown"),
        "recorded_by": audit,
    }

    artifact_issue_id = None
    if target in APPROVAL_GATE_STATES and linear_project_id:
        artifact_issue_id = create_artifact_issue(name, target, summary_path, linear_project_id)
        if artifact_issue_id:
            new_history_entry["artifact_issue_id"] = artifact_issue_id

    history.append(new_history_entry)
    project["state_history"] = history
    save_projects(projects)

    linear_state = target_info.get("linear_state", "Unknown")
    linear_sync_required = sm.get("enforcement", {}).get("require_linear_sync", True)
    linear_sync = None
    if linear_project_id:
        linear_sync = sync_linear_project_state(linear_project_id, linear_state)
        if linear_sync_required and not linear_sync.get("synced"):
            project["state"] = previous_state
            project["state_history"] = previous_history
            save_projects(projects)
            result = {
                "ok": False,
                "project": name,
                "previous_state": state,
                "attempted_state": target,
                "linear_state": linear_state,
                "error": "Linear project status sync failed. Transition rolled back.",
                "linear_sync": linear_sync,
                "transition_preconditions": readiness["preconditions"],
                "next_artifacts": target_info.get("artifacts", []),
            }
            if artifact_issue_id:
                result["artifact_issue_id"] = artifact_issue_id
            if artifact_close_info:
                result["artifact_close"] = artifact_close_info
            print(json.dumps(result, indent=2))
            return 1

    result = {
        "ok": True,
        "project": name,
        "previous_state": state,
        "new_state": target,
        "linear_state": linear_state,
        "actor": target_info.get("actor"),
        "approval_gate": target_info.get("approval_gate", False),
        "approval_by": target_info.get("approval_by"),
        "exit_criteria": target_info.get("exit_criteria", []),
        "transition_preconditions": readiness["preconditions"],
        "next_artifacts": target_info.get("artifacts", []),
        "audit": audit,
    }
    if linear_sync:
        result["linear_sync"] = linear_sync
    if artifact_issue_id:
        result["artifact_issue_id"] = artifact_issue_id
    if artifact_close_info:
        result["artifact_close"] = artifact_close_info

    print(json.dumps(result, indent=2))
    return 0


def cmd_plan(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = project.get("state", "UNKNOWN")
    tier = project.get("tier", "feature")
    tier_def = sm["tiers"].get(tier, {})
    all_states = tier_def.get("states", [])

    roadmap = []
    current_idx = all_states.index(state) if state in all_states else -1
    for i, roadmap_state in enumerate(all_states):
        info = get_state_info(sm, roadmap_state)
        if i < current_idx:
            status = "completed"
        elif i == current_idx:
            status = "current"
        else:
            status = "upcoming"
        roadmap.append({
            "state": roadmap_state,
            "status": status,
            "linear_state": info.get("linear_state") if info else None,
            "actor": info.get("actor") if info else None,
            "approval_gate": info.get("approval_gate", False) if info else False,
        })

    readiness = evaluate_transition_readiness(sm, name, project, state)
    state_info = get_state_info(sm, state)

    print(json.dumps({
        "ok": True,
        "project": name,
        "tier": tier,
        "tier_description": tier_def.get("description", ""),
        "current_state": state,
        "roadmap": roadmap,
        "current_state_detail": {
            "exit_criteria": state_info.get("exit_criteria", []) if state_info else [],
            "artifacts_found": readiness["artifacts"]["found"],
            "artifacts_missing": readiness["artifacts"]["missing"],
            "valid_transitions": get_valid_transitions(sm, tier, state),
            "approval_gate": state_info.get("approval_gate", False) if state_info else False,
            "transition_preconditions": readiness["preconditions"],
            "ready_to_transition": readiness["valid"],
        },
        "state_history": project.get("state_history", []),
    }, indent=2))
    return 0


def cmd_record_receipt(args, sm, projects):
    name = args.name
    project = get_project(projects, name)
    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = args.state or project.get("state")
    if state not in sm.get("states", {}):
        print(json.dumps({"ok": False, "error": f"Unknown state: {state}"}))
        return 1

    try:
        metadata = parse_metadata_json(args.metadata_json)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    if args.note:
        metadata["note"] = args.note
    if args.review_file:
        metadata["review_file"] = args.review_file
    if args.session_label:
        metadata["session_label"] = args.session_label

    status_defaults = {
        "approval": "approved",
        "child": "approved",
        "artifact": "verified",
        "pm_session": "active",
    }
    status = args.status or status_defaults[args.kind]
    audit = capture_audit(args)

    if args.kind in {"approval", "child", "pm_session"}:
        role_defaults = {
            "approval": "operator",
            "pm_session": "pm",
        }
        role = args.role or role_defaults.get(args.kind)
        if not role:
            print(json.dumps({"ok": False, "error": f"--role is required for kind={args.kind}"}))
            return 1

        subject = build_state_artifact_subject(name, project, state)
        if not subject:
            print(json.dumps({
                "ok": False,
                "error": f"Cannot bind {args.kind} receipt - no current artifact subject found for state '{state}'",
                "state": state,
            }, indent=2))
            return 2

        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "receipt_id": sha256_text(canonical_json({
                "project": name,
                "state": state,
                "kind": args.kind,
                "role": role,
                "recorded_at": audit["recorded_at"],
                "artifact_hash": subject["hash"],
            })),
            "kind": args.kind,
            "project": name,
            "state": state,
            "role": role,
            "status": status,
            "recorded_at": audit["recorded_at"],
            "artifact": subject,
            "metadata": metadata,
            "audit": audit,
        }
        path = write_receipt(name, state, subject["hash"], args.kind, role, receipt)

    else:
        if not args.artifact:
            print(json.dumps({"ok": False, "error": "--artifact is required for kind=artifact"}))
            return 1

        subject_payload = {
            "artifact": args.artifact,
            "state": state,
        }
        if args.path:
            evidence_path = Path(args.path)
            if not evidence_path.exists():
                print(json.dumps({"ok": False, "error": f"Evidence path does not exist: {args.path}"}))
                return 1
            subject_payload["path"] = args.path
            subject_payload["sha256"] = sha256_bytes(evidence_path.read_bytes())
        if metadata:
            subject_payload["metadata"] = metadata
        binding_hash = sha256_text(canonical_json(subject_payload))

        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "receipt_id": sha256_text(canonical_json({
                "project": name,
                "state": state,
                "kind": args.kind,
                "artifact": args.artifact,
                "recorded_at": audit["recorded_at"],
                "binding_hash": binding_hash,
            })),
            "kind": args.kind,
            "project": name,
            "state": state,
            "role": args.role or args.artifact,
            "status": status,
            "recorded_at": audit["recorded_at"],
            "subject": subject_payload,
            "metadata": metadata,
            "audit": audit,
        }
        path = write_receipt(name, state, binding_hash, args.kind, args.artifact, receipt)

    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "receipt": summarize_receipt(receipt),
        "receipt_path": try_relative_to_workspace(path),
    }, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Project Orchestrator - State Machine Engine")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    p_init = subparsers.add_parser("init", help="Initialize a new project")
    p_init.add_argument("name", help="Project name (key in PROJECTS.yaml)")
    p_init.add_argument("--tier", required=True, choices=["patch", "feature", "project"], help="Project tier")
    p_init.add_argument("--display-name", default=None, help="Human-readable name for Linear project")
    p_init.add_argument("--description", default=None, help="Short description for Linear project")
    add_audit_args(p_init)

    p_status = subparsers.add_parser("status", help="Show current state")
    p_status.add_argument("name", help="Project name")
    p_status.add_argument("--verbose", action="store_true", help="Include review-loop and child-task state summaries when available")

    p_trans = subparsers.add_parser("transition", help="Execute state transition")
    p_trans.add_argument("name", help="Project name")
    p_trans.add_argument("target_state", help="Target state to transition to")
    add_audit_args(p_trans)

    p_val = subparsers.add_parser("validate", help="Validate artifacts and transition preconditions for current state")
    p_val.add_argument("name", help="Project name")

    p_plan = subparsers.add_parser("plan", help="Show full roadmap and history")
    p_plan.add_argument("name", help="Project name")

    p_review = subparsers.add_parser("review-status", help="Check structured review status for the current state")
    p_review.add_argument("name", help="Project name")

    p_review_loop = subparsers.add_parser("record-review-loop", help="Persist canonical review-loop state for the current stage")
    p_review_loop.add_argument("name", help="Project name")
    p_review_loop.add_argument("--state", default=None, help="State to bind the review loop to (defaults to current state)")
    p_review_loop.add_argument("--current-round", type=int, default=None, help="Current critic round number")
    p_review_loop.add_argument("--max-rounds", type=int, default=None, help="Maximum allowed critic rounds")
    p_review_loop.add_argument("--decision", default=None, help="Post-cap decision: FREEZE_AND_ESCALATE, APPROVE, or CANCEL")
    p_review_loop.add_argument("--freeze-required", action="store_true", help="Mark that freeze is required at the current stage")
    p_review_loop.add_argument("--checkpoint-summary", default=None, help="Short operator checkpoint summary")
    p_review_loop.add_argument("--checkpoint-file", default=None, help="Path to operator checkpoint artifact")
    p_review_loop.add_argument("--override", action="store_true", help="Mark an explicit override active")
    p_review_loop.add_argument("--override-reason", default=None, help="Reason for allowing more rounds")
    p_review_loop.add_argument("--unresolved-issues-json", default=None, help="JSON array of unresolved issues")
    p_review_loop.add_argument("--accepted-risks-json", default=None, help="JSON array of accepted risks")
    p_review_loop.add_argument("--carry-forward-json", default=None, help="JSON array of carry-forward items")
    p_review_loop.add_argument("--note", default=None, help="Free-form note stored with the review-loop state")
    add_audit_args(p_review_loop)

    p_review_loop_decision = subparsers.add_parser("record-review-loop-decision", help="Persist a post-cap decision on the canonical review-loop state")
    p_review_loop_decision.add_argument("name", help="Project name")
    p_review_loop_decision.add_argument("--state", default=None, help="State to bind the review loop to (defaults to current state)")
    p_review_loop_decision.add_argument("--current-round", type=int, default=None, help="Current critic round number")
    p_review_loop_decision.add_argument("--max-rounds", type=int, default=None, help="Maximum allowed critic rounds")
    p_review_loop_decision.add_argument("--decision", required=True, help="Post-cap decision: FREEZE_AND_ESCALATE, APPROVE, or CANCEL")
    p_review_loop_decision.add_argument("--freeze-required", action="store_true", help="Mark that freeze is required at the current stage")
    p_review_loop_decision.add_argument("--checkpoint-summary", default=None, help="Short operator checkpoint summary")
    p_review_loop_decision.add_argument("--checkpoint-file", default=None, help="Path to operator checkpoint artifact")
    p_review_loop_decision.add_argument("--override", action="store_true", help="Mark an explicit override active")
    p_review_loop_decision.add_argument("--override-reason", default=None, help="Reason for allowing more rounds")
    p_review_loop_decision.add_argument("--unresolved-issues-json", default=None, help="JSON array of unresolved issues")
    p_review_loop_decision.add_argument("--accepted-risks-json", default=None, help="JSON array of accepted risks")
    p_review_loop_decision.add_argument("--carry-forward-json", default=None, help="JSON array of carry-forward items")
    p_review_loop_decision.add_argument("--note", default=None, help="Free-form note stored with the review-loop state")
    add_audit_args(p_review_loop_decision)

    p_backfill_review_loop = subparsers.add_parser("backfill-review-loop", help="Persist canonical review-loop state inferred from existing review round files")
    p_backfill_review_loop.add_argument("name", help="Project name")
    p_backfill_review_loop.add_argument("--state", default=None, help="State to bind the backfilled review loop to (defaults to current state)")
    p_backfill_review_loop.add_argument("--current-round", type=int, default=None, help="Override inferred current round number")
    p_backfill_review_loop.add_argument("--max-rounds", type=int, default=None, help="Override inferred maximum rounds")
    p_backfill_review_loop.add_argument("--checkpoint-summary", default=None, help="Optional checkpoint summary for the backfilled state")
    p_backfill_review_loop.add_argument("--checkpoint-file", default=None, help="Optional checkpoint file path for the backfilled state")
    p_backfill_review_loop.add_argument("--note", default=None, help="Optional note stored with the backfilled review-loop state")
    add_audit_args(p_backfill_review_loop)

    p_freeze = subparsers.add_parser("record-freeze-artifact", help="Persist structured freeze or carry-forward artifact data for the current stage")
    p_freeze.add_argument("name", help="Project name")
    p_freeze.add_argument("--state", default=None, help="State to bind the freeze artifact to (defaults to current state)")
    p_freeze.add_argument("--summary", default=None, help="Short freeze artifact summary")
    p_freeze.add_argument("--rationale", default=None, help="Why the artifact is being frozen or escalated")
    p_freeze.add_argument("--checkpoint-file", default=None, help="Operator checkpoint or review file linked to the freeze artifact")
    p_freeze.add_argument("--unresolved-issues-json", default=None, help="JSON array of unresolved issues")
    p_freeze.add_argument("--accepted-risks-json", default=None, help="JSON array of accepted risks")
    p_freeze.add_argument("--carry-forward-json", default=None, help="JSON array of carry-forward items")
    p_freeze.add_argument("--output", default=None, help="Optional path for the generated freeze-artifact markdown")
    add_audit_args(p_freeze)

    p_checkpoint = subparsers.add_parser("record-review-checkpoint", help="Write an operator checkpoint artifact and sync canonical review-loop state")
    p_checkpoint.add_argument("name", help="Project name")
    p_checkpoint.add_argument("--state", default=None, help="State to bind the checkpoint to (defaults to current state)")
    p_checkpoint.add_argument("--current-round", type=int, default=None, help="Current critic round number")
    p_checkpoint.add_argument("--max-rounds", type=int, default=None, help="Maximum allowed critic rounds")
    p_checkpoint.add_argument("--summary", default=None, help="Short checkpoint summary for the operator")
    p_checkpoint.add_argument("--producer-response-status", default=None, help="Producer response status, for example addressed or pending")
    p_checkpoint.add_argument("--decision", default=None, help="Post-cap decision: FREEZE_AND_ESCALATE, APPROVE, or CANCEL")
    p_checkpoint.add_argument("--freeze-required", action="store_true", help="Mark that freeze is required at the current stage")
    p_checkpoint.add_argument("--override", action="store_true", help="Mark an explicit override active")
    p_checkpoint.add_argument("--override-reason", default=None, help="Reason for allowing more rounds")
    p_checkpoint.add_argument("--unresolved-issues-json", default=None, help="JSON array of unresolved issues")
    p_checkpoint.add_argument("--accepted-risks-json", default=None, help="JSON array of accepted risks")
    p_checkpoint.add_argument("--carry-forward-json", default=None, help="JSON array of carry-forward items")
    p_checkpoint.add_argument("--note", default=None, help="Free-form note stored with the review-loop state")
    p_checkpoint.add_argument("--output", default=None, help="Optional path for the generated checkpoint markdown")
    add_audit_args(p_checkpoint)

    p_child_task = subparsers.add_parser("record-child-task", help="Persist durable child-task heartbeat/state for the current stage")
    p_child_task.add_argument("name", help="Project name")
    p_child_task.add_argument("--task-id", required=True, help="Stable child task id")
    p_child_task.add_argument("--state", default=None, help="State to bind the child task to (defaults to current state)")
    p_child_task.add_argument("--label", default=None, help="Human-readable child task label")
    p_child_task.add_argument("--kind", default=None, help="Task kind, for example subagent or detached_task")
    p_child_task.add_argument("--owner", default=None, help="Owner role for the child task")
    p_child_task.add_argument("--status", default=None, help="Status: queued, active, blocked, completed, failed, canceled")
    p_child_task.add_argument("--current-step", default=None, help="Current step or phase")
    p_child_task.add_argument("--summary", default=None, help="Compact status summary")
    p_child_task.add_argument("--started-at", default=None, help="ISO timestamp when the child task started")
    p_child_task.add_argument("--heartbeat-at", default=None, help="ISO timestamp of the latest heartbeat")
    p_child_task.add_argument("--blocked-reason", default=None, help="Why the child task is blocked")
    p_child_task.add_argument("--attention-required", action="store_true", help="Flag the child task as needing parent/PM attention")
    p_child_task.add_argument("--session-label", default=None, help="Linked session label, if any")
    p_child_task.add_argument("--metadata-json", default=None, help="JSON object with extra child-task metadata")
    add_audit_args(p_child_task)

    p_child_task_status = subparsers.add_parser("child-task-status", help="Show durable child-task heartbeat/state for the current stage")
    p_child_task_status.add_argument("name", help="Project name")
    p_child_task_status.add_argument("--state", default=None, help="State to inspect (defaults to current state)")
    p_child_task_status.add_argument("--stale-after-minutes", type=int, default=15, help="Heartbeat staleness threshold in minutes")

    p_backfill_child_tasks = subparsers.add_parser("backfill-child-tasks", help="Persist canonical child-task ledger state inferred from child and PM session receipts")
    p_backfill_child_tasks.add_argument("name", help="Project name")
    p_backfill_child_tasks.add_argument("--state", default=None, help="State to bind the backfilled child tasks to (defaults to current state)")
    add_audit_args(p_backfill_child_tasks)

    p_child_task_watchdog = subparsers.add_parser("child-task-watchdog", help="Report child-task exceptions suitable for watchdog or PM escalation")
    p_child_task_watchdog.add_argument("name", help="Project name")
    p_child_task_watchdog.add_argument("--state", default=None, help="State to inspect (defaults to current state)")
    p_child_task_watchdog.add_argument("--stale-after-minutes", type=int, default=15, help="Heartbeat staleness threshold in minutes")
    p_child_task_watchdog.add_argument("--exception-only", action="store_true", help="Return only alert-worthy child-task exceptions")
    p_child_task_watchdog.add_argument("--exit-nonzero-on-alert", action="store_true", help="Exit 2 when alert-worthy child-task exceptions are present")

    p_receipt = subparsers.add_parser("record-receipt", help="Persist a structured receipt")
    p_receipt.add_argument("name", help="Project name")
    p_receipt.add_argument("--kind", required=True, choices=["approval", "child", "artifact", "pm_session"], help="Receipt kind")
    p_receipt.add_argument("--state", default=None, help="State to bind the receipt to (defaults to current state)")
    p_receipt.add_argument("--role", default=None, help="Role for approval/child/pm_session receipts")
    p_receipt.add_argument("--status", default=None, help="Receipt status/decision")
    p_receipt.add_argument("--artifact", default=None, help="Artifact name for kind=artifact receipts")
    p_receipt.add_argument("--path", default=None, help="Evidence path for kind=artifact receipts")
    p_receipt.add_argument("--note", default=None, help="Free-form note stored in receipt metadata")
    p_receipt.add_argument("--review-file", default=None, help="Review file path recorded in receipt metadata")
    p_receipt.add_argument("--session-label", default=None, help="PM session label stored in receipt metadata")
    p_receipt.add_argument("--metadata-json", default=None, help="JSON object merged into receipt metadata")
    add_audit_args(p_receipt)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    sm = load_state_machine()
    projects = load_projects()

    commands = {
        "init": cmd_init,
        "status": cmd_status,
        "transition": cmd_transition,
        "validate": cmd_validate,
        "plan": cmd_plan,
        "review-status": cmd_review_status,
        "record-review-loop": cmd_record_review_loop,
        "record-review-loop-decision": cmd_record_review_loop_decision,
        "backfill-review-loop": cmd_backfill_review_loop,
        "record-freeze-artifact": cmd_record_freeze_artifact,
        "record-review-checkpoint": cmd_record_review_checkpoint,
        "record-child-task": cmd_record_child_task,
        "child-task-status": cmd_child_task_status,
        "backfill-child-tasks": cmd_backfill_child_tasks,
        "child-task-watchdog": cmd_child_task_watchdog,
        "record-receipt": cmd_record_receipt,
    }
    return commands[args.command](args, sm, projects)


if __name__ == "__main__":
    sys.exit(main())
