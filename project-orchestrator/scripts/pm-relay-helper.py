#!/usr/bin/env python3
"""PM relay continuity helper for project-orchestrator.

Commands:
  check <project>      - report whether a successor PM run should be spawned now
  sweep                - report all active projects that currently need PM rehydration
  sweep-active         - report only explicitly tracked in-flight projects/stages
  activate <project>   - mark one project as actively tracked for PM relay
  deactivate <project> - remove one project from active PM relay tracking
  list-active          - show explicitly tracked in-flight projects/stages
"""

import argparse
import json
import os
from pathlib import Path

import importlib.util

import orchestrator as orchestrator_lib

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent

_PM_CHECKER_PATH = SCRIPT_DIR / "pm-checker.py"
_pm_spec = importlib.util.spec_from_file_location("project_orchestrator_pm_checker", _PM_CHECKER_PATH)
pm_checker_lib = importlib.util.module_from_spec(_pm_spec)
_pm_spec.loader.exec_module(pm_checker_lib)


def load_state_machine():
    return pm_checker_lib.load_state_machine()


def load_projects():
    return pm_checker_lib.load_projects()


def active_scope_path():
    workspace = Path(os.environ.get("OPENCLAW_WORKSPACE", Path.cwd()))
    return workspace / "projects" / ".orchestrator" / "pm-relay-active.json"


def load_active_scope():
    path = active_scope_path()
    if not path.exists():
        return {"projects": []}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"projects": []}
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return {"projects": []}
    return data


def save_active_scope(data):
    path = active_scope_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


LIVE_STAGE_OWNER_STATUSES = {"queued", "running", "active", "waiting"}
BLOCKING_STAGE_OWNER_STATUSES = {"blocked", "needs_attention"}


def _get_stage_owner_state(project_name, state):
    child_tasks = orchestrator_lib.summarize_child_tasks(project_name, state)
    stale_after_seconds = int(child_tasks.get("stale_after_minutes", 15) * 60)
    for task in child_tasks.get("tasks", []):
        if not pm_checker_lib._is_stage_owner_task(task, project_name, state):
            continue
        status = str(task.get("status") or "").strip().lower()
        stale_seconds = task.get("stale_seconds")
        is_stale = isinstance(stale_seconds, (int, float)) and stale_seconds >= stale_after_seconds
        return {
            "task": task,
            "status": status,
            "is_live": status in LIVE_STAGE_OWNER_STATUSES and not is_stale,
            "is_blocked": status in BLOCKING_STAGE_OWNER_STATUSES,
            "is_stale": is_stale,
            "blocked_reason": task.get("blocked_reason"),
            "attention_required": bool(task.get("attention_required")),
        }
    return None


def evaluate_project(project_name, project, sm):
    state = project.get("state", "UNKNOWN")
    if state in {"CLOSED", "CANCELED"}:
        return {
            "project": project_name,
            "state": state,
            "should_respawn": False,
            "reason": "terminal_state",
        }

    operator_only = pm_checker_lib._is_operator_approval_only_remaining(project, state, sm)
    if operator_only:
        return {
            "project": project_name,
            "state": state,
            "should_respawn": False,
            "reason": "operator_approval_only_remaining",
        }

    stage_owner = _get_stage_owner_state(project_name, state)
    if stage_owner and stage_owner.get("is_live"):
        return {
            "project": project_name,
            "state": state,
            "should_respawn": False,
            "reason": "active_pm_owner_present",
        }

    if stage_owner and stage_owner.get("is_blocked") and stage_owner.get("blocked_reason"):
        return {
            "project": project_name,
            "state": state,
            "should_respawn": False,
            "reason": "real_blocker_present",
            "blocked_reason": stage_owner.get("blocked_reason"),
            "attention_required": stage_owner.get("attention_required", False),
            "stage_owner_stale": stage_owner.get("is_stale", False),
        }

    violations = [
        v.to_dict() for v in pm_checker_lib.check_pm_continuity(project, state, sm)
    ]
    codes = [v.get("code") for v in violations]
    should_respawn = any(code in {"PM_OWNER_MISSING", "PM_OWNER_ENDED_BEFORE_STAGE_EXIT"} for code in codes)

    result = {
        "project": project_name,
        "state": state,
        "should_respawn": should_respawn,
        "reason": "pm_continuity_violation" if should_respawn else "no_respawn_needed",
        "violations": violations,
        "next_label": f"pm-{project_name}-{state.lower()}",
    }
    return result


