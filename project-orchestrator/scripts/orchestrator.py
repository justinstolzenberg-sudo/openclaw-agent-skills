#!/usr/bin/env python3
"""
Project Orchestrator - Deterministic state machine engine.

Commands:
  init <name> --tier <patch|feature|project>  Initialize a new project
  status <name>                               Show current state + next steps
  transition <name> <target-state>            Validate and execute transition
  validate <name>                             Check artifacts for current state
  plan <name>                                 Show state history + next steps

Exit codes: 0 = success, 1 = invalid transition, 2 = missing artifacts
"""

import argparse
import json
import os
import subprocess
import sys
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
# Workspace detection: env var > walk up looking for PROJECTS.yaml > fallback to skill dir parent.parent
def _find_workspace_dir():
    env_ws = os.environ.get("OPENCLAW_WORKSPACE")
    if env_ws:
        return Path(env_ws)
    # Walk up from cwd looking for PROJECTS.yaml
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "PROJECTS.yaml").exists():
            return candidate
    # Fallback: resolve symlink manually if SCRIPT_DIR is under a symlinked path
    # Check if the script is in a symlinked skill dir inside a workspace
    for candidate in [SCRIPT_DIR.parent.parent, Path.home() / ".openclaw" / "workspace"]:
        if (candidate / "PROJECTS.yaml").exists():
            return candidate
    return SCRIPT_DIR.parent.parent

WORKSPACE_DIR = _find_workspace_dir()
PROJECTS_YAML_PATH = WORKSPACE_DIR / "PROJECTS.yaml"
PROJECTS_DIR = WORKSPACE_DIR / "projects"


def load_yaml(path):
    """Load a YAML file. Falls back to treating as JSON if PyYAML unavailable."""
    with open(path, "r") as f:
        if HAS_YAML:
            return yaml.safe_load(f)
        else:
            return json.load(f)


def save_yaml(path, data):
    """Save data to YAML file. Falls back to JSON if PyYAML unavailable."""
    with open(path, "w") as f:
        if HAS_YAML:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        else:
            json.dump(data, f, indent=2)


def load_state_machine():
    """Load the state machine configuration."""
    return load_yaml(STATE_MACHINE_PATH)


def load_projects():
    """Load PROJECTS.yaml."""
    return load_yaml(PROJECTS_YAML_PATH)


def save_projects(data):
    """Save PROJECTS.yaml."""
    save_yaml(PROJECTS_YAML_PATH, data)


def get_project(projects, name):
    """Get a project by name from PROJECTS.yaml."""
    prjs = projects.get("projects", {})
    if name not in prjs:
        return None
    return prjs[name]


def get_valid_transitions(sm, tier, current_state):
    """Get all valid target states from the current state for a given tier."""
    tier_states = sm["tiers"].get(tier, {}).get("states", [])
    transitions = []
    for t in sm["transitions"]:
        if t["from"] == current_state and t["to"] in tier_states:
            transitions.append({"to": t["to"], "condition": t["condition"]})
    return transitions


def get_state_info(sm, state_name):
    """Get state definition from state machine config."""
    return sm["states"].get(state_name)


def check_summary_section(project, section_name):
    """Check if a section exists in the project summary file."""
    summary_path = project.get("summary")
    if not summary_path:
        return False
    full_path = WORKSPACE_DIR / summary_path
    if not full_path.exists():
        return False
    content = full_path.read_text()
    return f"## {section_name}" in content


