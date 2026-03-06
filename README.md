# TeamContext

TeamContext is a Git-native context collaboration CLI for vibe coding teams.
> TeamContext is using itself for context collaboration.

## Why This Exists

When multiple developers use LLM coding tools on the same project, context drifts fast:
- Architecture assumptions split across chat sessions
- Decisions made by the founder are lost for later-joined members
- Parallel branches evolve with different "truths"

TeamContext treats context like code:
- Versioned in Git
- Reviewable in pull requests
- Synced across members with `git pull` + `tc sync`

This makes founder-to-new-member handoff and parallel collaboration reproducible instead of chat-history dependent.

## Why It Matters

As vibe coding becomes a default way to build software, context sharing becomes core engineering infrastructure.
Without shared context, team velocity collapses into re-explaining decisions and fixing integration mismatches.
With TeamContext, teams can keep a consistent project memory across people, branches, and LLM tools.

## What Problems It Solves

- Multiple members working on the same vibe coding project in parallel
- Passing founder context to members who join later
- Keeping context synchronized between Codex/Claude sessions across the team
- Reducing regressions caused by missing architectural constraints

## Engine

TeamContext uses OpenViking as its context engine:
- OpenViking repo: https://github.com/volcengine/OpenViking
- TeamContext vendors and pins OpenViking under `.tc/vendor/openviking`
- OpenViking powers indexing/integration while TeamContext manages the Git-native workflow and shared file layout

## Using With Codex/Claude

TeamContext is designed to be called directly by vibe coding agents.
Operating rule: humans should run only `tc init`; all other TeamContext operations should be executed by the LLM agent.

### Install TeamContext For A Project

TeamContext (this repo) is a CLI tool. Your application repo (for example `projectA`) stays separate.

1. Pull this repo to install TeamContext CLI (preferred for PATH availability):
   ```bash
   pipx install .
   ```
   Development option:
   ```bash
   pip install -e .
   ```
   If you use the development option, activate the same Python environment before running `tc`.
2. Move to your target project repo:
   ```bash
   cd /path/to/projectA
   ```
3. Initialize TeamContext inside that project:
   ```bash
   tc init
   ```
   Fallback if `tc` is not on PATH:
   ```bash
   python -m teamcontext.cli init
   ```

After `tc init`, TeamContext creates:
- `.tc/agent/bootstrap_prompt.md`
- `.tc/agent/workflow.md`
- `.tc/agent/intents.json`
- `TEAMCONTEXT_AGENT_RULES.md` (project-root persistent guardrails to reduce memory drift across long sessions)

These files tell your coding tool how to map user intents to TeamContext commands.
After init, the default operating mode is Agent mode (tool runs `tc` commands). Use manual commands only as fallback.
Execution rule: when an intent matches, execute the mapped `tc` command immediately (do not only print command text).
For strict execution without tool-side rewriting, you can run intents via:
```bash
tc agent run "sync latest context"
```
Alias intents are also supported:
- `save context` -> `save recent context to tc`
- `sync context` -> `sync latest context`
After execution, require the tool to report:
- `tc_command: <exact command>`
- `exit_code: <code>`
- `result: <key output>`

### Scenario 1: Founder sets up TeamContext in an existing project

1. In the project root, run:
   ```bash
   tc init
   ```
   `tc init` now runs an initial sync by default (equivalent to `tc sync --json`) and writes the latest sync snapshot into `.tc/agent/bootstrap_prompt.md`.
2. Open your coding tool and paste this file as the first system/session instruction:
   ```bash
   .tc/agent/bootstrap_prompt.md
   ```