def cmd_check(args):
    sm = load_state_machine()
    projects = load_projects().get("projects", {})
    if args.name not in projects:
        print(json.dumps({"ok": False, "error": f"Project '{args.name}' not found"}, indent=2))
        return 1
    project = dict(projects[args.name])
    project["_name"] = args.name
    result = evaluate_project(args.name, project, sm)
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0


def cmd_sweep(args):
    sm = load_state_machine()
    projects = load_projects().get("projects", {})
    results = []
    for name, project in sorted(projects.items()):
        project_payload = dict(project)
        project_payload["_name"] = name
        results.append(evaluate_project(name, project_payload, sm))
    respawn = [r for r in results if r.get("should_respawn")]
    print(json.dumps({
        "ok": True,
        "scope": "all_projects",
        "projects_checked": len(results),
        "respawn_needed": len(respawn),
        "results": results,
    }, indent=2))
    return 0 if not respawn else 2


def cmd_sweep_active(args):
    sm = load_state_machine()
    projects = load_projects().get("projects", {})
    scope = load_active_scope()
    results = []
    tracked = []
    for entry in scope.get("projects", []):
        name = entry.get("project")
        if not name or name not in projects:
            continue
        tracked.append(entry)
        project_payload = dict(projects[name])
        project_payload["_name"] = name
        result = evaluate_project(name, project_payload, sm)
        if entry.get("state") and result.get("state") != entry.get("state"):
            result["should_respawn"] = False
            result["reason"] = "tracked_state_mismatch"
            result["tracked_state"] = entry.get("state")
        results.append(result)
    respawn = [r for r in results if r.get("should_respawn")]
    print(json.dumps({
        "ok": True,
        "scope": "active_projects_only",
        "projects_checked": len(results),
        "respawn_needed": len(respawn),
        "tracked": tracked,
        "results": results,
        "scope_path": str(active_scope_path()),
    }, indent=2))
    return 0 if not respawn else 2


def cmd_activate(args):
    scope = load_active_scope()
    projects = [p for p in scope.get("projects", []) if p.get("project") != args.name]
    entry = {"project": args.name}
    if args.state:
        entry["state"] = args.state
    projects.append(entry)
    scope["projects"] = sorted(projects, key=lambda p: p.get("project", ""))
    save_active_scope(scope)
    print(json.dumps({"ok": True, "scope_path": str(active_scope_path()), "projects": scope["projects"]}, indent=2))
    return 0


def cmd_deactivate(args):
    scope = load_active_scope()
    scope["projects"] = [p for p in scope.get("projects", []) if p.get("project") != args.name]
    save_active_scope(scope)
    print(json.dumps({"ok": True, "scope_path": str(active_scope_path()), "projects": scope["projects"]}, indent=2))
    return 0


def cmd_list_active(args):
    scope = load_active_scope()
    print(json.dumps({"ok": True, "scope_path": str(active_scope_path()), "projects": scope.get("projects", [])}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="PM relay continuity helper")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Check whether one project needs PM rehydration")
    p_check.add_argument("name")

    sub.add_parser("sweep", help="Check all projects for PM rehydration needs")
    sub.add_parser("sweep-active", help="Check only explicitly tracked in-flight projects")

    p_activate = sub.add_parser("activate", help="Track one project for PM relay watchdogs")
    p_activate.add_argument("name")
    p_activate.add_argument("--state")

    p_deactivate = sub.add_parser("deactivate", help="Stop tracking one project for PM relay watchdogs")
    p_deactivate.add_argument("name")

    sub.add_parser("list-active", help="List explicitly tracked in-flight projects")

    args = parser.parse_args()
    if args.command == "check":
        return cmd_check(args)
    if args.command == "sweep":
        return cmd_sweep(args)
    if args.command == "sweep-active":
        return cmd_sweep_active(args)
    if args.command == "activate":
        return cmd_activate(args)
    if args.command == "deactivate":
        return cmd_deactivate(args)
    if args.command == "list-active":
        return cmd_list_active(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
