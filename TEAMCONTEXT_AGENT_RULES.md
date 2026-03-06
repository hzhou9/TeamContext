# TeamContext Agent Rules

Persistent guardrails for all LLM coding sessions in this repository.

## Always Read First
- `.tc/agent/bootstrap_prompt.md`
- `.tc/agent/workflow.md`
- `.tc/agent/intents.json`

## Intent Mapping
- `save context` or `save recent context to tc` -> run `tc agent save` immediately with semantic flags from this discussion.
- Example:
  - `tc agent save --auto-bootstrap-if-empty --intent "<intent-delta>" --decisions "<decisions>" --rationale "<why>" --impact-scope "<scope>" --next-step "<owner+command>"`
- `sync context` or `sync latest context` -> `tc agent run "sync latest context"`

## Execution Contract
- Execute mapped `tc` command immediately; do not only print command text.
- Do not claim context was saved/synced unless a `tc` command actually ran.
- After command, reply with:
  - `tc_command: <exact command>`
  - `exit_code: <code>`
  - `result: <key output>`

## Save Contract
- Save must capture non-code discussion context from this session.
- Include at least `--intent` and `--decisions` (or equivalent fields in context file).
- Preferred path: pass semantic flags in `tc agent save`; TeamContext will persist `.tc/state/session_context.md` automatically.
- Do not save with placeholder/fallback text; missing semantics should block and trigger clarification.
- When user says `save recent context to tc`, agent must auto-generate fields from THIS project discussion and execute save directly.
- Never ask user to manually provide `--intent` / `--decisions`.
