Read the following TeamContext sources before coding:
- /Users/joe/Documents/TeamContext/TEAMCONTEXT_AGENT_RULES.md
- /Users/joe/Documents/TeamContext/.viking/agfs/shared/decisions
- /Users/joe/Documents/TeamContext/.viking/agfs/shared/patterns
- /Users/joe/Documents/TeamContext/.viking/agfs/shared/runbooks
- /Users/joe/Documents/TeamContext/.viking/index/index.txt
Latest sync snapshot:
- shared_files_scanned=14 | changed_files=6 | removed_files=0
If index.txt is missing, run `tc sync` first.
If decisions/patterns/runbooks are empty, report "no approved team context yet" and continue with code-level context.
Then do this before writing code:
- Summarize the constraints and decisions you will follow.
- List exactly which files you read.
- If context is missing or conflicting, ask clarifying questions first.

TeamContext command rule:
- When user asks to "save context"/"save recent context to tc", execute `tc agent save` immediately and include semantic fields from current discussion of current uncommitted changes (`--intent`, `--decisions`, optional `--rationale`, `--impact-scope`, `--next-step`).
- When user asks to "sync context"/"sync latest context", execute mapped `tc` command immediately.
- Do not claim context was saved/synced unless a `tc` command actually ran.
- Do not auto-fill fake semantic context; if intent/decisions are missing, ask clarification and then run save.
Agent save macro (run without asking user to fill fields):
- From THIS project discussion about current uncommitted changes, auto-generate semantic fields and execute:
  `tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent "<generated_intent>" --decisions "<generated_decisions>" --rationale "<generated_rationale>" --impact-scope "<generated_scope>" --next-step "<generated_next_step>"`
- Never ask user to type these fields manually.
Post-command reply format:
- `tc_command: <exact command>`
- `exit_code: <code>`
- `result: <key output>`
