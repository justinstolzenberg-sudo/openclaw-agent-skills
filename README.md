# OpenClaw Agent Skills

Public skills for OpenClaw. Install on any instance by copying the skill directory into `~/.openclaw/workspace/skills/`.

## Available Skills

| Skill | Description |
|-------|-------------|
| [project-orchestrator](./project-orchestrator/) | Deterministic state machine for software project lifecycle. Enforces stage-based workflows with approval gates and artifact validation. |

## Installation

Copy a skill directory into your workspace:

```bash
# Clone the repo
git clone git@github.com:justinstolzenberg-sudo/openclaw-agent-skills.git /tmp/openclaw-agent-skills

# Copy the skill you need
cp -r /tmp/openclaw-agent-skills/project-orchestrator ~/.openclaw/workspace/skills/
```

Or symlink for easier local iteration:

```bash
ln -s /path/to/openclaw-agent-skills/project-orchestrator ~/.openclaw/workspace/skills/project-orchestrator
```

## Contributing

Each skill follows the OpenClaw AgentSkills spec:

```text
skill-name/
├── SKILL.md              # Required: frontmatter (name + description) + instructions
├── scripts/              # Optional: executable scripts
├── references/           # Optional: reference docs loaded on demand
└── assets/               # Optional: files used in output
```

Keep `SKILL.md` concise. Move detailed reference material to `references/`. Only include files the agent actually needs.