def check_artifacts(sm, project, state_name):
    """Check which artifacts exist for a given state. Returns (found, missing)."""
    state_def = get_state_info(sm, state_name)
    if not state_def:
        return [], []

    artifacts = state_def.get("artifacts", [])
    found = []
    missing = []

    for artifact in artifacts:
        exists = False

        if artifact == "projects_yaml_entry":
            # If we got this far, the project exists in PROJECTS.yaml
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
            # Check if plan section has MET- issue references
            summary_path = project.get("summary")
            if summary_path:
                full_path = WORKSPACE_DIR / summary_path
                if full_path.exists():
                    content = full_path.read_text()
                    exists = "MET-" in content and "## Plan" in content

        elif artifact == "design_spec_json":
            # Check if a design spec JSON exists for this project
            import glob
            name = project.get("_name", "")
            exists = bool(glob.glob(f"/tmp/*{name}*design*/design-spec.json") or
                         glob.glob(f"/tmp/*design*/design-spec.json"))

        elif artifact == "wireframe_svgs":
            import glob
            name = project.get("_name", "")
            exists = bool(glob.glob(f"/tmp/*{name}*design*/wireframes/*.svg") or
                         glob.glob(f"/tmp/*design*/wireframes/*.svg"))

        elif artifact == "design_critique_report":
            # Critique is embedded in the design spec JSON
            exists = True  # If design_spec_json exists, critique is included

        elif artifact in ("api_schemas_verified", "api_schema_section"):
            exists = check_summary_section(project, "Verified API Schemas")

        elif artifact in ("human_e2e_passed", "human_e2e_section"):
            exists = check_summary_section(project, "Human E2E Test Report")

        elif artifact in ("code_on_branch", "test_results", "pr_with_review",
                          "review_summary", "merged_pr", "retrospective_note",
                          "test_verification_report", "linear_audit_report"):
            # These require external verification - can't check deterministically
            # Mark as unknown (treat as missing for validation purposes)
            exists = False

        elif artifact == "updated_projects_yaml":
            exists = True  # We're always updating it

        if exists:
            found.append(artifact)
        else:
            missing.append(artifact)

    return found, missing


