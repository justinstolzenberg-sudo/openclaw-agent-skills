#!/usr/bin/env python3
"""Linear API integration for the project-orchestrator framework.

Provides CLI commands for managing Linear issues, projects, and state transitions.
Uses stdlib only (urllib.request, json). Token from LINEAR_API_TOKEN env var.

Usage:
    linear_integration.py <command> [options]

Commands:
    create-project      Create a Linear project
    create-issue        Create a Linear issue
    create-issues-from-plan  Bulk-create issues from a plan file
    update-state        Update an issue's workflow state
    sync-state          Map framework state to Linear state and update
    add-comment         Add a comment to an issue
    get-issue           Get issue details by identifier
    validate-transition Check if a state transition is valid
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

# --- Constants ---

LINEAR_API_URL = "https://api.linear.app/graphql"
TEAM_ID = os.environ.get("LINEAR_TEAM_ID", "")
DEFAULT_ASSIGNEE_ID = os.environ.get("LINEAR_DEFAULT_ASSIGNEE_ID", "")
DEFAULT_PROJECT_LEAD_ID = os.environ.get("LINEAR_PROJECT_LEAD_ID", "")

LINEAR_STATES = {
    "Backlog": "dd51040a-9d6e-4c30-849a-8301f50539b4",
    "Todo": "0e98be62-b140-4428-bd2e-3138d850c80e",
    "In Progress": "6b25f46d-3679-4e4e-ba39-5b101c778fe5",
    "In Dev": "9502e2ad-d1e8-456d-a40d-2a4228324e6d",
    "Review": "83acf228-0623-49ee-89fa-6d46bae37e36",
    "Done": "44cc8dae-a862-43d1-8874-5c9cc9c1253f",
    "In Prod": "8ba1ed9c-a4ad-4ec7-919e-89fc5bef71b6",
    "Canceled": "17b0e020-3ad7-405a-a2e1-5ff625620344",
}

FRAMEWORK_TO_LINEAR = {
    "INTAKE": "Backlog",
    "BRIEF": "Backlog",
    "ARCHITECTURE": "In Progress",
    "PLAN": "Todo",
    "BUILD": "In Dev",
    "REVIEW": "Review",
    "SHIP": "Done",
    "CLOSED": "In Prod",
}

# Valid state transitions: set of (from_state, to_state) tuples
VALID_TRANSITIONS = {
    ("Backlog", "Todo"),
    ("Backlog", "In Progress"),
    ("In Progress", "Todo"),
    ("Todo", "In Dev"),
    ("In Dev", "Review"),
    ("Review", "In Dev"),
    ("Review", "Done"),
    ("Done", "In Prod"),
}
# "Any -> Canceled" is handled separately


# --- Token retrieval ---

def get_token():
    """Get Linear API token from LINEAR_API_TOKEN env var or /tmp/.linear_token."""
    token = os.environ.get("LINEAR_API_TOKEN") or os.environ.get("LINEAR_TOKEN")
    if token:
        return token.strip()
    try:
        with open("/tmp/.linear_token") as f:
            token = f.read().strip()
        if token:
            return token
    except (FileNotFoundError, PermissionError):
        pass
    print(json.dumps({"error": "No LINEAR_API_TOKEN available"}), file=sys.stderr)
    sys.exit(2)


# --- Text helpers ---

def unescape_text(text):
    """Unescape literal \\n sequences to actual newlines.
    Handles text passed through CLI args or subprocess where newlines get escaped."""
    if not text:
        return text
    return text.replace('\\n', '\n').replace('\\t', '\t')


# --- GraphQL client ---

def graphql(query, variables=None, token=None):
    """Execute a GraphQL query against Linear API. Returns parsed response dict."""
    if token is None:
        token = get_token()
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else ""
        print(json.dumps({"error": f"HTTP {e.code}", "detail": err_body}), file=sys.stderr)
        sys.exit(2)
    except urllib.error.URLError as e:
        print(json.dumps({"error": f"Network error: {e.reason}"}), file=sys.stderr)
        sys.exit(2)

    if "errors" in body:
        print(json.dumps({"error": "GraphQL errors", "details": body["errors"]}), file=sys.stderr)
        sys.exit(2)
    return body


# --- Commands ---


def cmd_create_project(args):
    create_query = """
    mutation CreateProject($input: ProjectCreateInput!) {
        projectCreate(input: $input) {
            success
            project {
                id
                url
            }
        }
    }
    """
    # Linear caps project description at 255 chars
    short_desc = (args.description or "")[:255]

    variables = {
        "input": {
            "name": args.name,
            "teamIds": [TEAM_ID],
        }
    }
    if short_desc:
        variables["input"]["description"] = short_desc
    if DEFAULT_PROJECT_LEAD_ID:
        variables["input"]["leadId"] = DEFAULT_PROJECT_LEAD_ID

    result = graphql(create_query, variables)
    project = result["data"]["projectCreate"]["project"]
    project_id = project["id"]

    # If a brief file is provided, post its contents as a project update (description is capped at 255 chars)
    if args.brief_file:
        with open(args.brief_file, "r") as f:
            brief_content = f.read()
        update_query = """
        mutation CreateProjectUpdate($input: ProjectUpdateCreateInput!) {
            projectUpdateCreate(input: $input) {
                success
            }
        }
        """
        graphql(update_query, {"input": {"projectId": project_id, "body": brief_content}})

    print(json.dumps({"project_id": project_id, "url": project["url"]}))


def cmd_create_issue(args):
    query = """
    mutation CreateIssue($input: IssueCreateInput!) {
        issueCreate(input: $input) {
            success
            issue {
                id
                identifier
                url
            }
        }
    }
    """
    # Resolve state name to ID
    state_id = LINEAR_STATES.get(args.state)
    if not state_id:
        print(json.dumps({"error": f"Unknown state: {args.state}. Valid: {list(LINEAR_STATES.keys())}"}), file=sys.stderr)
        sys.exit(1)

    variables = {
        "input": {
            "teamId": TEAM_ID,
            "title": args.title,
            "stateId": state_id,
            "priority": args.priority,
        }
    }
    if args.project_id:
        variables["input"]["projectId"] = args.project_id
    if args.description:
        variables["input"]["description"] = unescape_text(args.description)
    assignee_id = args.assignee or DEFAULT_ASSIGNEE_ID
    if assignee_id:
        variables["input"]["assigneeId"] = assignee_id
    if args.parent_id:
        variables["input"]["parentId"] = args.parent_id

    result = graphql(query, variables)
    issue = result["data"]["issueCreate"]["issue"]
    print(json.dumps({"issue_id": issue["id"], "identifier": issue["identifier"], "url": issue["url"]}))


def cmd_create_issues_from_plan(args):
    plan_path = args.plan_file
    with open(plan_path, "r") as f:
        content = f.read()

    # Try JSON first, then YAML-like parsing (stdlib only - no pyyaml)
    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        plan = _parse_simple_yaml(content)

    tasks = plan.get("tasks", [])
    if not tasks:
        print(json.dumps({"error": "No tasks found in plan file"}), file=sys.stderr)
        sys.exit(1)

    created = []
    for task in tasks:
        query = """
        mutation CreateIssue($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                success
                issue {
                    id
                    identifier
                    url
                }
            }
        }
        """
        state = task.get("state", "Backlog")
        state_id = LINEAR_STATES.get(state, LINEAR_STATES["Backlog"])
        priority = task.get("priority", 3)
        estimate = task.get("estimate_hours")

        inp = {
            "teamId": TEAM_ID,
            "title": task["title"],
            "stateId": state_id,
            "priority": priority,
            "projectId": args.project_id,
        }
        if task.get("description"):
            inp["description"] = task["description"]
        if estimate is not None:
            inp["estimate"] = estimate
        inp["assigneeId"] = task.get("assignee") or DEFAULT_ASSIGNEE_ID

        result = graphql(query, {"input": inp})
        issue = result["data"]["issueCreate"]["issue"]
        created.append({
            "issue_id": issue["id"],
            "identifier": issue["identifier"],
            "url": issue["url"],
            "title": task["title"],
        })

    print(json.dumps(created, indent=2))


def cmd_update_state(args):
    state_id = LINEAR_STATES.get(args.state)
    if not state_id:
        print(json.dumps({"error": f"Unknown state: {args.state}. Valid: {list(LINEAR_STATES.keys())}"}), file=sys.stderr)
        sys.exit(1)

    query = """
    mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) {
            success
            issue {
                id
                identifier
                state {
                    name
                }
            }
        }
    }
    """
    variables = {
        "id": args.issue_id,
        "input": {"stateId": state_id},
    }
    result = graphql(query, variables)
    issue = result["data"]["issueUpdate"]["issue"]
    print(json.dumps({
        "issue_id": issue["id"],
        "identifier": issue["identifier"],
        "state": issue["state"]["name"],
    }))


def cmd_sync_state(args):
    fw_state = args.framework_state.upper()
    linear_state = FRAMEWORK_TO_LINEAR.get(fw_state)
    if not linear_state:
        print(json.dumps({
            "error": f"Unknown framework state: {fw_state}. Valid: {list(FRAMEWORK_TO_LINEAR.keys())}"
        }), file=sys.stderr)
        sys.exit(1)

    state_id = LINEAR_STATES[linear_state]

    query = """
    mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) {
            success
            issue {
                id
                identifier
                state {
                    name
                }
            }
        }
    }
    """
    variables = {
        "id": args.issue_id,
        "input": {"stateId": state_id},
    }
    result = graphql(query, variables)
    issue = result["data"]["issueUpdate"]["issue"]
    print(json.dumps({
        "issue_id": issue["id"],
        "identifier": issue["identifier"],
        "framework_state": fw_state,
        "linear_state": issue["state"]["name"],
    }))


def cmd_add_comment(args):
    query = """
    mutation CreateComment($input: CommentCreateInput!) {
        commentCreate(input: $input) {
            success
            comment {
                id
                body
            }
        }
    }
    """
    variables = {
        "input": {
            "issueId": args.issue_id,
            "body": unescape_text(args.body),
        }
    }
    result = graphql(query, variables)
    comment = result["data"]["commentCreate"]["comment"]
    print(json.dumps({"comment_id": comment["id"], "body": comment["body"]}))


def cmd_get_issue(args):
    query = """
    query GetIssue($identifier: String!) {
        issue(id: $identifier) {
            id
            identifier
            title
            description
            priority
            estimate
            url
            state {
                id
                name
            }
            assignee {
                id
                name
            }
            project {
                id
                name
            }
            parent {
                id
                identifier
            }
            labels {
                nodes {
                    id
                    name
                }
            }
            createdAt
            updatedAt
        }
    }
    """
    # Linear's issue() query actually needs the UUID, not the identifier.
    # For identifier-based lookup, use issueSearch or the issues filter.
    search_query = """
    query SearchIssue($filter: IssueFilter) {
        issues(filter: $filter, first: 1) {
            nodes {
                id
                identifier
                title
                description
                priority
                estimate
                url
                state {
                    id
                    name
                }
                assignee {
                    id
                    name
                }
                project {
                    id
                    name
                }
                parent {
                    id
                    identifier
                }
                labels {
                    nodes {
                        id
                        name
                    }
                }
                createdAt
                updatedAt
            }
        }
    }
    """
    identifier = args.identifier

    # If it looks like a UUID, use direct lookup
    if len(identifier) == 36 and identifier.count("-") == 4:
        result = graphql(query, {"identifier": identifier})
        issue = result["data"]["issue"]
    else:
        # Parse identifier like "MET-8503" into team key + number
        parts = identifier.split("-")
        if len(parts) == 2:
            try:
                number = int(parts[1])
                result = graphql(search_query, {
                    "filter": {
                        "team": {"id": {"eq": TEAM_ID}},
                        "number": {"eq": number},
                    }
                })
                nodes = result["data"]["issues"]["nodes"]
                if not nodes:
                    print(json.dumps({"error": f"Issue {identifier} not found"}), file=sys.stderr)
                    sys.exit(1)
                issue = nodes[0]
            except ValueError:
                print(json.dumps({"error": f"Invalid identifier format: {identifier}"}), file=sys.stderr)
                sys.exit(1)
        else:
            print(json.dumps({"error": f"Invalid identifier format: {identifier}"}), file=sys.stderr)
            sys.exit(1)

    output = {
        "id": issue["id"],
        "identifier": issue["identifier"],
        "title": issue["title"],
        "description": issue.get("description"),
        "priority": issue.get("priority"),
        "estimate": issue.get("estimate"),
        "url": issue["url"],
        "state": issue["state"]["name"] if issue.get("state") else None,
        "state_id": issue["state"]["id"] if issue.get("state") else None,
        "assignee": issue["assignee"]["name"] if issue.get("assignee") else None,
        "assignee_id": issue["assignee"]["id"] if issue.get("assignee") else None,
        "project": issue["project"]["name"] if issue.get("project") else None,
        "project_id": issue["project"]["id"] if issue.get("project") else None,
        "parent_id": issue["parent"]["id"] if issue.get("parent") else None,
        "parent_identifier": issue["parent"]["identifier"] if issue.get("parent") else None,
        "labels": [l["name"] for l in issue.get("labels", {}).get("nodes", [])],
        "created_at": issue.get("createdAt"),
        "updated_at": issue.get("updatedAt"),
    }
    print(json.dumps(output, indent=2))


def cmd_post_project_update(args):
    """Post a project update to a Linear project."""
    query = """
    mutation CreateProjectUpdate($input: ProjectUpdateCreateInput!) {
        projectUpdateCreate(input: $input) {
            success
            projectUpdate { id }
        }
    }
    """
    body = args.body
    if args.body_file:
        with open(args.body_file, "r") as f:
            body = f.read()

    if not body:
        print(json.dumps({"error": "No body provided. Use --body or --body-file"}), file=sys.stderr)
        sys.exit(1)

    variables = {"input": {"projectId": args.project_id, "body": body}}
    result = graphql(query, variables)
    update = result["data"]["projectUpdateCreate"]["projectUpdate"]
    print(json.dumps({"update_id": update["id"]}))


def cmd_update_project_description(args):
    """Update a Linear project's description field."""
    query = """
    mutation UpdateProject($id: String!, $input: ProjectUpdateInput!) {
        projectUpdate(id: $id, input: $input) {
            success
            project { id }
        }
    }
    """
    body = args.body
    if args.body_file:
        with open(args.body_file, "r") as f:
            body = f.read()

    if not body:
        print(json.dumps({"error": "No body provided. Use --body or --body-file"}), file=sys.stderr)
        sys.exit(1)

    variables = {"id": args.project_id, "input": {"description": body}}
    result = graphql(query, variables)
    success = result.get("data", {}).get("projectUpdate", {}).get("success", False)
    print(json.dumps({"success": success}))


