# Project Orchestrator

Deterministic state machine for software project lifecycle management. Enforces tier-based workflows with approval gates, multi-layer inter-agent review loops, and Linear state sync.

## What it does

Tracks projects through a fixed state flow:

```
INTAKE -> BRIEF -> [DESIGN] -> [ARCHITECTURE] -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

- **DESIGN** is optional - for UI-heavy projects. Skipped for backend-only, infra, CLI, library work.
- **ARCHITECTURE** is project-tier only. Feature/patch tiers skip it.
- **Tiers** determine scope: patch (< 50k tokens), feature (50k-300k tokens), project (> 300k tokens)
- **Approval gates** at BRIEF, ARCHITECTURE, PLAN, REVIEW - the agent cannot skip these
- **Inter-agent review** at every approval gate - artifacts are challenged by a critic before the operator sees them
- **Linear integration** keeps project and issue states in sync with the framework states
- **PROJECTS.yaml** is the single source of truth for project state

## Feedback Loops

The framework enforces multiple layers of quality control before anything reaches the operator for approval. At each approval-gate state, three nested loops run:

```
┌─────────────────────────────────────────────────────────────────────┐
│  OPERATOR APPROVAL LOOP (outer)                                     │
│  Operator reads PM summary → approves or requests changes           │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  PM REVIEW LOOP (middle)                                      │  │
│  │  PA reads agreed artifact → may trigger another iteration     │  │
│  │  (max 3 rounds before escalating to operator anyway)          │  │
│  │                                                               │  │
│  │  ┌─────────────────────────────────────────────────────────┐  │  │
│  │  │  INTER-AGENT DIALOGUE (inner)                           │  │  │
│  │  │  Producer subagent creates artifact                     │  │  │
│  │  │      ↓                                                  │  │  │
│  │  │  Critic subagent challenges it (BLOCKING / SIGNIFICANT  │  │  │
│  │  │      / MINOR issues, each rated accept/reject/partial)  │  │  │
│  │  │      ↓                                                  │  │  │
│  │  │  Producer responds, revises artifact                    │  │  │
│  │  │      ↓                                                  │  │  │
│  │  │  Both sign off on final agreed version                  │  │  │
│  │  └─────────────────────────────────────────────────────────┘  │  │
│  │                                                               │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Per-state roles

| State | Producer | Critic | PM |
|-------|----------|--------|----|
| BRIEF | PA | Domain expert | PA |
| DESIGN | Design subagent (design-producer.py) | UX critic subagent | PA |
| ARCHITECTURE | Architect subagent | Senior engineer subagent | PA |
| PLAN | Planner subagent | Tech lead subagent | PA |
| REVIEW | Reviewer subagent | QA subagent | PA |

### Inter-agent dialogue protocol

1. **Producer** writes the artifact (architecture doc, plan, etc.) to the project summary file
2. **Critic** reads the artifact and raises issues rated BLOCKING / SIGNIFICANT / MINOR
3. **Producer** responds: ACCEPT / REJECT / PARTIAL for each issue, revises artifact
4. Both write a sign-off to `projects/<name>-review-<stage>-<role>.md`
5. **PA (PM role)** reads both files, synthesizes, may request another iteration (max 3 rounds)
6. PA presents to operator for final approval

The `validate` command fails (exit 2) if the current state requires inter-agent review and sign-offs are missing. The `review-status` command shows the current state of review files.

### Why this matters

Without a critic loop, every artifact goes straight to the operator with no pre-filtering. Common failure modes that the inner loop catches before they reach the operator:

- Architecture: timing constraint violations, concurrency bugs, unhandled failure modes
- Plan: missing tasks, wrong sequencing, underestimated complexity
- Review: untested edge cases, missing rollback steps, incomplete acceptance criteria

The PM loop (middle) catches cases where the critic and producer agreed on something that still doesn't match the brief - the PA checks alignment before presenting to the operator.

## State machine

### Tier flows

**patch (no UI):**
```
INTAKE → BRIEF → PLAN → BUILD → REVIEW → SHIP → CLOSED
```

**feature (no UI):**
```
INTAKE → BRIEF → PLAN → BUILD → REVIEW → SHIP → CLOSED
```

**feature (with UI):**
```
INTAKE → BRIEF → DESIGN → PLAN → BUILD → REVIEW → SHIP → CLOSED
```

**project (no UI):**
```
INTAKE → BRIEF → ARCHITECTURE → PLAN → BUILD → REVIEW → SHIP → CLOSED
```

**project (with UI):**
```
INTAKE → BRIEF → DESIGN → ARCHITECTURE → PLAN → BUILD → REVIEW → SHIP → CLOSED
```

Any state can transition to `CANCELED`.

### Approval gates

States with `approval_gate: true`: **BRIEF, ARCHITECTURE, PLAN, REVIEW**

