# Project Orchestrator

Structured project lifecycle orchestration for OpenClaw.

This skill adds a deterministic state machine around tracked software projects so work moves through explicit stages, required artifacts, review loops, and approval gates instead of ad-hoc status changes.

## What it is for

Use this skill when you want OpenClaw to manage a real project in a workspace that already tracks projects in `PROJECTS.yaml`.

It is most useful when you want:

- predictable stage transitions
- explicit approval gates before major work proceeds
- artifact validation before transitions
- structured producer/critic review loops
- optional Linear project and issue sync
- a clear paper trail in `projects/<name>.md`

It is not a general task manager for one-off questions or untracked work.

## Lifecycle

```text
INTAKE -> BRIEF -> [DESIGN] -> [ARCHITECTURE] -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

Rules:

- `DESIGN` is optional and intended for UI-heavy work.
- `ARCHITECTURE` is only used for `project` tier work.
- Any state can transition to `CANCELED`.
- Approval gates exist at `BRIEF`, `ARCHITECTURE`, `PLAN`, and `REVIEW`.

### Tier flows

**patch**
```text
INTAKE -> BRIEF -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

**feature**
```text
INTAKE -> BRIEF -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

**feature with UI design**
```text
INTAKE -> BRIEF -> DESIGN -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

**project**
```text
INTAKE -> BRIEF -> ARCHITECTURE -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

**project with UI design**
```text
INTAKE -> BRIEF -> DESIGN -> ARCHITECTURE -> PLAN -> BUILD -> REVIEW -> SHIP -> CLOSED
```

## Workspace expectations

This skill assumes a standard OpenClaw workspace with at least:

- `PROJECTS.yaml`
- `projects/`
- `skills/project-orchestrator/`

The scripts auto-detect the workspace by:

1. `OPENCLAW_WORKSPACE`
2. walking upward from the current directory looking for `PROJECTS.yaml`
3. falling back to `~/.openclaw/workspace`

## Prerequisites

### Required

- Python 3.10+
- an OpenClaw workspace using `PROJECTS.yaml`

### Optional

- Linear credentials if you want project and issue sync
- `anthropic` Python package plus an Anthropic API key if you want to use the design generator
- a runtime that can launch subagents if you want the full producer/critic review workflow

## Install

From your workspace root:

```bash
mkdir -p ~/.openclaw/workspace/skills
cp -r /path/to/openclaw-agent-skills/project-orchestrator ~/.openclaw/workspace/skills/
```

Or symlink it:

```bash
ln -s /path/to/openclaw-agent-skills/project-orchestrator ~/.openclaw/workspace/skills/project-orchestrator
```

## Core commands

Run these from the workspace root.

```bash
python3 skills/project-orchestrator/scripts/orchestrator.py init <name> --tier <patch|feature|project>
python3 skills/project-orchestrator/scripts/orchestrator.py status <name>
python3 skills/project-orchestrator/scripts/orchestrator.py validate <name>
python3 skills/project-orchestrator/scripts/orchestrator.py transition <name> <TARGET_STATE>
python3 skills/project-orchestrator/scripts/orchestrator.py plan <name>
python3 skills/project-orchestrator/scripts/orchestrator.py review-status <name>
```

All commands return JSON.

## Typical workflow

1. Initialize a tracked project with `init`.
2. Fill in the project entry in `PROJECTS.yaml`.
3. Write the current-stage artifact in `projects/<name>.md` using the provided templates.
4. Run `validate` before every approval gate or transition.
5. Use `transition` to move to the next valid state.
6. Keep review files and audit artifacts alongside the project summary.

## Review model

The recommended pattern at approval gates is:

```text
producer -> critic -> project-manager review -> operator approval
```

In practice that means:

- one agent or subagent produces the artifact
- a second agent critiques it and raises issues
- a coordinating project-manager pass checks whether the result is ready
- the human operator gives final approval

The repository includes a reusable review template at:

```text
references/templates/inter-agent-review.md
```

## Linear integration

Linear support is built in, but it is a workflow choice rather than a documentation prerequisite.

Relevant helpers:

```bash
python3 skills/project-orchestrator/scripts/linear_integration.py create-project
python3 skills/project-orchestrator/scripts/linear_integration.py create-issue
python3 skills/project-orchestrator/scripts/linear_integration.py create-issues-from-plan
python3 skills/project-orchestrator/scripts/linear_integration.py update-state
python3 skills/project-orchestrator/scripts/linear_integration.py sync-state
```

Set the required Linear environment variables in the workspace where you run the scripts.

## Optional design workflow

For UI-heavy projects, the skill includes a design generator and SVG wireframe generator.

```bash
python3 skills/project-orchestrator/scripts/design-producer.py --brief <file> --output-dir <dir> --wireframes
python3 skills/project-orchestrator/scripts/wireframe-gen.py --input <spec.json> --output-dir <dir>
```

The design generator looks for an API key in this order:

1. `--api-key`
2. `ANTHROPIC_API_KEY`
3. an OpenClaw `auth-profiles.json` if present

## Included files

```text
project-orchestrator/
├── SKILL.md
├── README.md
├── scripts/
│   ├── orchestrator.py
│   ├── linear_integration.py
│   ├── pm-checker.py
│   ├── design-producer.py
│   └── wireframe-gen.py
├── references/
│   ├── state-machine.yaml
│   ├── design-workflow.md
│   └── templates/
└── tests/
```

## Notes on strictness

This skill is intentionally opinionated.

It favors:

- explicit states over informal progress updates
- validation over trust
- review loops over single-pass artifacts
- operator approval over silent transitions

If that is the operating model you want, this skill is a good fit.
