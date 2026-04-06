---
name: project-orchestrator
description: >
  Deterministic state machine for software project lifecycle management.
  Enforces tier-based workflows (patch/feature/project) with approval gates,
  artifact validation, and Linear state mapping. Use when: creating a new project,
  checking project status, transitioning project state, validating project artifacts,
  reviewing project roadmap/plan, or any mention of "project state", "project status",
  "start a project", "new project", "project tier", "state machine", "transition to",
  "approve brief/plan/review". NOT for: ad-hoc tasks, one-off questions, or anything
  that doesn't involve a tracked project in PROJECTS.yaml.
---

# Project Orchestrator

Deterministic state machine engine for software project lifecycle. No LLM decisions - purely validates states, transitions, and artifacts against `references/state-machine.yaml`.

## Quick Reference

All commands output JSON. Script path: `skills/project-orchestrator/scripts/orchestrator.py`

```bash
# Initialize a new project
python3 scripts/orchestrator.py init <name> --tier <patch|feature|project>

# Check current state + what's needed
python3 scripts/orchestrator.py status <name>

# Validate artifacts for current state
python3 scripts/orchestrator.py validate <name>

# Execute a state transition
python3 scripts/orchestrator.py transition <name> <TARGET_STATE>

# Show full roadmap + history
python3 scripts/orchestrator.py plan <name>
```

Run from workspace root (`~/.openclaw/workspace`).

## Tiers

Choose tier at INTAKE based on scope:

| Tier | Token Budget | Scope | ARCHITECTURE state? |
|------|-------------|-------|-------------------|
| patch | < 50k | Single module, no arch change | No |
| feature | 50k-300k | Contained scope, existing arch | No |
| project | > 300k | New repo or major arch change | Yes |

**All estimates use token cost, never time.** Estimate token budget at INTAKE: count expected subagent runs × avg tokens per run. Task-level estimates in PLAN use token cost (e.g. "~20k tokens", "~80k tokens") - never hours or days. When unsure, default to `feature`. Upgrade to `project` if arch decisions emerge during BRIEF.

## State Flow

**patch/feature:** INTAKE - BRIEF - PLAN - BUILD - REVIEW - SHIP - CLOSED
**project:** INTAKE - BRIEF - ARCHITECTURE - PLAN - BUILD - REVIEW - SHIP - CLOSED

Any state can transition to CANCELED.

## Approval Gates

States with `approval_gate: true`: **BRIEF, ARCHITECTURE, PLAN, REVIEW**

When hitting an approval gate:
1. Run `validate` to confirm artifacts exist
2. Present the artifact summary to the operator via Telegram
3. Wait for explicit operator approval ("approved", "yes", "lgtm", etc.)
4. Only then run `transition` to advance

Never auto-approve. Never skip gates.

### Automatic Artifact Issues