def now_iso():
    """Current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Artifact issue helpers ---

APPROVAL_GATE_STATES = {"BRIEF", "DESIGN", "ARCHITECTURE", "PLAN", "REVIEW"}
SECTION_MAP = {
    "BRIEF": "## Brief",
    "DESIGN": "## Design",
    "ARCHITECTURE": "## Architecture",
    "PLAN": "## Plan",
    "REVIEW": "## Review",
}
PA_OPS_BOT_ID = os.environ.get("LINEAR_DEFAULT_ASSIGNEE_ID", "")


def extract_section(summary_path, section_header):
    """Extract a section from a markdown file by header."""
    full_path = WORKSPACE_DIR / summary_path
    if not full_path.exists():
        return None
    text = full_path.read_text()
    start = text.find(section_header)
    if start == -1:
        return None
    # Find next ## header or end of file
    rest = text[start + len(section_header):]
    next_header = rest.find("\n## ")
    if next_header == -1:
        return text[start:]
    return text[start:start + len(section_header) + next_header]


def _build_design_artifact_content(project_name):
    """Build design artifact content from the design spec JSON output directory.
    
    Looks for the design spec in the standard output locations and builds
    a summary with screen inventory, critique highlights, and wireframe references.
    """
    import glob

    # Search for design spec in common output locations
    search_paths = [
        f"/tmp/{project_name}-design/design-spec.json",
        f"/tmp/{project_name.replace('-', '_')}-design/design-spec.json",
        f"/tmp/design-{project_name}/design-spec.json",
    ]
    # Also try workspace projects dir
    projects_dir = WORKSPACE_DIR / "projects"
    search_paths += glob.glob(str(projects_dir / f"{project_name}*design*.json"))
    search_paths += glob.glob("/tmp/*design*/design-spec.json")

    spec = None
    spec_path = None
    for p in search_paths:
        try:
            with open(p) as f:
                spec = json.load(f)
            spec_path = p
            break
        except (FileNotFoundError, json.JSONDecodeError):
            continue

    if not spec:
        return None

    # Build summary
    screens = spec.get("screens", [])
    critiques = spec.get("design_notes", [])
    edge_cases = spec.get("edge_cases", [])
    user_stories = spec.get("user_stories", [])
    flow_map = spec.get("flow_map", [])
    metadata = spec.get("metadata", {})

    lines = [
        f"# DESIGN Artifact: {project_name}",
        f"",
        f"**Model:** {metadata.get('model', 'unknown')}",
        f"**Generated:** {metadata.get('timestamp', 'unknown')}",
        f"**Steps:** {', '.join(metadata.get('steps_completed', []))}",
        f"",
        f"## Screen Inventory ({len(screens)} screens)",
        f"",
        f"| # | Screen ID | Title | Components |",
        f"|---|-----------|-------|------------|",
    ]
    for i, s in enumerate(screens, 1):
        comps = len(s.get("components", []))
        lines.append(f"| {i} | `{s.get('screen_id', '')}` | {s.get('title', '')} | {comps} |")

    lines += [
        f"",
        f"## Design Critique ({len(critiques)} issues)",
        f"",
        metadata.get("critique_summary", "")[:2000],
        f"",
    ]

    # Count by severity
    by_sev = {}
    for c in critiques:
        sev = c.get("severity", "unknown")
        by_sev[sev] = by_sev.get(sev, 0) + 1
    for sev, count in sorted(by_sev.items()):
        lines.append(f"- **{sev}:** {count}")

    lines += [
        f"",
        f"## Flow & Edge Cases",
        f"- **Flow transitions:** {len(flow_map)}",
        f"- **User stories:** {len(user_stories)}",
        f"- **Edge cases:** {len(edge_cases)}",
    ]

    # Wireframe references
    wireframe_dir = Path(spec_path).parent / "wireframes"
    if wireframe_dir.exists():
        svgs = sorted(wireframe_dir.glob("*.svg"))
        if svgs:
            lines += [f"", f"## Wireframes ({len(svgs)} rendered)"]
            for svg in svgs:
                lines.append(f"- `{svg.name}`")

    return "\n".join(lines)


def create_artifact_issue(project_name, state, summary_path, linear_project_id):
    """Create a Linear issue for the current stage artifact. Returns issue_id or None."""
    section_header = SECTION_MAP.get(state)
    if not section_header or not summary_path or not linear_project_id:
        return None

    # For DESIGN state, build content from design spec JSON instead of markdown section
    if state == "DESIGN":
        content = _build_design_artifact_content(project_name)
        if not content:
            content = extract_section(summary_path, section_header)
    else:
        content = extract_section(summary_path, section_header)

    short_desc = f"Artifact for {state} stage. Full content posted as comment."

    title = f"[{project_name}] {state} artifact"
    linear_script = SKILL_DIR / "scripts" / "linear_integration.py"
    try:
        result = subprocess.run([
            sys.executable, str(linear_script),
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
            # Post the full artifact content as a comment on the issue
            if issue_id and content:
                try:
                    subprocess.run([
                        sys.executable, str(linear_script),
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
    """Close the artifact issue and post the artifact as a project update.
    
    Returns dict with close/update results or None.
    """
    linear_script = SKILL_DIR / "scripts" / "linear_integration.py"
    result_info = {}

    # Close the artifact issue
    if artifact_issue_id:
        try:
            subprocess.run([
                sys.executable, str(linear_script),
                "update-state",
                "--issue-id", artifact_issue_id,
                "--state", "Done",
            ], capture_output=True, text=True, timeout=30)
            result_info["issue_closed"] = True
        except Exception:
            result_info["issue_closed"] = False

    # Update project description (short summary, max 255 chars) and post full artifact as project update
    section_header = SECTION_MAP.get(state)
    if section_header and summary_path and linear_project_id:
        content = extract_section(summary_path, section_header)
        if content:
            # Short description for the project (255 char limit)
            short_desc = f"{state} stage approved. See latest project update for details."
            try:
                subprocess.run([
                    sys.executable, str(linear_script),
                    "update-project-description",
                    "--project-id", linear_project_id,
                    "--body", short_desc[:255],
                ], capture_output=True, text=True, timeout=30)
            except Exception:
                pass  # Best effort

            # Full content as project update
            update_body = f"# {state} Stage Approved\n\n{content}"
            try:
                r = subprocess.run([
                    sys.executable, str(linear_script),
                    "post-project-update",
                    "--project-id", linear_project_id,
                    "--body", update_body[:50000],
                ], capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    data = json.loads(r.stdout.strip())
                    result_info["project_update_id"] = data.get("update_id")
                else:
                    result_info["project_update_error"] = r.stderr.strip() or r.stdout.strip()
            except Exception as e:
                result_info["project_update_error"] = str(e)

    return result_info if result_info else None


# --- Commands ---

def cmd_init(args, sm, projects):
    """Initialize a new project."""
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
    initial_state = tier_def["states"][0]  # INTAKE

    # Create project entry
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
            "actor": "pa"
        }],
        "linear_project_id": None
    }
    projects["projects"] = prjs
    save_projects(projects)

    # Create summary file stub
    summary_path = PROJECTS_DIR / f"{name}.md"
    if not summary_path.exists():
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(f"# {name}\n\n**Tier:** {tier}\n**State:** {initial_state}\n")

    # Auto-create Linear project via linear_integration.py
    linear_project_id = None
    linear_url = None
    linear_error = None
    linear_script = SKILL_DIR / "scripts" / "linear_integration.py"
    if linear_script.exists():
        try:
            import subprocess
            display_name = args.display_name or name.replace("-", " ").title()
            cmd = [
                sys.executable, str(linear_script),
                "create-project",
                "--name", display_name,
                "--description", args.description or f"[{tier}] {name}",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout.strip())
                linear_project_id = data.get("project_id")
                linear_url = data.get("url")
                # Save linear_project_id back to PROJECTS.yaml
                projects_reloaded = load_projects()
                projects_reloaded["projects"][name]["linear_project_id"] = linear_project_id
                save_projects(projects_reloaded)
            else:
                linear_error = result.stderr.strip() or result.stdout.strip()
        except Exception as e:
            linear_error = str(e)

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
            f"Transition to {tier_def['states'][1]} when exit criteria met"
        ]
    }, indent=2))
    return 0


def cmd_status(args, sm, projects):
    """Show current state and what's needed to transition."""
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

    found, missing = check_artifacts(sm, project, state)
    valid_transitions = get_valid_transitions(sm, tier, state)

    # Determine Linear state
    linear_state = state_info.get("linear_state", "Unknown")

    print(json.dumps({
        "ok": True,
        "project": name,
        "tier": tier,
        "state": state,
        "linear_state": linear_state,
        "actor": state_info.get("actor"),
        "approval_gate": state_info.get("approval_gate", False),
        "approval_by": state_info.get("approval_by"),
        "exit_criteria": state_info.get("exit_criteria", []),
        "artifacts": {
            "found": found,
            "missing": missing
        },
        "valid_transitions": valid_transitions,
        "state_history": project.get("state_history", [])
    }, indent=2))
    return 0