def cmd_validate_transition(args):
    from_state = args.from_state
    to_state = args.to_state

    # Validate state names
    for s in [from_state, to_state]:
        if s not in LINEAR_STATES:
            print(json.dumps({"error": f"Unknown state: {s}. Valid: {list(LINEAR_STATES.keys())}"}), file=sys.stderr)
            sys.exit(1)

    # Any -> Canceled is always valid
    if to_state == "Canceled":
        print(json.dumps({"valid": True, "from": from_state, "to": to_state}))
        return

    if (from_state, to_state) in VALID_TRANSITIONS:
        print(json.dumps({"valid": True, "from": from_state, "to": to_state}))
    else:
        print(json.dumps({
            "valid": False,
            "from": from_state,
            "to": to_state,
            "error": f"Invalid transition: {from_state} -> {to_state}",
            "valid_from_here": [t for f, t in VALID_TRANSITIONS if f == from_state] + ["Canceled"],
        }), file=sys.stderr)
        sys.exit(1)


# --- Simple YAML parser (stdlib only, handles the plan format) ---

def _parse_simple_yaml(content):
    """Minimal YAML parser for the plan file format. Handles simple key-value and list-of-dicts."""
    tasks = []
    current = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "tasks:":
            continue
        if stripped.startswith("- "):
            if current is not None:
                tasks.append(current)
            current = {}
            # Parse inline key-value on the "- " line
            kv = stripped[2:].strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                current[k.strip()] = _yaml_val(v.strip())
        elif current is not None and ":" in stripped:
            k, v = stripped.split(":", 1)
            current[k.strip()] = _yaml_val(v.strip())
    if current is not None:
        tasks.append(current)
    return {"tasks": tasks}