3. Save initial context through TeamContext:
   - Agent mode: tell your tool:
   ```bash
   save initial context to tc
   ```
   (tool should run `tc agent save` with semantic fields from the discussion, for example:)
   ```bash
   tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent "<intent-delta>" --decisions "<decisions>" --rationale "<why>" --impact-scope "<scope>" --next-step "<owner+command>"
   ```
   The tool must auto-generate these fields from THIS project discussion. User should not be asked to fill them manually.
   TeamContext will auto-write `.tc/state/session_context.md` from those fields.
   - Manual fallback:
   ```bash
   tc save --intent "<intent-delta>" --decisions "<decisions>"
   ```
   - For an existing project with prior progress, run a one-time baseline capture:
   ```bash
   tc agent save --changes-source uncommitted --bootstrap --intent "<intent-delta>" --decisions "<decisions>" --rationale "<why>"
   ```
   `tc save` requires semantic context (at least `--intent` and `--decisions`, or equivalent context file content).
   If baseline capture is very large, TeamContext blocks it by default. To proceed intentionally:
   ```bash
   tc agent save --changes-source uncommitted --bootstrap --force-large-save --intent "<intent-delta>" --decisions "<decisions>"
   ```
4. Commit and push to your Git remote:
   ```bash
   git add .tc .viking/agfs/shared
   git commit -m "teamcontext: initialize shared context"
   git push
   ```

### Scenario 2: New member joins and syncs founder context

1. Clone or pull the latest repository state:
   ```bash
   git pull
   ```
2. Run one-time local TeamContext setup:
   ```bash
   tc init
   ```
3. Open your coding tool and paste this file as the first system/session instruction:
   ```bash
   .tc/agent/bootstrap_prompt.md
   ```
4. Sync latest shared context:
   - Agent mode: tell your tool:
   ```bash
   sync latest context
   ```
   (tool should run `tc agent run "sync latest context"`)
   - Manual fallback (only if needed):
   ```bash
   tc sync --json
   ```
   In manual fallback, paste the JSON output into your coding tool so it can ingest the latest context deltas.
5. Run the preflight confirmation prompt (below) before coding.

### Scenario 3: Parallel work across multiple members

1. Each member codes with their preferred LLM tool, using the bootstrap prompt at session start.
2. Before push, publish recent context:
   - Agent mode: tell your tool:
   ```bash
   save recent context to tc
   ```
   (tool should run `tc agent save` with semantic fields from the current discussion):
   ```bash
   tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent "<intent-delta>" --decisions "<decisions>" --rationale "<why>" --impact-scope "<scope>" --next-step "<owner+command>"
   ```
   The tool must auto-generate these fields from THIS project discussion. User should not be asked to fill them manually.
   - Manual fallback (only if needed):
   ```bash
   tc save --intent "<intent-delta>" --decisions "<decisions>"
   ```
3. Each member pushes their updates:
   ```bash
   git add .viking/agfs/shared .tc
   git commit -m "<topic>"
   git push
   ```
4. Everyone periodically syncs others' context:
   ```bash
   git pull
   ```
   - Agent mode: tell your tool:
   ```bash
   sync latest context
   ```
   (tool should run `tc agent run "sync latest context"`)
   - Manual fallback (only if needed):
   ```bash
   tc sync --json
   ```
   In manual fallback, paste the JSON output into your coding tool.

### Preflight Confirmation Prompt

Use this before coding to verify context is loaded correctly:

```text
Before coding, list:
1) the constraints/decisions you will follow from TeamContext,
2) the exact files you read (full paths).
If anything is missing or conflicting, ask questions first.
```

## Install

```bash
pip install -e .
```

## Commands

```bash
tc init
tc agent run "sync latest context"
tc verify-context --latest 1
tc sync
tc sync --json
tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent "<intent-delta>" --decisions "<decisions>"
tc status
tc commit --topic "auth-refactor" --summary "Refined auth service boundaries"
tc doctor
tc vendor upgrade --ref v0.2.0
```

`tc sync` also prints a bootstrap prompt in human mode, and `tc sync --json` returns structured data for agent mode.
`tc sync` also writes machine-readable output to `.tc/state/last_sync.json` every time (with or without `--json`).
Use `tc verify-context` before push to ensure recent changelog entries contain non-`n/a` semantic context fields.

First-run note:
- In a brand-new project, `decisions/`, `patterns/`, and `runbooks/` may be empty. This is expected.
- In that case, the tool should report "no approved team context yet" and continue with repository code context.

## Run Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
