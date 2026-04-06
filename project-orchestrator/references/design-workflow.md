# Design Workflow

Optional DESIGN sub-phase within BRIEF or ARCHITECTURE states. Use when the project involves user-facing screens and benefits from upfront UI/UX design before planning.

## When to Use

- **Project tier** with new UI - run during ARCHITECTURE state
- **Feature tier** with significant UI changes - run during BRIEF state
- **Patch tier** - skip entirely (no design phase needed)

Trigger: operator mentions "design", "wireframes", "screens", or the brief describes user-facing features that need layout decisions.

## Integration with State Machine

The design workflow does NOT add new states. It runs as a sub-process within an existing state:

```
BRIEF (or ARCHITECTURE)
  └── DESIGN sub-phase
      ├── Run design-producer.py with the brief
      ├── Review output with operator
      ├── Optionally generate wireframes
      └── Include design spec in the state's artifacts
```

The state machine transitions remain unchanged. Design output becomes part of the approval gate artifact for BRIEF or ARCHITECTURE.

## Process

### 1. Prepare the Brief

Ensure the project brief contains enough detail for design:
- Target users and their goals
- Key workflows to support
- Any constraints (mobile-first, dashboard style, etc.)
- Reference existing UI patterns if applicable

### 2. Run design-producer.py

```bash
python3 scripts/design-producer.py \
  --brief projects/<name>-summary.md \
  --output-dir projects/<name>-design/ \
  --wireframes \
  --verbose
```

This runs a 5-step LLM chain:

| Step | What it does |
|------|-------------|
| DECOMPOSE | Breaks brief into user roles, workflows, screen hierarchy |
| DESIGN | Generates per-screen component specs |
| STITCH | Creates flow map and navigation structure |
| CRITIQUE | Reviews for UX, accessibility, consistency issues |
| REFINE | Applies fixes and outputs final spec |

### 3. Review Output

The producer outputs `design-spec.json` with:
- `screens` - per-screen component specs
- `flow_map` - screen-to-screen transitions
- `user_stories` - generated user stories
- `edge_cases` - identified edge cases and handling
- `design_notes` - key design decisions
- `metadata` - model, timestamp, critique summary

### 4. Generate Wireframes (Optional)

If `--wireframes` was passed, SVG wireframes are auto-generated. Otherwise:

```bash
python3 scripts/wireframe-gen.py \
  --input projects/<name>-design/design-spec.json \
  --output-dir projects/<name>-design/wireframes/ \
  --viewport 1440x900
```

### 5. Fill Design Spec Template

Use `references/templates/design-spec.md` to create a human-readable design doc:

```bash
cp references/templates/design-spec.md projects/<name>-design-spec.md
```

Populate the template from the JSON output.

### 6. Include in Approval Gate

Add the design spec to the state's artifact set:
- Reference the design JSON and wireframes in the project summary
- Present to the operator during the approval gate
- Design decisions inform the PLAN state's task breakdown

## Artifacts Produced

| Artifact | Path | Purpose |
|----------|------|---------|
| Design JSON | `projects/<name>-design/design-spec.json` | Machine-readable full spec |
| Wireframes | `projects/<name>-design/wireframes/*.svg` | Visual screen layouts |
| Design Spec | `projects/<name>-design-spec.md` | Human-readable design doc |

## Constraints

- All LLM calls use the Anthropic SDK (sync only). Requires `ANTHROPIC_API_KEY`.
- Zero dependencies on design-studio skill. Prompts are self-contained.
- Wireframe generation has zero external dependencies (stdlib only).
- Design output is advisory - operator approves before it shapes planning.