def _yaml_val(s):
    """Convert a simple YAML value string to a Python type."""
    if not s:
        return ""
    # Remove quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    # Booleans
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    # Numbers
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


# --- CLI parser ---

def build_parser():
    parser = argparse.ArgumentParser(description="Linear API integration for project-orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    # create-project
    p = sub.add_parser("create-project")
    p.add_argument("--name", required=True)
    p.add_argument("--description", default=None)
    p.add_argument("--brief-file", default=None, help="Path to brief markdown file - contents used as project description")

    # create-issue
    p = sub.add_parser("create-issue")
    p.add_argument("--project-id", default=None)
    p.add_argument("--title", required=True)
    p.add_argument("--description", default=None)
    p.add_argument("--state", default="Backlog")
    p.add_argument("--priority", type=int, default=3)
    p.add_argument("--assignee", default=None)
    p.add_argument("--parent-id", default=None)

    # create-issues-from-plan
    p = sub.add_parser("create-issues-from-plan")
    p.add_argument("--project-id", required=True)
    p.add_argument("--plan-file", required=True)

    # update-state
    p = sub.add_parser("update-state")
    p.add_argument("--issue-id", required=True)
    p.add_argument("--state", required=True)

    # sync-state
    p = sub.add_parser("sync-state")
    p.add_argument("--issue-id", required=True)
    p.add_argument("--framework-state", required=True)

    # add-comment
    p = sub.add_parser("add-comment")
    p.add_argument("--issue-id", required=True)
    p.add_argument("--body", required=True)

    # get-issue
    p = sub.add_parser("get-issue")
    p.add_argument("--identifier", required=True)

    # post-project-update
    p = sub.add_parser("post-project-update")
    p.add_argument("--project-id", required=True)
    p.add_argument("--body", default=None, help="Markdown body for the project update")
    p.add_argument("--body-file", default=None, help="Path to file containing the update body")

    # update-project-description
    p = sub.add_parser("update-project-description")
    p.add_argument("--project-id", required=True)
    p.add_argument("--body", default=None, help="Markdown body for the project description")
    p.add_argument("--body-file", default=None, help="Path to file containing the description body")

    # validate-transition
    p = sub.add_parser("validate-transition")
    p.add_argument("--issue-id", default=None, help="Issue ID (not used for validation, kept for CLI consistency)")
    p.add_argument("--from-state", required=True)
    p.add_argument("--to-state", required=True)

    return parser


COMMAND_MAP = {
    "create-project": cmd_create_project,
    "create-issue": cmd_create_issue,
    "create-issues-from-plan": cmd_create_issues_from_plan,
    "update-state": cmd_update_state,
    "sync-state": cmd_sync_state,
    "add-comment": cmd_add_comment,
    "get-issue": cmd_get_issue,
    "post-project-update": cmd_post_project_update,
    "update-project-description": cmd_update_project_description,
    "validate-transition": cmd_validate_transition,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    handler = COMMAND_MAP.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
