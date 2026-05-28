# Voice Loop Markdown Skills

Skills are Markdown folders under `agent_skills/`:

```text
agent_skills/
  my-skill/
    SKILL.md
```

`SKILL.md` must start with simple YAML-style frontmatter:

```markdown
---
name: my_skill
description: Use this when ...
action: markdown
---

# My Skill

Instructions for the assistant.
```

Supported `action` values:

- `markdown` - returns the Markdown instructions to the model.
- `calculate` - built-in safe calculator action.
- `time` - built-in current date/time action.
- `searxng` - searches the configured local SearXNG instance.
- `vision` - special live camera context action used by DRIA's vision loop.

This keeps skill definitions in Markdown, Claude/OpenCode style, while still
allowing safe local built-ins when a skill needs real local execution.

The FastRTC bridge loads these folders at runtime, injects the skill catalogue
into realtime session instructions, exposes them from `GET /agent/skills`, and
registers executable skills as Realtime tools when the backend supports tool
calls.