def cmd_transition(args, sm, projects):
    """Validate and execute a state transition."""
    name = args.name
    target = args.target_state
    project = get_project(projects, name)

    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = project.get("state", "UNKNOWN")
    tier = project.get("tier", "feature")

    # Check target state exists
    if target not in sm["states"] and target != "CANCELED":
        print(json.dumps({
            "ok": False,
            "error": f"Unknown target state: {target}",
            "valid_states": list(sm["states"].keys())
        }))
        return 1

    # Handle CANCELED - any state can go to canceled
    if target == "CANCELED":
        project["state"] = "CANCELED"
        history = project.get("state_history", [])
        history.append({
            "state": "CANCELED",
            "entered_at": now_iso(),
            "from_state": state,
            "actor": "operator"
        })
        project["state_history"] = history
        save_projects(projects)
        print(json.dumps({
            "ok": True,
            "project": name,
            "previous_state": state,
            "new_state": "CANCELED",
            "linear_state": "Canceled"
        }, indent=2))
        return 0

    # Check target is valid for tier
    tier_states = sm["tiers"].get(tier, {}).get("states", [])
    if target not in tier_states:
        print(json.dumps({
            "ok": False,
            "error": f"State '{target}' is not valid for tier '{tier}'",
            "valid_states_for_tier": tier_states
        }))
        return 1

    # Check transition exists
    valid = get_valid_transitions(sm, tier, state)
    target_transitions = [t for t in valid if t["to"] == target]

    if not target_transitions:
        print(json.dumps({
            "ok": False,
            "error": f"No valid transition from '{state}' to '{target}'",
            "valid_transitions": valid
        }))
        return 1

    # Check exit criteria of current state
    found, missing = check_artifacts(sm, project, state)
    if missing:
        # Warn about missing artifacts but allow transition
        # The PA decides whether to proceed
        artifact_warning = missing
    else:
        artifact_warning = []

    # --- Pre-transition: close previous approval-gate artifact ---
    linear_project_id = project.get("linear_project_id")
    summary_path = project.get("summary")
    artifact_close_info = None

    if state in APPROVAL_GATE_STATES and linear_project_id:
        # Find the artifact_issue_id from the state_history entry for the current state
        prev_artifact_issue_id = None
        for entry in reversed(project.get("state_history", [])):
            if entry.get("state") == state and entry.get("artifact_issue_id"):
                prev_artifact_issue_id = entry["artifact_issue_id"]
                break

        if prev_artifact_issue_id:
            artifact_close_info = close_artifact_and_post_update(
                name, state, summary_path, linear_project_id, prev_artifact_issue_id
            )
            # Store project_update_id in the outgoing state's history entry
            if artifact_close_info and artifact_close_info.get("project_update_id"):
                for entry in reversed(project.get("state_history", [])):
                    if entry.get("state") == state:
                        entry["project_update_id"] = artifact_close_info["project_update_id"]
                        break

    # Execute transition
    project["state"] = target
    history = project.get("state_history", [])
    new_history_entry = {
        "state": target,
        "entered_at": now_iso(),
        "from_state": state,
        "actor": sm["states"].get(target, {}).get("actor", "unknown")
    }

    # --- Post-transition: create artifact issue for new approval-gate state ---
    artifact_issue_id = None
    if target in APPROVAL_GATE_STATES and linear_project_id:
        artifact_issue_id = create_artifact_issue(
            name, target, summary_path, linear_project_id
        )
        if artifact_issue_id:
            new_history_entry["artifact_issue_id"] = artifact_issue_id

    history.append(new_history_entry)
    project["state_history"] = history
    save_projects(projects)

    target_info = get_state_info(sm, target)
    linear_state = target_info.get("linear_state", "Unknown")

    # Sync Linear project state if linear_project_id is set
    linear_sync = None
    if linear_project_id:
        linear_sync = sync_linear_project_state(linear_project_id, linear_state)

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
        "artifact_warnings": artifact_warning,
        "next_artifacts": target_info.get("artifacts", [])
    }
    if linear_sync:
        result["linear_sync"] = linear_sync
    if artifact_issue_id:
        result["artifact_issue_id"] = artifact_issue_id
    if artifact_close_info:
        result["artifact_close"] = artifact_close_info

    print(json.dumps(result, indent=2))
    return 0


