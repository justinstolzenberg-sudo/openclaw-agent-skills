#!/usr/bin/env python3
"""
Project Orchestrator - Deterministic state machine engine.

Commands:
  init <name> --tier <patch|feature|project>  Initialize a new project
  status <name>                               Show current state + next steps
  transition <name> <target-state>            Validate and execute transition
  validate <name>                             Check artifacts and transition preconditions
  plan <name>                                 Show state history + next steps
  review-status <name>                        Show structured review receipt status
  record-receipt <name> ...                   Persist structured approval/coordination receipts

Exit codes: 0 = success, 1 = invalid transition, 2 = validation/precondition failure
"""

import argparse
import copy
import glob
import hashlib
import json
import re
import os
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
PA_OPS_BOT_ID = os.environ.get("LINEAR_DEFAULT_ASSIGNEE_ID", "")
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_ACCEPTED_STATUSES = {"approved", "verified", "completed", "passed", "done", "merged", "active"}
RECEIPT_REJECTED_STATUSES = {"rejected", "failed", "needs_fixes", "needs_revision", "stale"}
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


def load_json_file(path):
    with open(path, "r") as f:
        return json.load(f)


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
                    exists = "## Plan" in content and bool(re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", content))

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
            required_statuses = [precondition.get("decision", "approved")]
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
            missing_roles = []
            stale_roles = {}
            matched_roles = {}
            if not artifact_hash:
                missing_roles = roles
            else:
                for role in roles:
                    matched, stale = find_state_bound_receipt(
                        project_name, state_name, "child", role, artifact_hash, required_statuses=required_statuses
                    )
                    if matched:
                        matched_roles[role] = summarize_receipt(matched)
                    else:
                        missing_roles.append(role)
                        if stale:
                            stale_roles[role] = summarize_receipt(stale)
            result.update({
                "roles": roles,
                "required_statuses": required_statuses,
                "matched_roles": matched_roles,
                "missing_roles": missing_roles,
                "stale_roles": stale_roles,
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

    print(json.dumps({
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
    }, indent=2))
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
    child_precondition = next((p for p in readiness["preconditions"] if p["type"] == "child_receipts"), None)

    review_files = []
    if child_precondition:
        for receipt in child_precondition.get("matched_roles", {}).values():
            metadata = receipt.get("metadata", {}) or {}
            review_file = metadata.get("review_file")
            if review_file and review_file not in review_files:
                review_files.append(review_file)

    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "tier": tier,
        "valid": readiness["valid"],
        "exit_criteria": state_info.get("exit_criteria", []),
        "artifacts": readiness["artifacts"],
        "artifact_subject": readiness["artifact_subject"],
        "transition_preconditions": readiness["preconditions"],
        "inter_agent_review": {
            "required": state_info.get("inter_agent_review", False),
            "signed_off": bool(child_precondition and len(child_precondition.get("missing_roles", [])) == 0),
            "pm_signed_off": bool(child_precondition and "pm" in child_precondition.get("matched_roles", {})),
            "review_files": review_files,
            "issues": [
                p for p in readiness["preconditions"] if not p.get("satisfied")
            ],
        },
        "ready_to_transition": readiness["valid"],
    }, indent=2))
    return 0 if readiness["valid"] else 2


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
    child_precondition = next((p for p in readiness["preconditions"] if p["type"] == "child_receipts"), None)
    pm_session_precondition = next((p for p in readiness["preconditions"] if p["type"] == "pm_session_receipt"), None)

    review_files = []
    matched_roles = child_precondition.get("matched_roles", {}) if child_precondition else {}
    for receipt in matched_roles.values():
        metadata = receipt.get("metadata", {}) or {}
        review_file = metadata.get("review_file")
        if review_file and review_file not in review_files:
            review_files.append(review_file)

    result = {
        "ok": True,
        "project": name,
        "state": state,
        "inter_agent_review_required": state_info.get("inter_agent_review", False),
        "producer_role": state_info.get("producer_role"),
        "critic_role": state_info.get("critic_role"),
        "pm_signoff_required": state_info.get("pm_signoff_required", False),
        "review_files": review_files,
        "signed_off": bool(child_precondition and len(child_precondition.get("missing_roles", [])) == 0),
        "pm_signed_off": bool(child_precondition and "pm" in child_precondition.get("matched_roles", {})),
        "child_receipts": child_precondition,
        "pm_session_receipt": pm_session_precondition,
        "artifact_subject": readiness.get("artifact_subject"),
    }
    print(json.dumps(result, indent=2))
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
        "record-receipt": cmd_record_receipt,
    }
    return commands[args.command](args, sm, projects)


if __name__ == "__main__":
    sys.exit(main())