When transitioning INTO an approval-gate state, the orchestrator automatically:
- Creates a Linear issue titled `[<project>] <STATE> artifact` (e.g. `[memory-system-upgrade] BRIEF artifact`)
- Sets the issue description to the corresponding section from the project summary (## Brief, ## Architecture, ## Plan, ## Review)
- Assigns it to PA Ops Bot, state "In Progress"
- Stores the `artifact_issue_id` in the state history entry

When transitioning OUT of an approval-gate state (approved), the orchestrator automatically:
- Closes the artifact issue (sets to "Done")
- Updates the Linear project description with a short summary (255 char limit)
- Posts the full artifact section content as a Linear project update
- Stores the `project_update_id` in the state history entry

This provides a full audit trail in Linear: each approval gate has a trackable issue, the project description is updated with a summary, and the full artifact is posted as a project update.

## Workflow: New Project

1. Operator describes a project (or a Linear issue triggers it)
2. Run `init <name> --tier <tier>` - creates PROJECTS.yaml entry + summary stub
3. Fill in PROJECTS.yaml fields: repo, path, description, tags, linear_project_id
4. Write the brief in the project summary file (use `references/templates/brief.md`)
5. Run `transition <name> BRIEF`
6. Present brief to operator for approval
7. On approval, run `transition <name> PLAN` (or ARCHITECTURE for project tier)
8. Continue through states per the flow

## Workflow: Status Check

1. Run `status <name>` - returns current state, exit criteria, artifacts, valid transitions
2. Report to operator in plain language

## Workflow: Transition

1. Run `validate <name>` first - check exit code (0 = clear, 2 = missing artifacts)
2. If artifacts missing, complete them before transitioning
3. If approval gate, get operator confirmation
4. Run `transition <name> <TARGET>` - updates PROJECTS.yaml state + history AND syncs Linear project status automatically
5. Verify `linear_sync` in the response confirms sync succeeded
6. If sync failed, manually update Linear project status
7. Check `artifact_issue_id` in response - confirms a tracking issue was created for the new approval-gate state
8. Check `artifact_close` in response - confirms the previous approval-gate artifact was closed, description updated, and full artifact posted as project update
9. **MANDATORY: Spawn a PM subagent for the new state** (see "Project Manager Subagent" section). Every non-terminal state (not CLOSED, not CANCELED) must have a PM subagent running.

## Linear Enforcement Rules

**State sync is mandatory.** Every transition MUST result in the Linear project status being updated. The `transition` command does this automatically. If it fails, fix it before proceeding.

**Issue assignment in PLAN state:**
When creating Linear issues during planning, assign them to one of:
- **Bot user** (default): the configured Linear bot (e.g. PA Ops Bot) for tasks the agent will execute
- **Operator**: the human operator for tasks requiring human judgment or approval

Use `linear_integration.py create-issue --assignee <user-id>` to set assignee.

**Project updates:**
On approval gates, the orchestrator updates the project description (short summary, 255 char limit) and posts the full artifact as a project update. Use `linear_integration.py update-project-description --project-id <id> --body "short text"` for descriptions, or `post-project-update --project-id <id> --body "markdown content"` for full updates.

**Issue state tracking during BUILD:**
When completing a task during BUILD, update the Linear issue state to Done:
```bash
python3 scripts/linear_integration.py update-state --issue-id <id> --state Done
```
Do NOT leave issues in their original state after completing them. Update immediately.

## Linear State Mapping

The script returns `linear_state` on status/transition. Update Linear accordingly:

| Orchestrator State | Linear State |
|-------------------|-------------|
| INTAKE, BRIEF | Backlog |
| ARCHITECTURE | Todo |
| PLAN | In Progress |
| BUILD | In Dev |
| REVIEW | Review |
| SHIP | In Prod |
| CLOSED | Done |
| CANCELED | Canceled |

## Mandatory Pre-Close Verifications

Before transitioning from SHIP to CLOSED, two independent subagent verifications MUST pass. These are non-negotiable.

### 1. Test Verification (subagent)

Spawn a subagent that does NOT have access to the codebase. Provide it ONLY with the test output logs (copy-paste or file). The subagent must:
- Confirm tests actually ran (not skipped, not mocked to pass)
- Confirm the reported pass count matches the test output
- Flag any suspicious patterns (all tests pass in 0ms, no assertions, etc.)
- Return a verdict: PASS or FAIL with reasoning

The subagent must NOT have context about what the code does. It reviews test logs as an independent auditor.

```bash
# Example: capture test output, send to verification subagent
node --test tests/ 2>&1 > /tmp/test-output.txt
# Then spawn subagent with only the test output file
```

### 2. Linear Audit (subagent)

Spawn a subagent that queries all Linear issues in the project and verifies:
- Every issue has a final status (Done, Canceled, or explicitly deferred with comment)
- Every Done issue has a comment describing what was actually delivered
- No issues are stuck in intermediate states (In Dev, Review) without explanation
- Issue states match reality (if marked Done, the work is actually shipped)
- Project description and definition of done checkboxes are updated

The subagent reports: issues audited, issues with problems, specific problems found.

Both verifications must pass before `transition <name> CLOSED` is allowed. Include the verification reports in the project summary.

## Mandatory: API Schema Verification (BUILD)

**Never build against assumed API schemas.** Before writing integration code against any external API, you MUST:

1. **Make a real test call** to every endpoint you plan to use. Use curl, a script, or the product's SDK. Log the actual response.
2. **Compare against documentation.** If the actual response differs from docs (extra fields, different nesting, renamed fields), the actual response wins.
3. **Record in project summary** under `## Verified API Schemas`:
   ```
   ### <API Name> - <endpoint>
   - Verified: YYYY-MM-DD
   - Doc says: <shape from docs>
   - Actual: <shape from real call>
   - Differences: <list or "none">
   ```
4. **Re-verify after failures.** If an integration fails at runtime, the first step is always a fresh test call to check if the API shape changed.

This is an exit criterion for BUILD. The `validate` command checks for the `## Verified API Schemas` section.

**Why:** On 2026-03-28 we built a Linear webhook receiver against documented `data.botUserId` / `data.id` fields. The actual payload used `appUserId` (top-level) and `agentSession.id`. This cost hours of live debugging that one test call would have caught.

## Mandatory: Human E2E Testing (SHIP)

**"End-to-end" means the human triggers the real action and sees the result in the real product.** Not an API call. Not a mock. Not "the agent says it works."

Before `transition <name> CLOSED`, the operator must:

1. **Perform the user action** as a real user would (click a button, assign an issue, send a message, open a page)
2. **Observe the outcome** in the actual product UI (not in logs, not in a terminal)
3. **Report what happened** - what they did, what they saw, pass or fail

The agent documents this in `## Human E2E Test Report` in the project summary:
```
### Test: <what was tested>
- Tester: <operator name>
- Date: YYYY-MM-DD
- Action taken: <exact user action, e.g. "Assigned PA Ops Bot to MET-8605 in Linear UI">
- Expected result: <what should happen>
- Actual result: <what the operator saw>
- Verdict: PASS / FAIL
- Issues found: <list or "none">
```

Multiple test scenarios are encouraged. At minimum one happy-path E2E per major feature.

This is an exit criterion for SHIP. The `validate` command checks for the `## Human E2E Test Report` section.

**Why:** On the same project, "forwarding works" was declared success based on log output. The actual product (Linear's agent chat panel) showed nothing because the response-back auth was wrong. The human seeing the result in the product is the only reliable signal.

## Templates

Located in `references/templates/`. Copy content into the project summary file at the appropriate stage:

- `brief.md` - Use during BRIEF state
- `architecture.md` - Use during ARCHITECTURE state (project tier only)
- `plan.md` - Use during PLAN state
- `review-checklist.md` - Use during REVIEW state

## Inter-Agent Review Workflow

States with `approval_gate: true` (BRIEF, ARCHITECTURE, PLAN, REVIEW) require an inter-agent review before the transition becomes available for operator approval.

### The 4-Step Pattern

```
Producer → Critic (iterate up to 3 rounds) → PM sign-off → Operator approval
```

1. **Producer** spawns a subagent to write the artifact (architect writes arch doc, planner writes plan, etc.)
2. **Critic** spawns a subagent from a relevant discipline to review it (senior engineer, tech lead, QA)
3. **Iteration**: Producer and critic exchange responses tracked in a review file. Max 3 rounds.
4. **PM review**: PA (acting as PM) reads both subagent files, synthesizes, may request another round (up to the max). PM signs off when satisfied.
5. **Operator approval**: After PM sign-off, present the artifact + review summary to the operator as normal.

### Producer and Critic Roles by Stage

| Stage | Producer | Critic |
|-------|----------|--------|
| BRIEF | pa | domain_expert_subagent |
| ARCHITECTURE | architect_subagent | senior_engineer_subagent |
| PLAN | planner_subagent | tech_lead_subagent |
| REVIEW | reviewer_subagent | qa_subagent |

### Spawning Subagents

Spawn producer and critic as separate subagents. Brief each with:
- The project summary file path
- Their specific role and what to produce/critique
- The review file path where they write their output

### Critic task templates

Use these exact task descriptions when spawning critic subagents. Replace `[file]`, `[checklist-file]`, and `[review-file]` with actual paths.

**BRIEF critic (domain_expert_subagent):**
"You are a Domain Expert subagent reviewing a project brief. Read [file]. Your job: identify missing scope, wrong assumptions, unclear success criteria. Do NOT summarise the brief. Do NOT reproduce the project doc. Write only your critique. Rate each issue BLOCKING/SIGNIFICANT/MINOR. Write critique to [review-file] under ## Critic's Issues. Output structured summary of BLOCKING issues and verdict APPROVED or NEEDS_FIXES."

**ARCHITECTURE critic (senior_engineer_subagent):**
"You are a Senior Engineer subagent reviewing an architecture document. Read [file]. Your job: identify performance, reliability, and security risks. Do NOT summarise the document. Do NOT reproduce the project doc. Write only your critique. Rate each issue BLOCKING/SIGNIFICANT/MINOR. Write critique to [review-file] under ## Critic's Issues. Output structured summary of BLOCKING issues and verdict APPROVED or NEEDS_FIXES."

**PLAN critic (tech_lead_subagent):**
"You are a Tech Lead subagent reviewing a project plan. Read [file]. Your job: identify missing tasks, wrong sequencing, unrealistic estimates. Do NOT summarise the plan. Do NOT reproduce the project doc. Write only your critique. Rate each issue BLOCKING/SIGNIFICANT/MINOR. Write critique to [review-file] under ## Critic's Issues. Output structured summary of BLOCKING issues and verdict APPROVED or NEEDS_FIXES."

**REVIEW critic (qa_subagent):**
"You are a QA critic subagent reviewing a completed review checklist. Read [checklist-file] (this is the reviewer's checklist, NOT the project doc). Your job: identify what the reviewer missed, glossed over, or should have tested. Verify claims against actual system state where possible (check src/ vs dist/, run git log, check test output). Do NOT summarise the checklist or the project doc. Do NOT reproduce the project doc. Write only your critique appended to [checklist-file] under ## QA Critique. Rate each issue BLOCKING/SIGNIFICANT/MINOR. Output: list of BLOCKING issues and verdict APPROVED or NEEDS_FIXES."

### Tracking Iterations

Review files live in `projects/`:
```
projects/<name>-review-<stage>-<role>.md
```

Examples:
- `projects/my-feature-review-architecture-producer.md`
- `projects/my-feature-review-architecture-critic.md`

Use the template at `references/templates/inter-agent-review.md`. Both producer and critic write to the **same** review file (the template has sections for both).

Single review file per stage per round:
```
projects/<name>-review-<stage>-round<N>.md
```

### Detecting Agreement

Agreement is reached when, in the review file:
- No BLOCKING issues remain unresolved
- Both producer and critic have `Status: APPROVED` in the Sign-Off Block
- Run `python3 scripts/orchestrator.py review-status <name>` to check

### PM Review

After critic signs off, PA (as PM) reads the review file and the artifact:
1. Check: does the artifact meet the brief goals?
2. Check: no scope creep?
3. If yes: set `Status: APPROVED` in the PM sign-off block
4. If no: request another round (increment round counter, re-spawn producer with specific PM feedback)
5. Max 3 rounds total. If still unresolved after round 3, escalate to operator with issues listed.

### After PM Sign-Off

- Run `validate <name>` - it now checks for inter-agent sign-offs
- Present artifact + review summary to operator
- Operator approves → run `transition <name> <NEXT_STATE>`

### Checking Review Status

```bash
python3 scripts/orchestrator.py review-status <name>
```

Returns JSON with:
- `inter_agent_review_required`: whether current state needs review
- `review_files`: list of review files found
- `signed_off`: whether producer + critic both approved
- `pm_signed_off`: whether PM approved

## Exit Codes

- `0` - Success / valid / compliant
- `1` - Invalid transition or project not found
- `2` - Missing artifacts (validation failure), missing inter-agent sign-offs, or compliance violations found

## Project Manager Subagent

**MANDATORY: Every active project must have a PM subagent running.** When a project enters ANY non-terminal state (anything except CLOSED or CANCELED), the PA MUST spawn a PM subagent. This is not optional. Skipping this step is itself a process violation.

The PM subagent is the project's shepherd - it ensures every step is followed meticulously and minimizes operator wait time by unblocking non-critical decisions autonomously.

### How it works

The PM checker (`scripts/pm-checker.py`) runs all compliance checks for a project and returns a structured report with violations categorized as BLOCKING, SIGNIFICANT, or MINOR.

```bash
# Run compliance check
python3 scripts/pm-checker.py check <project-name>
python3 scripts/pm-checker.py check <project-name> --verbose
```

### What it checks

1. **Linear sync** - Project status in Linear matches the framework state. BLOCKING if mismatched, auto-fixable.
2. **Artifact issues** - Approval gate stages have artifact issues created in Linear. Previous stages have closed artifacts, and the project description reflects the latest approved artifact.
3. **Inter-agent review** - If current state requires review: are review files present? Has the producer responded to critic? Is the review stalled?
4. **BUILD progress** - Are Linear issues being worked? Any stuck in Backlog? Any stale (not updated in 4h)?
5. **SHIP requirements** - Are mandatory sections present (API schemas, Human E2E test report)?
6. **Summary completeness** - Does the project summary have all expected sections for states it has passed through?
7. **State staleness** - Has the project been stuck in a state beyond the threshold? (INTAKE: 1h, BRIEF/PLAN/ARCH: 24h, BUILD: 7d, REVIEW: 48h, SHIP: 3d)

### PM subagent behavior

**Spawn the PM subagent when entering any active state.** The PM subagent should:

1. Run `pm-checker.py check <name> --verbose` every 2-3 minutes
2. For **BLOCKING** violations with `auto_fixable: true`: fix them immediately (e.g., sync Linear status)
3. For **BLOCKING** violations without auto_fixable: alert the PA agent to take action
4. For **SIGNIFICANT** violations: alert once, don't re-alert for the same violation
5. For **MINOR** violations: log but don't alert unless they persist for >30 minutes

**Unblocking other subagents:** The PM subagent's second critical job is minimizing operator dependency. If any subagent (producer, critic, developer) asks a question or is stalled waiting for a decision that is NOT a formal approval gate, the PM makes the call. This includes:
- Choosing between equivalent implementation approaches
- Deciding on naming conventions or file structure
- Resolving ambiguous requirements that don't change scope
- Answering technical questions the PM can research (read files, check docs, search web)
- Breaking deadlocks between producer and critic (if they disagree on a non-blocking issue, PM decides)
- Providing missing context by reading project files, MEMORY.md, or other workspace docs

The ONLY things the PM subagent must NOT do:
- Approve formal approval gates (BRIEF, ARCHITECTURE, PLAN, REVIEW) - only the operator does that
- Skip or bypass any required step in the orchestrator workflow
- Suppress BLOCKING violations
- Make scope changes (adding/removing features, changing success criteria)
- Override explicit operator decisions already recorded in the project doc

**Process enforcement examples** (things the PM must catch):
- Agent tries to transition without Linear sync succeeding -> BLOCK, fix the sync first
- Agent skips inter-agent review -> BLOCK, review is mandatory
- Agent doesn't create artifact issue on approval gate entry -> BLOCK, create it
- Agent doesn't close previous artifact issue on transition -> BLOCK, close it
- Agent marks work "done" without updating Linear issue state -> flag and fix
- Agent hasn't started work 10+ minutes into a state with no review files -> alert

### Spawning pattern

```
# In the PA agent, after entering a new state:
sessions_spawn(
  task="You are a Project Manager subagent for the '<name>' project (currently in <STATE>).
    Run compliance checks every 2-3 minutes using:
    python3 /path/to/scripts/pm-checker.py check <name> --verbose

    Your responsibilities:
    1. Fix auto_fixable violations immediately (Linear sync, artifact issues)
    2. Alert on BLOCKING violations that need PA/operator attention
    3. Unblock other subagents by making non-critical decisions (anything that is NOT a formal approval gate)
    4. Monitor that every process step is followed exactly as specified in the orchestrator SKILL.md
    5. Report state: compliant/non-compliant + violation summary

    Stop when the project transitions to the next state (you'll be replaced by a new PM for that state).",
  runtime="subagent",
  mode="run",
  label="pm-<name>"
)
```

### Output format

```json
{
  "project": "my-project",
  "state": "BUILD",
  "compliant": false,
  "summary": {
    "blocking": 1,
    "significant": 2,
    "minor": 3,
    "auto_fixable": 1,
    "total": 6
  },
  "violations": [
    {
      "severity": "BLOCKING",
      "code": "LINEAR_STATUS_MISMATCH",
      "message": "Linear project status is 'Planned' but should be 'In Progress'",
      "action": "Sync Linear status to 'In Progress'",
      "auto_fixable": true
    }
  ]
}
```

## Design Workflow

Optional sub-phase for projects with user-facing UI. Runs within BRIEF (feature tier) or ARCHITECTURE (project tier) - does NOT add new states to the state machine.

**When to use:** Project involves screens/UI and benefits from upfront design before planning. Skip for patch tier or backend-only work.

**Quick start:**

```bash
# Generate design spec from a product brief
python3 scripts/design-producer.py \
  --brief projects/<name>-summary.md \
  --output-dir projects/<name>-design/ \
  --wireframes --verbose

# Generate wireframes standalone (if not using --wireframes above)
python3 scripts/wireframe-gen.py \
  --input projects/<name>-design/design-spec.json \
  --output-dir projects/<name>-design/wireframes/
```

**Pipeline steps:** DECOMPOSE -> DESIGN -> STITCH -> CRITIQUE -> REFINE

The 5-step LLM chain produces `design-spec.json` with: screens, flow_map, user_stories, edge_cases, design_notes, metadata. Wireframe SVGs are generated per-screen using stdlib only (zero external deps).

**Integration:** Design output becomes part of the approval gate artifact. Present to operator during BRIEF/ARCHITECTURE approval. Design decisions feed into PLAN task breakdown.

**Requirements:** `ANTHROPIC_API_KEY` env var, `anthropic` SDK. No dependency on design-studio.

See `references/design-workflow.md` for full process documentation and `references/templates/design-spec.md` for the human-readable template.