def sync_linear_project_state(project_id, linear_state):
    """Sync Linear project status to match the framework state.
    
    Uses linear_integration.py if available, falls back to direct API call.
    Returns sync result dict or None on failure.
    """
    linear_script = SKILL_DIR / "scripts" / "linear_integration.py"
    
    # Map our linear states to Linear project status names
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
        import subprocess
        # Try using linear_integration.py for issue state sync
        # For project-level status, use direct API call
        env = os.environ.copy()
        
        # Get token from env or temp token file
        token = env.get("LINEAR_TOKEN") or env.get("LINEAR_API_TOKEN")
        if not token:
            try:
                with open("/tmp/.linear_token") as f:
                    token = f.read().strip()
            except (FileNotFoundError, PermissionError):
                pass
        if not token:
            return {"synced": False, "error": "No LINEAR_TOKEN available"}
        
        # Update project status via GraphQL
        import json as _json
        import urllib.request
        
        # First get available statuses
        query_statuses = '{ projectStatuses { nodes { id name } } }'
        req = urllib.request.Request(
            'https://api.linear.app/graphql',
            data=_json.dumps({"query": query_statuses}).encode(),
            headers={'Authorization': token, 'Content-Type': 'application/json'}
        )
        resp = _json.loads(urllib.request.urlopen(req, timeout=10).read())
        statuses = {s["name"]: s["id"] for s in resp.get("data", {}).get("projectStatuses", {}).get("nodes", [])}
        
        status_id = statuses.get(project_status)
        if not status_id:
            return {"synced": False, "error": f"Status '{project_status}' not found in Linear"}
        
        # Update project
        query_update = """mutation($id: String!, $statusId: String!) {
            projectUpdate(id: $id, input: { statusId: $statusId }) { success }
        }"""
        req = urllib.request.Request(
            'https://api.linear.app/graphql',
            data=_json.dumps({"query": query_update, "variables": {"id": project_id, "statusId": status_id}}).encode(),
            headers={'Authorization': token, 'Content-Type': 'application/json'}
        )
        resp = _json.loads(urllib.request.urlopen(req, timeout=10).read())
        success = resp.get("data", {}).get("projectUpdate", {}).get("success", False)
        
        return {"synced": success, "project_status": project_status, "linear_state": linear_state}
    
    except Exception as e:
        return {"synced": False, "error": str(e)}


