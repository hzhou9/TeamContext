# TeamContext Agent Workflow

Use these intent->command mappings in vibe coding sessions:

- User says: "save recent context to tc"
- Summarize this session into semantic flags, then run:
  `tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent "<intent-delta>" --decisions "<decisions>" --rationale "<why>" --impact-scope "<scope>" --next-step "<owner+command>"`
- TeamContext will auto-write `.tc/state/session_context.md` from provided flags.
- Required behavior: auto-generate all semantic fields from current project discussion.
- Do not ask user to provide `--intent`/`--decisions` values.

- User says: "save context"
- Treat as alias of `save recent context to tc` and run the same `tc agent save ...` command with semantic flags.

- User says: "sync latest context"
- Run: `tc agent run "sync latest context"`

- User says: "sync context"
- Treat as alias of `sync latest context` and run: `tc agent run "sync latest context"`

Strict intent router compatibility:
- `tc agent run "save recent context to tc"` only works if mapped command already includes semantic fields or a valid context file.
- If placeholders are present (e.g. `<intent-delta>`), replace with generated values before execution.

Execution rule:
- Execute mapped commands immediately; do not only print command text.
- Do not switch to unrelated tools/commands when intent is context save/sync.
- Only return command text without execution if user explicitly asks for command-only output.

Post-execution response contract:
- Include exact command, exit code, and key results from stdout.
- Use this strict format:
  - tc_command: <exact command>
  - exit_code: <code>
  - result: <key output>
Then summarize key deltas for the user.
