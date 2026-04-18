#!/usr/bin/env python3
"""Hardening tests for project-orchestrator.py."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SKILL_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = SKILL_ROOT / "scripts" / "orchestrator.py"
LIVE_SKILL_ROOT = Path("/home/ubuntu/openclaw-central-skills/project-orchestrator")
STAGED_SKILL_ROOT = Path("/home/ubuntu/.openclaw/workspace/skills/project-orchestrator.local-bak")


def make_temp_workspace(project_name, project_data, summary_files=None):
    tmpdir = Path(tempfile.mkdtemp(prefix="orchestrator-hardening-"))
    (tmpdir / "projects").mkdir(exist_ok=True)
    (tmpdir / "PROJECTS.yaml").write_text(json.dumps({"projects": {project_name: project_data}}, indent=2))
    for rel_path, content in (summary_files or {}).items():
        full_path = tmpdir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
    return tmpdir


def run_orchestrator(workspace, *args, expect_json=True):
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE"] = str(workspace)
    cmd = [sys.executable, str(ORCHESTRATOR), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if expect_json:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"Invalid JSON output for {cmd}: {result.stdout}\n{result.stderr}") from exc
        return payload, result.returncode
    return result.stdout, result.returncode


def load_projects_yaml(workspace):
    content = (Path(workspace) / "PROJECTS.yaml").read_text()
    if yaml:
        return yaml.safe_load(content)
    return json.loads(content)


def brief_project(project_name):
    return {
        "state": "BRIEF",
        "tier": "patch",
        "summary": f"projects/{project_name}.md",
        "linear_project_id": None,
        "state_history": [
            {"state": "INTAKE", "entered_at": "2026-04-01T00:00:00Z"},
            {"state": "BRIEF", "entered_at": "2026-04-01T00:01:00Z"},
        ],
    }


def plan_project(project_name):
    return {
        "state": "PLAN",
        "tier": "patch",
        "summary": f"projects/{project_name}.md",
        "linear_project_id": None,
        "state_history": [
            {"state": "INTAKE", "entered_at": "2026-04-01T00:00:00Z"},
            {"state": "BRIEF", "entered_at": "2026-04-01T00:01:00Z"},
            {"state": "PLAN", "entered_at": "2026-04-01T00:02:00Z"},
        ],
    }


def build_project(project_name):
    return {
        "state": "BUILD",
        "tier": "patch",
        "summary": f"projects/{project_name}.md",
        "linear_project_id": None,
        "state_history": [
            {"state": "INTAKE", "entered_at": "2026-04-01T00:00:00Z"},
            {"state": "BRIEF", "entered_at": "2026-04-01T00:01:00Z"},
            {"state": "PLAN", "entered_at": "2026-04-01T00:02:00Z"},
            {"state": "BUILD", "entered_at": "2026-04-01T00:03:00Z"},
        ],
    }


def design_project(project_name):
    return {
        "state": "DESIGN",
        "tier": "feature",
        "summary": f"projects/{project_name}.md",
        "linear_project_id": None,
        "state_history": [
            {"state": "INTAKE", "entered_at": "2026-04-01T00:00:00Z"},
            {"state": "BRIEF", "entered_at": "2026-04-01T00:01:00Z"},
            {"state": "DESIGN", "entered_at": "2026-04-01T00:02:00Z"},
        ],
    }


def write_design_bundle(project_name, spec_payload):
    spec_dir = Path("/tmp") / f"{project_name}-design"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "wireframes").mkdir(exist_ok=True)
    (spec_dir / "design-spec.json").write_text(json.dumps(spec_payload, indent=2))
    (spec_dir / "wireframes" / "screen-a.svg").write_text("<svg><text>screen-a</text></svg>")
    return spec_dir


def record_brief_transition_receipts(workspace, project_name):
    steps = [
        ("record-receipt", project_name, "--kind", "child", "--role", "producer", "--review-file", f"projects/{project_name}-review-brief-round1.md"),
        ("record-receipt", project_name, "--kind", "child", "--role", "critic", "--review-file", f"projects/{project_name}-review-brief-round1.md"),
        ("record-receipt", project_name, "--kind", "child", "--role", "pm", "--review-file", f"projects/{project_name}-review-brief-round1.md"),
        ("record-receipt", project_name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-1"),
        ("record-receipt", project_name, "--kind", "approval", "--role", "operator"),
    ]
    for step in steps:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload


def test_transition_blocked_when_approval_receipt_missing():
    name = "missing-approval"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "producer"),
        ("record-receipt", name, "--kind", "child", "--role", "critic"),
        ("record-receipt", name, "--kind", "child", "--role", "pm"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-1"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    assert payload["ok"] is False
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert "approval_receipt" in failed, failed
    print("  PASS: transition blocked for missing approval receipt")


def test_transition_blocks_stale_approval_and_child_receipts():
    name = "stale-approval"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nOriginal brief text.\n"},
    )

    record_brief_transition_receipts(workspace, name)

    summary_path = Path(workspace) / "projects" / f"{name}.md"
    summary_path.write_text(f"# {name}\n\n## Brief\nOriginal brief text changed after approval.\n")

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert failed["approval_receipt"]["stale_receipt"], failed
    assert failed["child_receipts"]["stale_roles"], failed
    print("  PASS: stale approval and child receipts rejected")


def test_design_receipts_bind_to_design_bundle_and_go_stale_on_spec_change():
    name = "design-stale-approval"
    workspace = make_temp_workspace(
        name,
        design_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Design\nDesign summary only.\n"},
    )
    write_design_bundle(name, {"screens": [{"screen_id": "welcome", "title": "Welcome"}]})

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "producer"),
        ("record-receipt", name, "--kind", "child", "--role", "critic"),
        ("record-receipt", name, "--kind", "child", "--role", "pm"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-design-1"),
        ("record-receipt", name, "--kind", "approval", "--role", "operator"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload
        if step[-1] == "operator":
            assert payload["receipt"]["artifact_hash"], payload

    approval_receipt, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, approval_receipt
    assert approval_receipt["artifact_subject"]["kind"] == "design_bundle", approval_receipt

    spec_path = Path("/tmp") / f"{name}-design" / "design-spec.json"
    spec_path.write_text(json.dumps({"screens": [{"screen_id": "welcome", "title": "Changed after sign-off"}]}, indent=2))

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert failed["approval_receipt"]["stale_receipt"], failed
    assert failed["child_receipts"]["stale_roles"], failed
    print("  PASS: DESIGN receipts bind to design bundle and stale spec changes are rejected")


def test_transition_blocked_when_child_receipts_missing():
    name = "missing-child-receipts"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    for step in [
        ("record-receipt", name, "--kind", "approval", "--role", "operator"),
        ("record-receipt", name, "--kind", "child", "--role", "producer"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-1"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert set(failed["child_receipts"]["missing_roles"]) == {"critic", "pm"}, failed
    print("  PASS: transition blocked for missing child receipts")


def test_transition_blocked_when_validation_missing():
    name = "missing-validation"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\nChecked.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-receipt", name,
        "--kind", "artifact",
        "--artifact", "test_results",
        "--metadata-json", '{"suite":"unit"}'
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "REVIEW")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert "validation_passed" in failed, failed
    assert "code_on_branch" in failed["validation_passed"]["missing"], failed
    print("  PASS: transition blocked for missing validation artifacts")


def test_transition_succeeds_with_current_structured_receipts():
    name = "brief-success"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    record_brief_transition_receipts(workspace, name)

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["new_state"] == "PLAN"
    assert "artifact_warnings" not in payload, payload
    assert all(item["satisfied"] for item in payload["transition_preconditions"]), payload


def test_status_uses_review_gate_satisfied_exit_criteria_for_approval_gates():
    name = "approval-gate-exit-criteria"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(workspace, "status", name)
    assert code == 0, payload
    assert "review_gate_satisfied" in payload["exit_criteria"], payload
    assert "inter_agent_agreement_reached" not in payload["exit_criteria"], payload
    assert "pm_signed_off" not in payload["exit_criteria"], payload



def test_review_status_exposes_persisted_review_loop_state():
    name = "review-loop-status"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--freeze-required",
        "--decision", "FREEZE_AND_ESCALATE",
        "--checkpoint-summary", "Round 3 critic failed. Freeze required.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, payload
    assert payload["review_loop"]["present"] is True, payload
    assert payload["review_loop"]["current_round"] == 3, payload
    assert payload["review_loop"]["freeze_required"] is True, payload
    assert payload["review_loop"]["decision"] == "FREEZE_AND_ESCALATE", payload


def test_review_status_infers_backfill_candidate_from_existing_review_files():
    name = "review-loop-backfill-preview"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {
            f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n",
            f"projects/{name}-review-brief-round1.md": "# round 1\n",
            f"projects/{name}-review-brief-round2.md": "# round 2\n",
        },
    )

    payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, payload
    assert payload["review_loop"]["present"] is True, payload
    assert payload["review_loop"]["source"] == "inferred_from_review_files", payload
    assert payload["review_loop"]["migration"]["needed"] is True, payload
    assert payload["review_loop"]["current_round"] == 2, payload
    assert payload["review_loop"]["checkpoint"]["file"] == f"projects/{name}-review-brief-round2.md", payload


def test_backfill_review_loop_persists_inferred_review_file_state():
    name = "review-loop-backfill"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {
            f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n",
            f"projects/{name}-review-brief-round1.md": "# round 1\n",
            f"projects/{name}-review-brief-round2.md": "# round 2\n",
        },
    )

    payload, code = run_orchestrator(workspace, "backfill-review-loop", name)
    assert code == 0, payload
    assert payload["review_loop"]["source"] == "persisted", payload
    assert payload["review_loop"]["migration"]["needed"] is False, payload
    assert payload["review_loop"]["current_round"] == 2, payload
    assert payload["review_loop"]["checkpoint"]["file"] == f"projects/{name}-review-brief-round2.md", payload


def test_child_task_status_infers_backfill_candidate_from_receipts():
    name = "child-task-backfill-preview"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "producer", "--status", "approved", "--review-file", f"projects/{name}-review-brief-round1.md"),
        ("record-receipt", name, "--kind", "child", "--role", "critic", "--status", "needs_fixes", "--review-file", f"projects/{name}-review-brief-round1.md"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--status", "active", "--session-label", "pm-brief-preview"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "child-task-status", name)
    assert code == 0, payload
    assert payload["child_tasks"]["source"] == "inferred_from_receipts", payload
    assert payload["child_tasks"]["migration"]["needed"] is True, payload
    assert payload["child_tasks"]["migration"]["source"] == "receipts", payload
    assert payload["child_tasks"]["next_actions"] == ["backfill-child-tasks"], payload
    assert payload["child_tasks"]["summary"]["total"] == 3, payload


def test_backfill_child_tasks_persists_receipt_inferred_state():
    name = "child-task-backfill"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "producer", "--status", "approved", "--review-file", f"projects/{name}-review-brief-round1.md"),
        ("record-receipt", name, "--kind", "child", "--role", "pm", "--status", "approved", "--review-file", f"projects/{name}-review-brief-round1.md"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--status", "active", "--session-label", "pm-brief-backfill"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "backfill-child-tasks", name)
    assert code == 0, payload
    assert payload["backfilled_count"] == 3, payload

    status_payload, code = run_orchestrator(workspace, "child-task-status", name)
    assert code == 0, status_payload
    assert status_payload["child_tasks"]["source"] == "persisted", status_payload
    assert status_payload["child_tasks"]["migration"]["needed"] is False, status_payload
    assert status_payload["child_tasks"]["next_actions"] == [], status_payload
    backfill_sources = [task.get("metadata", {}).get("backfill", {}).get("source") for task in status_payload["child_tasks"]["tasks"]]
    assert backfill_sources.count("receipts") == 3, status_payload


def test_record_review_checkpoint_writes_markdown_and_syncs_review_loop_state():
    name = "review-checkpoint"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-checkpoint", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--summary", "Round 3 critic still found blocking planning gaps.",
        "--producer-response-status", "addressed",
        "--decision", "FREEZE_AND_ESCALATE",
        "--unresolved-issues-json", '[{"title":"Scope still over-broad","severity":"BLOCKING"}]',
        "--accepted-risks-json", '["Operator may need to accept one deferred detail"]',
        "--carry-forward-json", '["Carry implementation detail cleanup into BUILD"]',
    )
    assert code == 0, payload
    checkpoint_path = Path(workspace) / payload["checkpoint_file"]
    assert checkpoint_path.exists(), payload
    checkpoint_text = checkpoint_path.read_text()
    assert "Round 3 critic still found blocking planning gaps." in checkpoint_text, checkpoint_text
    assert "Freeze required now: Yes" in checkpoint_text, checkpoint_text
    assert "Another round permitted: no" in checkpoint_text, checkpoint_text

    status_payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, status_payload
    assert status_payload["review_loop"]["checkpoint"]["file"] == payload["checkpoint_file"], status_payload
    assert status_payload["review_loop"]["checkpoint"]["producer_response_status"] == "addressed", status_payload
    assert status_payload["review_loop"]["freeze_required"] is True, status_payload
    assert status_payload["review_loop"]["decision"] == "FREEZE_AND_ESCALATE", status_payload


def test_record_review_loop_blocks_round_above_cap_without_override():
    name = "review-loop-write-blocked"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "4",
        "--max-rounds", "3",
    )
    assert code == 2, payload
    error_codes = {item["code"] for item in payload["errors"]}
    assert "round_cap_write_blocked" in error_codes, payload


def test_record_review_checkpoint_blocks_round_above_cap_without_override():
    name = "review-checkpoint-write-blocked"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-checkpoint", name,
        "--current-round", "4",
        "--max-rounds", "3",
        "--summary", "Attempted round 4 checkpoint.",
    )
    assert code == 2, payload
    error_codes = {item["code"] for item in payload["errors"]}
    assert "round_cap_write_blocked" in error_codes, payload


def test_validate_fails_when_backfilled_review_loop_exceeds_cap_without_override():
    name = "review-loop-invalid"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {
            f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n",
            f"projects/{name}-review-brief-round4.md": "# Legacy round 4\n",
        },
    )

    payload, code = run_orchestrator(workspace, "backfill-review-loop", name)
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "validate", name)
    assert code == 2, payload
    assert payload["review_loop"]["valid"] is False, payload
    assert payload["review_loop"]["current_round"] == 4, payload
    issue_codes = {item["code"] for item in payload["review_loop"]["issues"]}
    assert "freeze_decision_required" in issue_codes, payload


def test_validate_requires_freeze_artifact_for_frozen_cap_path():
    name = "review-loop-freeze-artifact-missing"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--freeze-required",
        "--decision", "FREEZE_AND_ESCALATE",
        "--checkpoint-summary", "Round cap hit.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "validate", name)
    assert code == 2, payload
    issue_codes = {item["code"] for item in payload["review_loop"]["issues"]}
    assert "freeze_artifact_missing" in issue_codes, payload


def test_review_status_surfaces_next_actions_at_round_cap():
    name = "review-loop-next-actions"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, payload
    assert payload["review_loop"]["at_round_cap"] is True, payload
    assert payload["review_loop"]["another_round_permitted"] is False, payload
    assert payload["review_loop"]["checkpoint_required"] is True, payload
    assert payload["review_loop"]["decision_required"] is True, payload
    assert payload["review_loop"]["decision_options"] == ["APPROVE", "CANCEL", "FREEZE_AND_ESCALATE"], payload
    assert payload["review_loop"]["next_actions"] == ["record-review-checkpoint", "record-review-loop-decision"], payload

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--freeze-required",
        "--decision", "FREEZE_AND_ESCALATE",
        "--checkpoint-summary", "Round cap hit.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, payload
    assert payload["review_loop"]["decision_required"] is False, payload
    assert payload["review_loop"]["next_actions"] == ["record-freeze-artifact"], payload


def test_record_review_loop_decision_is_actionable_follow_up_to_round_cap_guidance():
    name = "review-loop-decision-alias"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-checkpoint", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--summary", "Round cap hit.",
        "--producer-response-status", "addressed",
        "--unresolved-issues-json", '[{"title":"One scope disagreement remains","severity":"SIGNIFICANT"}]',
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop-decision", name,
        "--decision", "FREEZE_AND_ESCALATE",
        "--freeze-required",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
        "--note", "Freeze and escalate after the cap.",
    )
    assert code == 0, payload
    assert payload["review_loop"]["decision"] == "FREEZE_AND_ESCALATE", payload
    assert payload["review_loop"]["freeze_required"] is True, payload
    assert payload["review_loop"]["checkpoint"]["file"] == f"projects/{name}-review-brief-round3.md", payload

    payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, payload
    assert payload["review_loop"]["decision_required"] is False, payload
    assert payload["review_loop"]["next_actions"] == ["record-freeze-artifact"], payload


def test_review_status_distinguishes_frozen_cap_gate_from_full_signoff():
    name = "review-loop-frozen-cap-gate"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--freeze-required",
        "--decision", "FREEZE_AND_ESCALATE",
        "--checkpoint-summary", "Round cap hit.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-freeze-artifact", name,
        "--summary", "Freeze and escalate the remaining brief gaps.",
        "--rationale", "Round cap reached with one unresolved scope disagreement.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
        "--unresolved-issues-json", '[{"title":"One scope question remains open","severity":"BLOCKING"}]',
        "--accepted-risks-json", '["Operator will need to accept one carry-forward scope risk"]',
        "--carry-forward-json", '["Carry the unresolved scope note into PLAN"]',
    )
    assert code == 0, payload

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "pm"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-frozen-cap"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, payload
    assert payload["gate_satisfied"] is True, payload
    assert payload["signed_off"] is False, payload
    assert payload["full_signoff_complete"] is False, payload
    assert payload["frozen_cap_waiver_active"] is True, payload
    assert set(payload["waived_roles"]) == {"producer", "critic"}, payload
    assert payload["pm_signed_off"] is True, payload
    assert payload["child_receipts"]["satisfied"] is True, payload

    status_payload, code = run_orchestrator(workspace, "validate", name)
    assert code == 2, status_payload
    assert status_payload["inter_agent_review"]["gate_satisfied"] is True, status_payload
    assert status_payload["inter_agent_review"]["signed_off"] is False, status_payload
    assert status_payload["inter_agent_review"]["frozen_cap_waiver_active"] is True, status_payload
    print("  PASS: frozen-cap review status distinguishes gate satisfaction from full sign-off")


def test_record_freeze_artifact_writes_markdown_summary():
    name = "freeze-artifact-markdown"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-freeze-artifact", name,
        "--summary", "Freeze the brief after three rounds and escalate remaining scope gaps.",
        "--rationale", "Critic and producer still disagree on one scope boundary after the cap.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
        "--unresolved-issues-json", '[{"title":"Clarify rollout ownership","severity":"BLOCKING"}]',
        "--accepted-risks-json", '["Operator accepts one rollout ownership risk for PLAN"]',
        "--carry-forward-json", '["Carry the rollout-owner decision into PLAN"]',
    )
    assert code == 0, payload

    markdown_path = Path(workspace) / payload["freeze_artifact_markdown"]
    assert markdown_path.exists(), payload
    markdown_text = markdown_path.read_text()
    assert "Freeze the brief after three rounds and escalate remaining scope gaps." in markdown_text, markdown_text
    assert "Clarify rollout ownership" in markdown_text, markdown_text
    assert f"projects/{name}-review-brief-round3.md" in markdown_text, markdown_text
    assert payload["review_loop"]["freeze_artifact"]["markdown_file"] == payload["freeze_artifact_markdown"], payload
    print("  PASS: record-freeze-artifact writes a human-readable markdown summary alongside JSON state")


def test_review_status_flags_stage_boundary_drift_for_plan_reviews():
    name = "plan-stage-boundary-drift"
    workspace = make_temp_workspace(
        name,
        plan_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Plan\nReady for review.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-checkpoint", name,
        "--current-round", "1",
        "--max-rounds", "3",
        "--summary", "Critic drifted into implementation details.",
        "--producer-response-status", "pending",
        "--unresolved-issues-json", '[{"title":"Rename helper functions in run-weekday-peak-workflow.js","severity":"SIGNIFICANT"},{"title":"Move post_as_maya response logic into a different file","severity":"MINOR"}]',
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "review-status", name)
    assert code == 0, payload
    issue_codes = {item["code"] for item in payload["review_loop"]["issues"]}
    assert "stage_boundary_drift" in issue_codes, payload
    assert payload["review_loop"]["stage_boundary"]["count"] >= 1, payload


def test_status_verbose_includes_review_loop_child_tasks_and_gate_shape():
    name = "status-verbose"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--freeze-required",
        "--decision", "FREEZE_AND_ESCALATE",
        "--checkpoint-summary", "Round cap hit.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-freeze-artifact", name,
        "--summary", "Freeze at cap with explicit carry-forward items.",
        "--rationale", "Round cap reached and PM accepted carry-forward into PLAN.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
        "--unresolved-issues-json", '[{"title":"Sequence rollout before clean-up completion","severity":"SIGNIFICANT"}]',
        "--accepted-risks-json", '["Sequencing risk accepted for PLAN handoff"]',
        "--carry-forward-json", '["Confirm rollout order during PLAN"]',
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "critic-round-3",
        "--label", "Critic round 3",
        "--status", "active",
        "--current-step", "Reviewing updated brief",
        "--summary", "Waiting on producer response",
        "--heartbeat-at", "2099-04-01T00:05:00Z",
    )
    assert code == 0, payload

    for step in [
        (
            "record-receipt", name,
            "--kind", "child",
            "--role", "pm",
            "--status", "approved",
            "--note", "PM accepts frozen-cap handoff",
            "--review-file", f"projects/{name}-review-brief-round3.md",
        ),
        (
            "record-receipt", name,
            "--kind", "pm_session",
            "--role", "pm",
            "--status", "active",
            "--session-label", "pm-brief-frozen-cap",
        ),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "status", name, "--verbose")
    assert code == 0, payload
    assert payload["review_loop"]["present"] is True, payload
    assert payload["review_loop"]["current_round"] == 3, payload
    assert payload["child_tasks"]["present"] is True, payload
    assert payload["child_tasks"]["summary"]["active"] == 1, payload
    assert payload["inter_agent_review"]["gate_satisfied"] is True, payload
    assert payload["inter_agent_review"]["signed_off"] is False, payload
    assert payload["inter_agent_review"]["frozen_cap_waiver_active"] is True, payload
    assert set(payload["inter_agent_review"]["waived_roles"]) == {"producer", "critic"}, payload
    print("  PASS: status --verbose includes review-loop, child-task state, and frozen-cap gate shape")


def test_child_task_status_flags_stale_heartbeat():
    name = "child-task-health"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\n- none\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--label", "Watchdog worker",
        "--status", "active",
        "--current-step", "waiting for critic",
        "--summary", "No recent heartbeat",
        "--heartbeat-at", "2026-04-01T00:00:00Z",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "child-task-status", name, "--stale-after-minutes", "15")
    assert code == 0, payload
    issue_codes = {item["code"] for item in payload["child_tasks"]["issues"]}
    assert "child_task_stale" in issue_codes, payload
    assert payload["child_tasks"]["summary"]["active"] == 1, payload
    print("  PASS: child-task status flags stale heartbeat")


def test_record_child_task_refreshes_heartbeat_by_default():
    name = "child-task-heartbeat-refresh"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\n- none\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--label", "Watchdog worker",
        "--status", "active",
        "--current-step", "waiting for critic",
        "--summary", "No recent heartbeat",
        "--heartbeat-at", "2026-04-01T00:00:00Z",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--status", "blocked",
        "--current-step", "waiting for secret",
        "--summary", "External blocker remains",
        "--blocked-reason", "Missing provider credential",
        "--attention-required",
    )
    assert code == 0, payload
    child_task = payload["child_task"]
    assert child_task["heartbeat_at"] == child_task["updated_at"], payload
    assert child_task["heartbeat_at"] != "2026-04-01T00:00:00Z", payload

    payload, code = run_orchestrator(workspace, "child-task-status", name, "--stale-after-minutes", "15")
    assert code == 0, payload
    issue_codes = {item["code"] for item in payload["child_tasks"]["issues"]}
    assert "child_task_stale" not in issue_codes, payload
    assert "child_task_attention_required" in issue_codes, payload
    print("  PASS: record-child-task refreshes heartbeat by default")


def test_child_task_watchdog_returns_alert_payload_for_stale_task():
    name = "child-task-watchdog"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\n- none\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--label", "Watchdog worker",
        "--status", "active",
        "--current-step", "waiting for PM",
        "--summary", "No recent heartbeat",
        "--heartbeat-at", "2026-04-01T00:00:00Z",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "child-task-watchdog", name, "--stale-after-minutes", "15", "--exception-only")
    assert code == 0, payload
    assert payload["should_alert"] is True, payload
    issue_codes = {item["code"] for item in payload["exceptions"]}
    assert "child_task_stale" in issue_codes, payload
    print("  PASS: child-task watchdog returns alert payload")


def test_child_task_watchdog_can_exit_nonzero_when_alerts_exist():
    name = "child-task-watchdog-exit"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\n- none\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--label", "Watchdog worker",
        "--status", "active",
        "--current-step", "waiting for PM",
        "--summary", "No recent heartbeat",
        "--heartbeat-at", "2026-04-01T00:00:00Z",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "child-task-watchdog", name,
        "--stale-after-minutes", "15",
        "--exception-only",
        "--exit-nonzero-on-alert",
    )
    assert code == 2, payload
    assert payload["should_alert"] is True, payload
    print("  PASS: child-task watchdog can fail closed for alerting automation")


def test_child_task_watchdog_flags_attention_required_without_waiting_for_staleness():
    name = "child-task-attention-required"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\n- none\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--label", "Producer worker",
        "--status", "blocked",
        "--current-step", "waiting for PM",
        "--summary", "Needs a decision before continuing",
        "--heartbeat-at", "2099-04-01T00:00:00Z",
        "--blocked-reason", "Waiting on PM decision",
        "--attention-required",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "child-task-watchdog", name,
        "--stale-after-minutes", "15",
        "--exception-only",
    )
    assert code == 0, payload
    assert payload["should_alert"] is True, payload
    assert payload["summary"]["blocked"] == 1, payload
    issue_codes = {item["code"] for item in payload["exceptions"]}
    assert "child_task_attention_required" in issue_codes, payload
    assert "child_task_stale" not in issue_codes, payload
    print("  PASS: child-task watchdog flags attention-required work before it goes stale")


def test_validate_marks_payload_invalid_when_child_tasks_are_invalid():
    name = "validate-child-task-invalid"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\n- none\n"},
    )

    for artifact, metadata in [
        ("code_on_branch", '{"branch":"feature/test"}'),
        ("test_results", '{"suite":"unit","status":"pass"}'),
    ]:
        payload, code = run_orchestrator(
            workspace,
            "record-receipt", name,
            "--kind", "artifact",
            "--artifact", artifact,
            "--metadata-json", metadata,
        )
        assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--label", "Watchdog worker",
        "--status", "active",
        "--current-step", "waiting for PM",
        "--summary", "No recent heartbeat",
        "--heartbeat-at", "2026-04-01T00:00:00Z",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "validate", name)
    assert code == 2, payload
    assert payload["valid"] is False, payload
    assert payload["ready_to_transition"] is False, payload
    issue_codes = {item["code"] for item in payload["child_tasks"]["issues"]}
    assert "child_task_stale" in issue_codes, payload
    print("  PASS: validate stays invalid when child-task watchdog reports exceptions")


def test_transition_block_output_includes_review_loop_and_child_task_context():
    name = "transition-block-context"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\n- none\n"},
    )

    for artifact, metadata in [
        ("code_on_branch", '{"branch":"feature/test"}'),
        ("test_results", '{"suite":"unit","status":"pass"}'),
    ]:
        payload, code = run_orchestrator(
            workspace,
            "record-receipt", name,
            "--kind", "artifact",
            "--artifact", artifact,
            "--metadata-json", metadata,
        )
        assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-child-task", name,
        "--task-id", "worker-1",
        "--label", "Watchdog worker",
        "--status", "active",
        "--current-step", "waiting for PM",
        "--summary", "No recent heartbeat",
        "--heartbeat-at", "2026-04-01T00:00:00Z",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "REVIEW")
    assert code == 2, payload
    assert payload["review_loop"]["present"] is False, payload
    assert payload["child_tasks"]["present"] is True, payload
    issue_codes = {item["code"] for item in payload["child_tasks"]["issues"]}
    assert "child_task_stale" in issue_codes, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert "child_task_health" in failed, payload
    print("  PASS: blocked transition output includes review-loop and child-task context")



def test_validate_accepts_frozen_cap_transition_path_without_critic_signoff():
    name = "review-loop-freeze-artifact-present"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--freeze-required",
        "--decision", "FREEZE_AND_ESCALATE",
        "--checkpoint-summary", "Round cap hit.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
    )
    assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "record-freeze-artifact", name,
        "--summary", "Freeze at cap and escalate remaining blockers.",
        "--rationale", "Critic kept reopening implementation-level items after the bounded review window.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
        "--unresolved-issues-json", '[{"severity":"BLOCKING","issue":"Need operator decision on unresolved plan gap."}]',
        "--accepted-risks-json", '["Operator reviews with one unresolved critic concern still open."]',
        "--carry-forward-json", '["Carry implementation-detail follow-ups into BUILD task breakdown."]',
    )
    assert code == 0, payload

    steps = [
        ("record-receipt", name, "--kind", "child", "--role", "pm", "--review-file", f"projects/{name}-review-brief-round3.md"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-frozen-cap"),
        ("record-receipt", name, "--kind", "approval", "--role", "operator"),
    ]
    for step in steps:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "validate", name)
    assert code == 0, payload
    child_precondition = next(item for item in payload["transition_preconditions"] if item["type"] == "child_receipts")
    assert child_precondition["satisfied"] is True, payload
    assert child_precondition["required_roles"] == ["pm"], payload
    assert set(child_precondition["waived_roles"]) == {"producer", "critic"}, payload
    assert payload["review_loop"]["mode"] == "frozen_cap", payload
    assert payload["review_loop"]["freeze_artifact"]["present"] is True, payload

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 0, payload
    assert payload["new_state"] == "PLAN", payload
    print("  PASS: frozen-cap path transitions with PM + freeze artifact even without critic sign-off")


def test_transition_blocked_when_review_loop_records_cancel_decision():
    name = "review-loop-cancel-decision"
    workspace = make_temp_workspace(
        name,
        brief_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Brief\nReady for plan.\n"},
    )

    payload, code = run_orchestrator(
        workspace,
        "record-review-loop", name,
        "--current-round", "3",
        "--max-rounds", "3",
        "--freeze-required",
        "--decision", "CANCEL",
        "--checkpoint-summary", "Round cap hit and PM recommends canceling instead of forcing another review round.",
        "--checkpoint-file", f"projects/{name}-review-brief-round3.md",
    )
    assert code == 0, payload

    for step in [
        ("record-receipt", name, "--kind", "child", "--role", "producer", "--review-file", f"projects/{name}-review-brief-round3.md"),
        ("record-receipt", name, "--kind", "child", "--role", "critic", "--review-file", f"projects/{name}-review-brief-round3.md"),
        ("record-receipt", name, "--kind", "child", "--role", "pm", "--review-file", f"projects/{name}-review-brief-round3.md"),
        ("record-receipt", name, "--kind", "pm_session", "--role", "pm", "--session-label", "pm-brief-cancel"),
        ("record-receipt", name, "--kind", "approval", "--role", "operator"),
    ]:
        payload, code = run_orchestrator(workspace, *step)
        assert code == 0, payload

    payload, code = run_orchestrator(workspace, "transition", name, "PLAN")
    assert code == 2, payload
    failed = {item["type"]: item for item in payload["transition_preconditions"] if not item["satisfied"]}
    assert "review_loop_decision" in failed, payload
    assert failed["review_loop_decision"]["decision"] == "CANCEL", payload
    assert failed["review_loop_decision"]["reason"] == "cancel_decision_recorded", payload
    print("  PASS: approval-gate transition is blocked when the review loop records CANCEL")


def test_build_transition_succeeds_with_artifact_receipts():
    name = "build-success"
    workspace = make_temp_workspace(
        name,
        build_project(name),
        {f"projects/{name}.md": f"# {name}\n\n## Verified API Schemas\nChecked.\n"},
    )

    for artifact, metadata in [
        ("code_on_branch", '{"branch":"feature/test"}'),
        ("test_results", '{"suite":"unit","status":"pass"}'),
    ]:
        payload, code = run_orchestrator(
            workspace,
            "record-receipt", name,
            "--kind", "artifact",
            "--artifact", artifact,
            "--metadata-json", metadata,
        )
        assert code == 0, payload

    payload, code = run_orchestrator(
        workspace,
        "transition", name, "REVIEW",
        "--actor-id", "justin",
        "--actor-role", "pa",
        "--session-id", "sess-123",
        "--request-id", "req-456",
        "--channel", "telegram",
    )
    assert code == 0, payload
    assert payload["new_state"] == "REVIEW"

    updated = load_projects_yaml(workspace)
    history = updated["projects"][name]["state_history"]
    new_entry = history[-1]
    assert new_entry["recorded_by"]["actor_id"] == "justin", new_entry
    assert new_entry["recorded_by"]["session_id"] == "sess-123", new_entry
    assert new_entry["recorded_by"]["request_id"] == "req-456", new_entry
    assert new_entry["recorded_by"]["channel"] == "telegram", new_entry
    print("  PASS: BUILD -> REVIEW succeeds and records real audit metadata")


def test_staged_skill_sources_match_live_skill_sources():
    files = [
        "README.md",
        "SKILL.md",
        "references/state-machine.yaml",
        "references/templates/inter-agent-review.md",
        "scripts/linear_integration.py",
        "scripts/orchestrator.py",
        "scripts/pm-checker.py",
        "tests/test_orchestrator_hardening.py",
        "tests/test_pm_checker.py",
    ]

    mismatches = []
    for rel_path in files:
        staged_path = STAGED_SKILL_ROOT / rel_path
        live_path = LIVE_SKILL_ROOT / rel_path
        assert staged_path.exists(), f"staged path missing: {staged_path}"
        assert live_path.exists(), f"live path missing: {live_path}"
        if staged_path.read_text() != live_path.read_text():
            mismatches.append(rel_path)

    assert not mismatches, f"staged/live drift detected: {', '.join(mismatches)}"
    print("  PASS: staged project-orchestrator sources stay aligned with the live skill path")


def main():
    print("Running orchestrator hardening tests...\n")

    tests = [
        test_transition_blocked_when_approval_receipt_missing,
        test_transition_blocks_stale_approval_and_child_receipts,
        test_design_receipts_bind_to_design_bundle_and_go_stale_on_spec_change,
        test_transition_blocked_when_child_receipts_missing,
        test_transition_blocked_when_validation_missing,
        test_transition_succeeds_with_current_structured_receipts,
        test_review_status_exposes_persisted_review_loop_state,
        test_review_status_infers_backfill_candidate_from_existing_review_files,
        test_backfill_review_loop_persists_inferred_review_file_state,
        test_child_task_status_infers_backfill_candidate_from_receipts,
        test_backfill_child_tasks_persists_receipt_inferred_state,
        test_record_review_checkpoint_writes_markdown_and_syncs_review_loop_state,
        test_record_review_loop_blocks_round_above_cap_without_override,
        test_record_review_checkpoint_blocks_round_above_cap_without_override,
        test_validate_fails_when_backfilled_review_loop_exceeds_cap_without_override,
        test_validate_requires_freeze_artifact_for_frozen_cap_path,
        test_review_status_surfaces_next_actions_at_round_cap,
        test_record_review_loop_decision_is_actionable_follow_up_to_round_cap_guidance,
        test_review_status_distinguishes_frozen_cap_gate_from_full_signoff,
        test_record_freeze_artifact_writes_markdown_summary,
        test_review_status_flags_stage_boundary_drift_for_plan_reviews,
        test_status_verbose_includes_review_loop_child_tasks_and_gate_shape,
        test_child_task_status_flags_stale_heartbeat,
        test_child_task_watchdog_returns_alert_payload_for_stale_task,
        test_child_task_watchdog_can_exit_nonzero_when_alerts_exist,
        test_child_task_watchdog_flags_attention_required_without_waiting_for_staleness,
        test_validate_marks_payload_invalid_when_child_tasks_are_invalid,
        test_transition_block_output_includes_review_loop_and_child_task_context,
        test_validate_accepts_frozen_cap_transition_path_without_critic_signoff,
        test_transition_blocked_when_review_loop_records_cancel_decision,
        test_build_transition_succeeds_with_artifact_receipts,
        test_staged_skill_sources_match_live_skill_sources,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {test.__name__}: {exc}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed, {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
