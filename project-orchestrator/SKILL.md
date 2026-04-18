---
name: project-orchestrator
description: >
  Orchestrate tracked software projects with a deterministic lifecycle state
  machine, required artifacts, approval gates, review loops, and optional
  Linear synchronization. Use when: creating a new tracked project, checking
  project status, validating project artifacts, transitioning project state,
  planning project work, or enforcing a stage-based workflow for an entry in
  PROJECTS.yaml. NOT for: ad-hoc tasks, one-off questions, or work that is not
  being tracked as a project in the current OpenClaw workspace.
---

# Project Orchestrator

Use this skill to manage tracked software projects through explicit states. The scripts validate states, transitions, and required artifacts against `references/state-machine.yaml`.

## Workspace assumptions

Run from an OpenClaw workspace that uses `PROJECTS.yaml` and `projects/`.

Workspace detection order:
1. `OPENCLAW_WORKSPACE`
2. walk upward from the current directory looking for `PROJECTS.yaml`
3. fallback to `~/.openclaw/workspace`

## Quick reference

All commands output JSON.

```bash
python3 skills/project-orchestrator/scripts/orchestrator.py init <name> --tier <patch|feature|project>
python3 skills/project-orchestrator/scripts/orchestrator.py status <name>
python3 skills/project-orchestrator/scripts/orchestrator.py validate <name>
python3 skills/project-orchestrator/scripts/orchestrator.py transition <name> <TARGET_STATE>
python3 skills/project-orchestrator/scripts/orchestrator.py plan <name>
python3 skills/project-orchestrator/scripts/orchestrator.py review-status <name>
```

If you are already in the skill directory, `python3 scripts/orchestrator.py ...` also works.

## Tiers

Choose the tier at `INTAKE` based on scope.

| Tier | Token budget | Typical scope | Uses `ARCHITECTURE`? |
|------|--------------|---------------|----------------------|
| patch | < 50k | Small, local change | No |
| feature | 50k-300k | Contained feature in an existing system | No |
| project | > 300k | New repo or major architectural change | Yes |

Estimate in tokens, not time. When unsure, start with `feature`. Upgrade to `project` if major architecture work emerges during `BRIEF`.

## State flow

**patch / feature**
```text
INTAKE -> BRIEF -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

**project**
```text
INTAKE -> BRIEF -> ARCHITECTURE -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

**optional UI design path**
```text
INTAKE -> BRIEF -> DESIGN -> PLAN/ARCHITECTURE -> ...
```

Any state can transition to `CANCELED`.

## Approval gates

Approval gates exist at `BRIEF`, `ARCHITECTURE`, `PLAN`, and `REVIEW`.

At each gate:
1. Run `validate <name>`.
2. Confirm the required artifact and review files exist.
3. Present a concise summary to the human operator in the current working channel.
4. Wait for explicit approval.
5. Only then run `transition <name> <TARGET_STATE>`.

Never auto-approve. Never skip approval gates.

## New project workflow

1. Run `init <name> --tier <tier>`.
2. Update the project entry in `PROJECTS.yaml` with repo, path, description, tags, and any external IDs.
3. Write the current-stage artifact in `projects/<name>.md`.
4. Use the templates in `references/templates/`.
5. Run `transition <name> BRIEF` once intake is complete.
6. Continue state by state, validating before every transition.

## Status workflow

1. Run `status <name>`.
2. Read the current state, exit criteria, required artifacts, and valid transitions.
3. Report the result in plain language.

## Transition workflow

1. Run `validate <name>` first.
2. If validation fails, fix the missing artifact or review requirement before proceeding.
3. If the current state is an approval gate, get explicit human approval.
4. Run `transition <name> <TARGET_STATE>`.
5. Confirm the returned JSON shows the expected new state and follow-up actions.
6. If your workspace uses Linear, verify the sync fields in the response and correct failures immediately.

## PM relay watchdog, correct usage

When your runtime cannot keep a persistent PM session alive, use the PM relay helper as an explicitly scoped watchdog.

Recommended commands:

```bash
python3 skills/project-orchestrator/scripts/pm-relay-helper.py activate <name> --state <STATE>
python3 skills/project-orchestrator/scripts/pm-relay-helper.py list-active
python3 skills/project-orchestrator/scripts/pm-relay-helper.py sweep-active
python3 skills/project-orchestrator/scripts/pm-relay-helper.py deactivate <name>
```

Rules:
- track only projects that are actively in flight
- keep the active-project list as the source of truth for relay scope
- do not run global sweeps across every tracked project
- do not respawn a PM owner if a real blocker already explains the gap
- do not respawn into an operator-approval-only wait state
- keep idle watchdog runs silent, and only surface user-facing updates when the watchdog actually changes something meaningful

## Linear integration

Linear support is built into the scripts. Use it when your workspace tracks projects there.

### Commands

```bash
python3 skills/project-orchestrator/scripts/linear_integration.py create-project
python3 skills/project-orchestrator/scripts/linear_integration.py create-issue
python3 skills/project-orchestrator/scripts/linear_integration.py create-issues-from-plan
python3 skills/project-orchestrator/scripts/linear_integration.py update-state
python3 skills/project-orchestrator/scripts/linear_integration.py sync-state
python3 skills/project-orchestrator/scripts/linear_integration.py add-comment
python3 skills/project-orchestrator/scripts/linear_integration.py get-issue
python3 skills/project-orchestrator/scripts/linear_integration.py validate-transition
```