At each gate:
1. Run `validate <name>` - confirms artifacts exist and inter-agent sign-offs are present
2. PA presents summary to operator
3. Operator approves (or requests changes)
4. Run `transition <name> <TARGET_STATE>`

Never auto-approve. Never skip gates.

### Linear state mapping

| Framework State | Linear State |
|----------------|-------------|
| INTAKE, BRIEF, DESIGN | Backlog |
| ARCHITECTURE | Todo |
| PLAN | In Progress |
| BUILD | In Dev |
| REVIEW | Review |
| SHIP | In Prod |
| CLOSED | Done |
| CANCELED | Canceled |

## Design Phase (optional)

Full optional state in the orchestrator lifecycle for UI-heavy projects. Runs after BRIEF, before ARCHITECTURE (project tier) or PLAN (feature tier). Skipped for backend-only, infra, CLI, and library projects - the operator decides at BRIEF approval.

Uses a 5-step LLM prompt chain (DECOMPOSE -> DESIGN -> STITCH -> CRITIQUE -> REFINE) to produce structured screen specs, flow maps, wireframes, and a design critique. Output feeds directly into the next phase as input constraints.

**Inter-agent review:** Design subagent (producer) runs `design-producer.py`, then UX critic subagent reviews the output for accessibility, consistency, edge cases, and missing states. Same inner/middle/outer feedback loop as other approval gates.

### `scripts/design-producer.py`

Runs: DECOMPOSE -> DESIGN -> STITCH -> CRITIQUE -> REFINE

```bash
python3 scripts/design-producer.py --brief <file> [--output-dir <dir>] [--model claude-sonnet-4-6] [--wireframes] [--verbose]
```

**API key sourcing** (in order):
1. `--api-key` flag
2. `ANTHROPIC_API_KEY` env var
3. Auto-detected from OpenClaw `auth-profiles.json` (`~/.openclaw/agents/main/agent/auth-profiles.json`)

OAuth tokens (`sk-ant-oat*`) are auto-detected - the script adds the required Bearer auth and Claude Code identity headers automatically. No manual key configuration needed on OpenClaw instances.

**Output:** JSON with `screens`, `flow_map`, `user_stories`, `edge_cases`, `design_notes`, `metadata`.

### `scripts/wireframe-gen.py`

Generates SVG wireframes from design spec JSON. 12 component types, zero external dependencies.

```bash
python3 scripts/wireframe-gen.py --input <spec.json> [--output-dir <dir>] [--viewport 1440x900]
```

See `references/design-workflow.md` for full process documentation.

## Scripts

### `scripts/orchestrator.py`

Core state machine. Commands:

```bash
python3 scripts/orchestrator.py init <name> --tier <patch|feature|project> [--display-name "..."] [--description "..."]
python3 scripts/orchestrator.py status <name>
python3 scripts/orchestrator.py validate <name>
python3 scripts/orchestrator.py transition <name> <TARGET_STATE>
python3 scripts/orchestrator.py plan <name>
python3 scripts/orchestrator.py review-status <name>
```

Run from workspace root. All output is JSON. Exit codes: 0 = success, 1 = invalid, 2 = missing artifacts/sign-offs.

`init` can create a Linear project and save `linear_project_id` to PROJECTS.yaml when the required Linear environment variables are set.

### `scripts/linear_integration.py`

Linear API integration. Commands: `create-project`, `create-issue`, `create-issues-from-plan`, `update-state`, `sync-state`, `add-comment`, `get-issue`, `validate-transition`.

Requires `LINEAR_TOKEN` or `LINEAR_API_TOKEN` in the environment. Optional IDs such as team, lead, and default assignee should also be provided via environment variables.

## Review artifacts

Review files live in `projects/` alongside the project summary:

```
projects/
  my-project.md                          # main summary (brief + architecture + plan)
  my-project-review-architecture-engineer.md   # senior engineer critique
  my-project-review-architecture-architect.md  # architect response + sign-off
```

Template: `references/templates/inter-agent-review.md`

## Templates

| Template | Used at |
|----------|---------|
| `brief.md` | BRIEF state |
| `design-spec.md` | DESIGN state |
| `architecture.md` | ARCHITECTURE state |
| `plan.md` | PLAN state |
| `review-checklist.md` | REVIEW state |
| `inter-agent-review.md` | Every approval-gate review dialogue |

## Pre-close verification

Before transitioning SHIP → CLOSED, two independent subagent audits are required:

1. **Test audit** - independent subagent reviews test output logs (no codebase access). Confirms tests actually ran, pass count is real, no suspicious patterns.
2. **Linear audit** - subagent queries all Linear issues, confirms every issue has a final state with delivery comment.

Both must pass. Include audit reports in the project summary before closing.

## Installation

```bash
# Copy (one-time)
cp -r project-orchestrator ~/.openclaw/workspace/skills/

# Or symlink (stays up to date when repo is pulled)
ln -s /path/to/openclaw-agent-skills/project-orchestrator ~/.openclaw/workspace/skills/project-orchestrator
```