def cmd_validate(args, sm, projects):
    """Check if all artifacts exist for current state."""
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

    found, missing = check_artifacts(sm, project, state)
    exit_criteria = state_info.get("exit_criteria", [])

    all_clear = len(missing) == 0

    # Check inter-agent review sign-offs if required
    inter_agent_review_required = state_info.get("inter_agent_review", False)
    inter_agent_signed_off = True
    pm_signed_off = True
    review_files = []
    inter_agent_issues = []

    if inter_agent_review_required:
        stage = state.lower()
        review_pattern = f"{name}-review-{stage}-"
        if PROJECTS_DIR.exists():
            for f in PROJECTS_DIR.iterdir():
                if f.name.startswith(review_pattern) and f.suffix == ".md":
                    review_files.append(str(f.relative_to(WORKSPACE_DIR)))

        if not review_files:
            inter_agent_signed_off = False
            pm_signed_off = False
            inter_agent_issues.append("no_review_files_found")
        else:
            review_files_sorted = sorted(review_files)
            latest_review = WORKSPACE_DIR / review_files_sorted[-1]
            if latest_review.exists():
                content = latest_review.read_text()
                approved_count = content.count("Status:** APPROVED") + content.count("Status: APPROVED")
                inter_agent_signed_off = approved_count >= 2
                pm_approved = ("pa (PM)" in content and "APPROVED" in content)
                pm_signed_off = pm_approved and approved_count >= 3
                if not inter_agent_signed_off:
                    inter_agent_issues.append("producer_or_critic_not_signed_off")
                if not pm_signed_off:
                    inter_agent_issues.append("pm_not_signed_off")

    all_clear = all_clear and inter_agent_signed_off and pm_signed_off

    print(json.dumps({
        "ok": True,
        "project": name,
        "state": state,
        "tier": tier,
        "valid": all_clear,
        "exit_criteria": exit_criteria,
        "artifacts": {
            "found": found,
            "missing": missing
        },
        "inter_agent_review": {
            "required": inter_agent_review_required,
            "signed_off": inter_agent_signed_off,
            "pm_signed_off": pm_signed_off,
            "review_files": review_files,
            "issues": inter_agent_issues
        },
        "ready_to_transition": all_clear
    }, indent=2))
    return 0 if all_clear else 2


