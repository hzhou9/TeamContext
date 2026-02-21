# TeamContext Agent Workflow

Use these intent->command mappings in vibe coding sessions:

- User says: "save recent context to tc"
- Run: `tc save --auto-bootstrap-if-empty`

- User says: "sync latest context"
- Run: `tc sync --json`

Execution rule:
- Execute mapped commands immediately; do not only print command text.
- Only return command text without execution if user explicitly asks for command-only output.

Post-execution response contract:
- Include command, exit code, and key results from stdout.
Then summarize key deltas from JSON output for the user.
