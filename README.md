# OpenClaw Agent Skills

Public, reusable skills for OpenClaw.

These directories are meant to be dropped into an OpenClaw workspace so the agent can load them as native skills.

## Available Skills

| Skill | Description |
|-------|-------------|
| [project-orchestrator](./project-orchestrator/) | Structured project lifecycle orchestration for tracked software work. Adds explicit states, approval gates, artifact validation, review loops, and optional Linear sync. |

## Install

Skills in this repo assume a standard OpenClaw workspace layout, usually at `~/.openclaw/workspace`.

### Option 1 - copy the skill

```bash
git clone git@github.com:justinstolzenberg-sudo/openclaw-agent-skills.git /tmp/openclaw-agent-skills
mkdir -p ~/.openclaw/workspace/skills
cp -r /tmp/openclaw-agent-skills/project-orchestrator ~/.openclaw/workspace/skills/
```

### Option 2 - symlink for local iteration

```bash
git clone git@github.com:justinstolzenberg-sudo/openclaw-agent-skills.git ~/src/openclaw-agent-skills
mkdir -p ~/.openclaw/workspace/skills
ln -s ~/src/openclaw-agent-skills/project-orchestrator ~/.openclaw/workspace/skills/project-orchestrator
```

## Repo Layout

```text
skill-name/
├── SKILL.md      # Agent-facing instructions and trigger metadata
├── README.md     # Human-facing overview and setup notes
├── scripts/      # Optional executable helpers
├── references/   # Optional reference docs and templates
└── assets/       # Optional output assets
```

## Notes

- `SKILL.md` is the source of truth for the agent.
- `README.md` is for humans installing or evaluating the skill.
- Keep skills lean. Put detailed examples and templates in `references/` rather than bloating `SKILL.md`.