def cmd_review_status(args, sm, projects):
    """Check inter-agent review status for the current state."""
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

    inter_agent_review_required = state_info.get("inter_agent_review", False)

    # Find review files: projects/<name>-review-<stage>-*.md
    stage = state.lower()
    review_pattern = f"{name}-review-{stage}-"
    review_files = []
    if PROJECTS_DIR.exists():
        for f in PROJECTS_DIR.iterdir():
            if f.name.startswith(review_pattern) and f.suffix == ".md":
                review_files.append(str(f.relative_to(WORKSPACE_DIR)))

    # Check sign-offs in review files
    signed_off = False
    pm_signed_off = False

    if review_files:
        # Check the most recent review file (highest round number or last alphabetically)
        review_files_sorted = sorted(review_files)
        latest_review = WORKSPACE_DIR / review_files_sorted[-1]
        if latest_review.exists():
            content = latest_review.read_text()
            # Detect producer + critic both approved
            producer_approved = "Status:** APPROVED" in content or "Status: APPROVED" in content
            # PM sign-off: look for pa (PM) approval
            pm_approved = ("pa (PM)" in content and "APPROVED" in content)
            # More robust: count APPROVED occurrences - need at least 2 (producer+critic) for signed_off
            approved_count = content.count("Status:** APPROVED") + content.count("Status: APPROVED")
            signed_off = approved_count >= 2
            pm_signed_off = pm_approved and approved_count >= 3

    result = {
        "ok": True,
        "project": name,
        "state": state,
        "inter_agent_review_required": inter_agent_review_required,
        "producer_role": state_info.get("producer_role"),
        "critic_role": state_info.get("critic_role"),
        "pm_signoff_required": state_info.get("pm_signoff_required", False),
        "review_files": review_files,
        "signed_off": signed_off,
        "pm_signed_off": pm_signed_off
    }
    print(json.dumps(result, indent=2))
    return 0


def cmd_plan(args, sm, projects):
    """Show the full state history and next steps."""
    name = args.name
    project = get_project(projects, name)

    if not project:
        print(json.dumps({"ok": False, "error": f"Project '{name}' not found"}))
        return 1

    state = project.get("state", "UNKNOWN")
    tier = project.get("tier", "feature")
    tier_def = sm["tiers"].get(tier, {})
    all_states = tier_def.get("states", [])

    # Build roadmap: states with their status
    roadmap = []
    current_idx = all_states.index(state) if state in all_states else -1
    for i, s in enumerate(all_states):
        s_info = get_state_info(sm, s)
        if i < current_idx:
            status = "completed"
        elif i == current_idx:
            status = "current"
        else:
            status = "upcoming"
        roadmap.append({
            "state": s,
            "status": status,
            "linear_state": s_info.get("linear_state") if s_info else None,
            "actor": s_info.get("actor") if s_info else None,
            "approval_gate": s_info.get("approval_gate", False) if s_info else False
        })

    # Get current state details
    valid_transitions = get_valid_transitions(sm, tier, state)
    found, missing = check_artifacts(sm, project, state)
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
            "artifacts_found": found,
            "artifacts_missing": missing,
            "valid_transitions": valid_transitions,
            "approval_gate": state_info.get("approval_gate", False) if state_info else False
        },
        "state_history": project.get("state_history", [])
    }, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Project Orchestrator - State Machine Engine")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init
    p_init = subparsers.add_parser("init", help="Initialize a new project")
    p_init.add_argument("name", help="Project name (key in PROJECTS.yaml)")
    p_init.add_argument("--tier", required=True, choices=["patch", "feature", "project"],
                        help="Project tier")
    p_init.add_argument("--display-name", default=None,
                        help="Human-readable name for Linear project (defaults to title-cased name)")
    p_init.add_argument("--description", default=None,
                        help="Short description for Linear project (max 255 chars)")

    # status
    p_status = subparsers.add_parser("status", help="Show current state")
    p_status.add_argument("name", help="Project name")

    # transition
    p_trans = subparsers.add_parser("transition", help="Execute state transition")
    p_trans.add_argument("name", help="Project name")
    p_trans.add_argument("target_state", help="Target state to transition to")

    # validate
    p_val = subparsers.add_parser("validate", help="Validate artifacts for current state")
    p_val.add_argument("name", help="Project name")

    # plan
    p_plan = subparsers.add_parser("plan", help="Show full roadmap and history")
    p_plan.add_argument("name", help="Project name")

    # review-status
    p_review = subparsers.add_parser("review-status", help="Check inter-agent review status for current state")
    p_review.add_argument("name", help="Project name")

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
    }

    return commands[args.command](args, sm, projects)


if __name__ == "__main__":
    sys.exit(main())
