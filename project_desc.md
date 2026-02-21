# TeamContext — Project Description

TeamContext is a **Git-native team context collaboration framework** that helps teams share and reuse high-quality “working context” across different LLM coding tools (e.g., Codex, Cursor/Claude Code, local agents). It turns conversations, decisions, patterns, and runbooks into **versioned, reviewable artifacts** that can be synced with normal `git pull/push`.

TeamContext is designed to be **language-agnostic** (works for Python/Node/C++/Go/etc. projects) because it is a **developer tool**, not a runtime dependency. It uses **OpenViking as the default context engine** via a **vendored + pinned** integration (import API + lock), ensuring deterministic behavior across the team while keeping the project repository clean.

---

## Why TeamContext

### The problem
In team “vibe coding,” each developer and each LLM tool tends to build a separate implicit world model:
- Different assumptions about architecture, constraints, and APIs
- Conflicting “truths” embedded in chat history
- Context gets lost between tools and sessions
- Teams waste time reconciling mismatched designs

### The goal
Make team context behave like code:
- **Versioned** (history and diffs)
- **Reviewable** (publish gate)
- **Syncable** (via git pull/push)
- **Searchable** (local index + retrieval)
- **Composable** (decisions/patterns/runbooks/changelog)

---

## Core Principles

1. **Git-native workflow**
   - Team members use their own Git habits and flags.
   - TeamContext **never calls git**. It only produces files and indexes.

2. **Shared-in-repo, raw-local by default**
   - **Shared context** is committed to the main repo.
   - **Raw sessions** (full chat logs) stay local by default to reduce noise and risk.

3. **Pinned engine for deterministic collaboration**
   - OpenViking is cloned into a local vendor directory and pinned to a specific commit via a lock file.
   - Everyone uses the same engine version, avoiding “works on my machine” context drift.

4. **Append-only and sharded storage**
   - Avoid merge conflicts by writing new files (or append-only files) per user/session/day rather than editing giant shared documents.

5. **Index is local**
   - Vector index is **not committed**.
   - After `git pull`, users run `tc sync` to refresh local indexes.

6. **Publish gate**
   - Only high-signal artifacts become shared “truth.”
   - Candidate items are created for review before becoming decisions/patterns/runbooks.

---

## What TeamContext Produces

TeamContext stores content under a predictable structure:

- **Shared (committed)**
  - `decisions/` — Architecture or product decisions (reviewed truth)
  - `patterns/` — Reusable coding/engineering patterns
  - `runbooks/` — Operational guides and troubleshooting
  - `changelog/` — Short, append-only summaries of what changed and why
  - `candidates/` — Proposed items pending review (not default retrieval)

- **Local only (not committed by default)**
  - `sessions/` — Raw conversations and session-level summaries (L0/L1), stored locally

- **Local only**
  - `index/` — Local vector index built from shared content

---

## User Experience (UX)

TeamContext fits into an existing Git workflow. The intended daily loop:

### Sync team context
```bash
git pull ...
tc sync
```

### Work with your preferred LLM tool
Use Codex/Cursor/anything. TeamContext does not constrain your tool choice.

### Publish high-signal context
```bash
tc commit
# tc prints a message suggesting you run git status/add/commit/push
git push ...
```

TeamContext does **not** execute Git commands; it only generates/updates files.

---

## Key Commands

### `tc init`
Initialize TeamContext in a project:
- Create directories and default configuration
- Create lock file for OpenViking
- Clone OpenViking into a local vendor directory
- Checkout the pinned ref/commit
- Run a quick health check

### `tc sync`
Refresh local searchability after `git pull`:
- Detect changes in shared context files
- Build/refresh summaries (optional L0/L1)
- Incrementally update local index
- Print a sync report

### `tc commit`
Turn local work into shared, reviewable context:
- Finalize the current local session (raw stays local)
- Generate shared artifacts:
  - `shared/changelog/YYYY-MM-DD-<user>-<topic>.md` (append-only)
  - `shared/candidates/*.md` for proposed decisions/patterns/runbooks
- Run secret/PII scanning (configurable)
- Print “next steps” (git status/add/commit/push)

### `tc doctor`
Diagnose environment and installation issues:
- Validate config + directory structure
- Validate OpenViking vendor checkout matches lock
- Validate index and shared paths are writable

### Optional: `tc vendor upgrade --ref <tag|commit>`
Explicitly upgrade the pinned OpenViking engine:
- Fetch and checkout new ref
- Run compatibility checks
- Update lock.json resolved commit

---

## Architecture Overview

TeamContext consists of three layers:

1. **CLI + Orchestration (TeamContext)**
   - Parses commands
   - Manages config, paths, and lock
   - Implements publish gate logic
   - Runs scanning and reporting

2. **Engine Integration (OpenVikingEngine)**
   - Adds the vendored OpenViking repo to `sys.path`
   - Imports OpenViking Python APIs
   - Provides a stable wrapper interface:
     - initialize store
     - index shared docs
     - build summaries
     - commit sessions
     - health checks

3. **Storage**
   - Content store: files under `.viking/agfs/`
   - Local index: `.viking/index/` (ignored by git)

---

## Repository Layout (for projects using TeamContext)

Suggested project layout after `tc init`:

```text
<project>/
  .tc/
    config.yaml
    lock.json
    vendor/openviking/         # gitignored
    state/                     # gitignored
  .viking/
    agfs/
      shared/                  # committed
        decisions/
        patterns/
        runbooks/
        candidates/
        changelog/
      sessions/                # gitignored by default
    index/                     # gitignored
```

Suggested `.gitignore` entries:
```gitignore
.tc/vendor/
.tc/state/
.viking/index/
.viking/agfs/sessions/
```

---

## Security & Privacy

TeamContext is designed to reduce risk:
- Raw chat logs are local by default
- Secret/PII scanning can block `tc commit`
- Shared context is structured and reviewable

Recommended best practice:
- Keep `shared/` high-signal and low-noise
- Move raw sessions to a separate private repo only if needed

---

## Scope (MVP)

### Included in v0.1
- `tc init`, `tc sync`, `tc commit`, `tc doctor`
- Vendored OpenViking + lock pinning
- Shared skeleton generation
- Local indexing of shared context
- Append-only changelog + candidates generation
- Basic secret scanning hook (optional dependency)

### Out of scope for v0.1
- Hosting a shared remote index service
- Syncing raw sessions via main repo
- Full multi-engine plugin ecosystem (but we will design the interface)

---

## Future Roadmap

- **v0.2**
  - Better candidate deduplication and conflict detection
  - Pre-commit hook templates
  - `tc status` and richer reports

- **v0.3**
  - Optional team-shared index backend
  - More engines via plugin interface
  - CI checks for shared context quality and policy

---

## Contribution Guidelines (High Level)

- Keep CLI stable; breaking changes require a clear migration path
- Default behavior should be safe (no raw sync, no git calls)
- Prefer append-only files to avoid conflicts
- Add tests for lock behavior and engine integration
- Document every new config field and directory contract

---

## Quick Start (for Codex-driven development)

1. Build the Python package skeleton with an entrypoint `tc`.
2. Implement `tc init`:
   - create directories
   - create config + lock
   - clone OpenViking into `.tc/vendor/openviking`
   - checkout pinned commit
   - run `doctor`
3. Implement `tc sync`:
   - scan `.viking/agfs/shared`
   - update summaries (optional)
   - update local index
4. Implement `tc commit`:
   - write changelog + candidates
   - keep sessions local
   - scan secrets
5. Add documentation and templates in `templates/`.

---

## Project Name

**TeamContext** (CLI: `tc`)
