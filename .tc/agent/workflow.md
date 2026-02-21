# TeamContext Agent Workflow

Use these intent->command mappings in vibe coding sessions:

- User says: "save recent context to tc"
- Run: `tc save`

- User says: "sync latest context"
- Run: `tc sync --json`

Then summarize key deltas from JSON output for the user.