### Rules

- Keep orchestrator state and Linear state aligned if the project has a `linear_project_id`.
- Use your workspace's configured bot or service account for agent-owned issues.
- Assign human-only tasks to the human operator.
- When work is completed during `BUILD`, update the related Linear issue state immediately.

### State mapping

| Orchestrator state | Linear state |
|--------------------|--------------|
| INTAKE, BRIEF | Backlog |
| ARCHITECTURE | Todo |
| PLAN | In Progress |
| BUILD | In Dev |
| REVIEW | Review |
| SHIP | In Prod |
| CLOSED | Done |
| CANCELED | Canceled |

## Templates

Templates live in `references/templates/`.

- `brief.md` - `BRIEF`
- `design-spec.md` - `DESIGN`
- `architecture.md` - `ARCHITECTURE`
- `plan.md` - `PLAN`
- `review-checklist.md` - `REVIEW`
- `inter-agent-review.md` - approval-gate review loops

## Inter-agent review workflow

Approval-gate artifacts should go through a review loop before operator approval.

Pattern:

```text
producer -> critic -> project-manager review -> operator approval
```

### Recommended roles by stage

| Stage | Producer | Critic |
|-------|----------|--------|
| BRIEF | main agent or project lead agent | domain expert reviewer |
| ARCHITECTURE | architect agent | senior engineer reviewer |
| PLAN | planner agent | tech lead reviewer |
| REVIEW | reviewer agent | QA reviewer |

### Review process

1. Producer writes or updates the artifact.
2. Critic reviews it and records issues as `BLOCKING`, `SIGNIFICANT`, or `MINOR`.
3. Producer responds and revises.
4. Repeat up to 3 rounds.
5. A project-manager pass decides whether the artifact is ready for operator review.
6. Present the artifact and review summary to the operator.

### Review files

Store review files in `projects/` and use the template at `references/templates/inter-agent-review.md`.

Suggested naming:

```text
projects/<name>-review-<stage>-round<N>.md
```

Run:

```bash
python3 skills/project-orchestrator/scripts/orchestrator.py review-status <name>
```

Use the result to confirm whether review files exist and whether sign-off is complete.

## Pre-close verifications

Before moving from `SHIP` to `CLOSED`, require two independent checks.

### 1. Test verification

Have an independent reviewer inspect test output without codebase context.

The reviewer should confirm:
- tests actually ran
- the reported pass count matches the output
- there are no suspicious patterns such as empty suites or obviously fake success output

### 2. Delivery / tracker audit

Have an independent reviewer inspect the tracked project work and confirm:
- every issue has a final disposition
- delivered work has clear delivery notes
- nothing is stuck in an in-between state without explanation
- project summary and closeout sections are up to date

Include both reports in the project summary before closing the project.

## Mandatory: API schema verification during BUILD

Before writing integration code against an external API:

1. Make a real test call to each endpoint you plan to use.
2. Compare the real response to the docs.
3. Record the verified shape in `## Verified API Schemas` in the project summary.
4. Re-check the endpoint after any runtime schema-related failure.

Use this structure:

```text
### <API name> - <endpoint>
- Verified: YYYY-MM-DD
- Doc says: <shape from docs>
- Actual: <shape from real call>
- Differences: <list or "none">
```

Treat the actual response as authoritative.

## Mandatory: human end-to-end testing during SHIP

Before closing the project, require a real human to perform at least one real end-to-end test in the actual product.

Document it under `## Human E2E Test Report` in the project summary:

```text
### Test: <what was tested>
- Tester: <name>
- Date: YYYY-MM-DD
- Action taken: <exact user action>
- Expected result: <what should happen>
- Actual result: <what the tester observed>
- Verdict: PASS / FAIL
- Issues found: <list or "none">
```

Do not treat logs alone as proof of end-to-end success.

## Project-manager checks

If your runtime supports long-lived background work, keep a project-manager checker running for active projects. Otherwise run the checker manually before major transitions.

```bash
python3 skills/project-orchestrator/scripts/pm-checker.py check <project-name>
python3 skills/project-orchestrator/scripts/pm-checker.py check <project-name> --verbose
```

Use it to catch process drift such as:
- tracker state out of sync
- missing review files
- missing artifact issues
- stale build progress
- missing SHIP closeout sections
- incomplete project summaries

## Design workflow

Use the design workflow for UI-heavy projects when early screen and flow decisions will improve planning.

```bash
python3 skills/project-orchestrator/scripts/design-producer.py \
  --brief projects/<name>.md \
  --output-dir projects/<name>-design/ \
  --wireframes --verbose

python3 skills/project-orchestrator/scripts/wireframe-gen.py \
  --input projects/<name>-design/design-spec.json \
  --output-dir projects/<name>-design/wireframes/
```

API key resolution order:
1. `--api-key`
2. `ANTHROPIC_API_KEY`
3. OpenClaw `auth-profiles.json` if present

See `references/design-workflow.md` for details.

## Exit codes

- `0` - success / valid
- `1` - invalid request, transition, or missing project
- `2` - validation failure, missing artifacts, or missing review/sign-off requirements
