from __future__ import annotations

import argparse
import getpass
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from teamcontext import __build_id__, __display_version__, __version__
from teamcontext.engine import OpenVikingEngine

DEFAULT_VENDOR_REPO = "https://github.com/volcengine/OpenViking.git"
DEFAULT_VENDOR_REF = "main"


class TcError(RuntimeError):
    """Domain error for user-facing command failures."""


@dataclass(frozen=True)
class TcPaths:
    root: Path
    tc_dir: Path
    config_path: Path
    lock_path: Path
    vendor_dir: Path
    vendor_openviking: Path
    state_dir: Path
    viking_dir: Path
    agfs_dir: Path
    shared_dir: Path
    sessions_dir: Path
    index_dir: Path

    @classmethod
    def for_root(cls, root: Path) -> "TcPaths":
        tc_dir = root / ".tc"
        return cls(
            root=root,
            tc_dir=tc_dir,
            config_path=tc_dir / "config.yaml",
            lock_path=tc_dir / "lock.json",
            vendor_dir=tc_dir / "vendor",
            vendor_openviking=tc_dir / "vendor" / "openviking",
            state_dir=tc_dir / "state",
            viking_dir=root / ".viking",
            agfs_dir=root / ".viking" / "agfs",
            shared_dir=root / ".viking" / "agfs" / "shared",
            sessions_dir=root / ".viking" / "agfs" / "sessions",
            index_dir=root / ".viking" / "index",
        )


def _load_lock(lock_path: Path) -> dict[str, Any]:
    if not lock_path.exists():
        return {}
    return json.loads(lock_path.read_text(encoding="utf-8"))


def _write_lock(lock_path: Path, payload: dict[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_config(config_path: Path, payload: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=False, capture_output=True, text=True)


def _resolve_root(project_root: str | None) -> Path:
    return Path(project_root).resolve() if project_root else Path.cwd().resolve()


def _project_root_from_args(args: argparse.Namespace) -> str | None:
    local = getattr(args, "project_root_local", None)
    if local:
        return local
    return getattr(args, "project_root", None)


def _ensure_base_dirs(paths: TcPaths) -> None:
    dirs = [
        paths.tc_dir,
        paths.vendor_dir,
        paths.state_dir,
        paths.viking_dir,
        paths.agfs_dir,
        paths.shared_dir,
        paths.sessions_dir,
        paths.index_dir,
        paths.shared_dir / "decisions",
        paths.shared_dir / "patterns",
        paths.shared_dir / "runbooks",
        paths.shared_dir / "candidates",
        paths.shared_dir / "changelog",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _gitignore_lines() -> list[str]:
    return [
        ".tc/vendor/",
        ".tc/state/",
        ".viking/index/",
        ".viking/agfs/sessions/",
    ]


def _merge_gitignore(root: Path) -> tuple[bool, list[str]]:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    to_add = [line for line in _gitignore_lines() if line not in existing]
    if not to_add:
        return False, []
    with gitignore.open("a", encoding="utf-8") as f:
        if existing and existing[-1] != "":
            f.write("\n")
        for line in to_add:
            f.write(f"{line}\n")
    return True, to_add


def _git_commit(cwd: Path) -> str | None:
    cp = _run(["git", "rev-parse", "HEAD"], cwd=cwd)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def _git_has_remote(cwd: Path) -> bool:
    cp = _run(["git", "remote"], cwd=cwd)
    if cp.returncode != 0:
        return False
    return bool(cp.stdout.strip())


def _vendor_health(lock: dict[str, Any], vendor_repo_path: Path) -> tuple[bool, str]:
    expected = lock.get("openviking", {}).get("resolved_commit")
    if not vendor_repo_path.exists():
        return False, "missing vendor repository"
    if not (vendor_repo_path / ".git").exists():
        return False, "vendor exists but is not a git repository"
    actual = _git_commit(vendor_repo_path)
    if not actual:
        return False, "unable to read vendor commit"
    if expected and actual != expected:
        return False, f"commit mismatch (expected {expected[:12]}, got {actual[:12]})"
    return True, f"ok ({actual[:12]})"


def _maybe_clone_vendor(paths: TcPaths, lock: dict[str, Any]) -> tuple[bool, str]:
    repo = lock["openviking"]["repo"]
    ref = lock["openviking"]["ref"]

    if not shutil.which("git"):
        return False, "git not found; skipped vendor clone"

    if (paths.vendor_openviking / ".git").exists():
        checkout = _run(["git", "checkout", ref], cwd=paths.vendor_openviking)
        if checkout.returncode != 0:
            return False, f"vendor checkout failed: {checkout.stderr.strip()}"
        commit = _git_commit(paths.vendor_openviking)
        if commit:
            lock["openviking"]["resolved_commit"] = commit
            _write_lock(paths.lock_path, lock)
        return True, "vendor already present; checked out requested ref"

    clone = _run(["git", "clone", "--depth", "1", "--branch", ref, repo, str(paths.vendor_openviking)], cwd=paths.root)
    if clone.returncode != 0:
        return False, f"vendor clone skipped: {clone.stderr.strip() or clone.stdout.strip() or 'unknown git error'}"

    commit = _git_commit(paths.vendor_openviking)
    if commit:
        lock["openviking"]["resolved_commit"] = commit
        _write_lock(paths.lock_path, lock)
    return True, "vendor cloned and pinned"


def _checkout_vendor_ref(paths: TcPaths, lock: dict[str, Any], ref: str) -> tuple[bool, str]:
    if not shutil.which("git"):
        return False, "git not found"
    if not (paths.vendor_openviking / ".git").exists():
        return False, "vendor repository is missing; run `tc init` first"

    if _git_has_remote(paths.vendor_openviking):
        fetch = _run(["git", "fetch", "--tags", "--prune"], cwd=paths.vendor_openviking)
        if fetch.returncode != 0:
            return False, f"git fetch failed: {fetch.stderr.strip() or fetch.stdout.strip()}"

    checkout = _run(["git", "checkout", ref], cwd=paths.vendor_openviking)
    if checkout.returncode != 0:
        return False, f"git checkout failed: {checkout.stderr.strip() or checkout.stdout.strip()}"

    commit = _git_commit(paths.vendor_openviking)
    if not commit:
        return False, "unable to resolve checked out commit"

    lock.setdefault("openviking", {})
    lock["openviking"]["ref"] = ref
    lock["openviking"]["resolved_commit"] = commit
    _write_lock(paths.lock_path, lock)
    return True, f"checked out {ref} ({commit[:12]})"


def cmd_init(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

    config = _load_config(paths.config_path)
    if not config:
        config = {
            "project_root": str(root),
            "paths": {
                "shared": str(paths.shared_dir.relative_to(root)),
                "sessions": str(paths.sessions_dir.relative_to(root)),
                "index": str(paths.index_dir.relative_to(root)),
            },
            "security": {"secret_scan": True, "block_on_findings": True},
            "engine": {"name": "openviking", "vendor_path": str(paths.vendor_openviking.relative_to(root))},
        }
        _write_config(paths.config_path, config)

    lock = _load_lock(paths.lock_path)
    if not lock:
        lock = {
            "version": 1,
            "openviking": {
                "repo": DEFAULT_VENDOR_REPO,
                "ref": args.vendor_ref or DEFAULT_VENDOR_REF,
                "resolved_commit": None,
            },
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        _write_lock(paths.lock_path, lock)

    updated_ignore, added = _merge_gitignore(root)
    clone_ok, clone_msg = _maybe_clone_vendor(paths, lock)
    init_sync_payload = _run_sync(paths, root)
    root_rules_path = _write_root_agent_rules(paths)
    bootstrap_path, workflow_path, intents_path = _write_agent_files(paths, init_sync_payload)
    _write_save_state(paths, _tracked_workspace_files(root))

    print(f"Initialized TeamContext in {root}")
    print(f"- teamcontext version: {__version__} (build {__build_id__})")
    print(f"- config: {paths.config_path}")
    print(f"- lock: {paths.lock_path}")
    print(f"- vendor: {clone_msg}")
    print(f"- agent bootstrap: {bootstrap_path}")
    print(f"- agent workflow: {workflow_path}")
    print(f"- agent intents: {intents_path}")
    print(f"- agent context template: {paths.tc_dir / 'agent' / 'session_context_template.md'}")
    print(f"- root agent rules: {root_rules_path}")
    print(
        "- initial sync: "
        f"scanned={init_sync_payload['shared_files_scanned']}, "
        f"changed={init_sync_payload['changed_files']}, "
        f"removed={init_sync_payload['removed_files']}"
    )
    if updated_ignore:
        print(f"- .gitignore updated with: {', '.join(added)}")
    if not clone_ok:
        print("- note: run `tc doctor` after network/git access is available")
    print("LLM workflow:")
    print("- after git pull: run `tc sync`")
    print("- `tc sync` prints a paste-ready bootstrap prompt for Codex/Claude")

    doctor_status = cmd_doctor(argparse.Namespace(project_root=str(root), quiet=True))
    if doctor_status != 0 and clone_ok:
        return 1
    return 0


def _collect_shared_files(shared_dir: Path) -> list[Path]:
    if not shared_dir.exists():
        return []
    return sorted([p for p in shared_dir.rglob("*.md") if p.is_file()])


def _index_state_path(paths: TcPaths) -> Path:
    return paths.state_dir / "sync_state.json"


def _load_sync_state(paths: TcPaths) -> dict[str, float]:
    state_path = _index_state_path(paths)
    if not state_path.exists():
        return {}
    data = json.loads(state_path.read_text(encoding="utf-8"))
    files = data.get("files", {})
    return files if isinstance(files, dict) else {}


def _write_sync_state(paths: TcPaths, state: dict[str, float]) -> None:
    state_path = _index_state_path(paths)
    payload = {"updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "files": state}
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _save_state_path(paths: TcPaths) -> Path:
    return paths.state_dir / "save_state.json"


def _load_save_state(paths: TcPaths) -> dict[str, dict[str, int]]:
    state_path = _save_state_path(paths)
    if not state_path.exists():
        return {}
    data = json.loads(state_path.read_text(encoding="utf-8"))
    files = data.get("files", {})
    return files if isinstance(files, dict) else {}


def _write_save_state(paths: TcPaths, files: dict[str, dict[str, int]]) -> None:
    state_path = _save_state_path(paths)
    payload = {"updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "files": files}
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _category_counts(paths: TcPaths) -> dict[str, int]:
    counts: dict[str, int] = {}
    for category in ["decisions", "patterns", "runbooks", "candidates", "changelog"]:
        base = paths.shared_dir / category
        if not base.exists():
            counts[category] = 0
            continue
        counts[category] = len([p for p in base.rglob("*.md") if p.is_file()])
    return counts


def _has_shared_history(paths: TcPaths) -> bool:
    counts = _category_counts(paths)
    return (counts.get("changelog", 0) + counts.get("candidates", 0)) > 0


def _sync_snapshot(paths: TcPaths) -> tuple[str | None, int]:
    state_path = _index_state_path(paths)
    if not state_path.exists():
        return None, 0
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    updated_at = payload.get("updated_at")
    files = payload.get("files", {})
    file_count = len(files) if isinstance(files, dict) else 0
    return updated_at if isinstance(updated_at, str) else None, file_count


def _write_root_agent_rules(paths: TcPaths) -> Path:
    out = paths.root / "TEAMCONTEXT_AGENT_RULES.md"
    out.write_text(
        "\n".join(
            [
                "# TeamContext Agent Rules",
                "",
                "Persistent guardrails for all LLM coding sessions in this repository.",
                "",
                "## Always Read First",
                "- `.tc/agent/bootstrap_prompt.md`",
                "- `.tc/agent/workflow.md`",
                "- `.tc/agent/intents.json`",
                "",
                "## Human vs Agent Rule",
                "- Human should run only `tc init`.",
                "- All other TeamContext operations must be executed by the LLM agent.",
                "",
                "## Intent Mapping",
                "- `save context` or `save recent context to tc` -> run `tc agent save` with semantic fields from current discussion.",
                "  Example: `tc agent save --intent \"<intent-delta>\" --decisions \"<decisions>\" --rationale \"<why>\" --next-step \"<owner+command>\"`",
                "- `sync context` or `sync latest context` -> `tc agent run \"sync latest context\"`",
                "",
                "## Execution Contract",
                "- Execute mapped `tc` command immediately; do not only print command text.",
                "- Do not claim context was saved/synced unless a `tc` command actually ran.",
                "- After command, reply with:",
                "  - `tc_command: <exact command>`",
                "  - `exit_code: <code>`",
                "  - `result: <key output>`",
                "",
                "## Save Contract",
                "- Save must capture non-code discussion context from this session.",
                "- Include at least: `--intent` and `--decisions` (or equivalent fields in context file).",
                "- TeamContext does not auto-generate semantic fields; missing fields cause save failure.",
                "- When user says `save recent context to tc`, agent must auto-generate required fields from current project discussion and execute save directly.",
                "- Never ask the user to manually provide `--intent` / `--decisions` values.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def _bootstrap_prompt(paths: TcPaths, sync_payload: dict[str, Any] | None = None) -> str:
    lines = [
        "Read the following TeamContext sources before coding:\n"
    ]
    lines.append(f"- {paths.root / 'TEAMCONTEXT_AGENT_RULES.md'}\n")
    lines.append(f"- {paths.shared_dir / 'decisions'}\n")
    lines.append(f"- {paths.shared_dir / 'patterns'}\n")
    lines.append(f"- {paths.shared_dir / 'runbooks'}\n")
    lines.append(f"- {paths.index_dir / 'index.txt'}\n")
    if sync_payload:
        lines.append("Latest sync snapshot:\n")
        lines.append(
            f"- shared_files_scanned={sync_payload.get('shared_files_scanned', 0)} | "
            f"changed_files={sync_payload.get('changed_files', 0)} | "
            f"removed_files={sync_payload.get('removed_files', 0)}\n"
        )
    lines.append("If index.txt is missing, run `tc sync` first.\n")
    lines.append(
        'If decisions/patterns/runbooks are empty, report "no approved team context yet" '
        "and continue with code-level context.\n"
    )
    lines.append("Then do this before writing code:\n")
    lines.append("- Summarize the constraints and decisions you will follow.\n")
    lines.append("- List exactly which files you read.\n")
    lines.append("- If context is missing or conflicting, ask clarifying questions first.\n")
    lines.append("\n")
    lines.append("Human vs Agent rule:\n")
    lines.append("- Human should run only `tc init`.\n")
    lines.append("- All other TeamContext operations must be executed by the LLM agent.\n")
    lines.append("\n")
    lines.append("TeamContext command rule:\n")
    lines.append('- When user asks to "save context"/"save recent context to tc", execute `tc agent save` immediately and include semantic fields from current discussion (`--intent`, `--decisions`, optional `--rationale`, `--impact-scope`, `--next-step`).\n')
    lines.append('- When user asks to "sync context"/"sync latest context", execute mapped `tc` command immediately.\n')
    lines.append("- Do not claim context was saved/synced unless a `tc` command actually ran.\n")
    lines.append("- Do not auto-fill fake semantic context; if intent/decisions are missing, ask clarification and then run save.\n")
    lines.append("Agent save macro (run without asking user to fill fields):\n")
    lines.append('- From THIS project discussion about current uncommitted changes, auto-generate semantic fields and execute:\n')
    lines.append('  `tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent "<generated_intent>" --decisions "<generated_decisions>" --rationale "<generated_rationale>" --impact-scope "<generated_scope>" --next-step "<generated_next_step>"`\n')
    lines.append("- Never ask user to type these fields manually.\n")
    lines.append("Post-command reply format:\n")
    lines.append("- `tc_command: <exact command>`\n")
    lines.append("- `exit_code: <code>`\n")
    lines.append("- `result: <key output>`")
    return "".join(lines)


def _write_agent_files(paths: TcPaths, sync_payload: dict[str, Any] | None = None) -> tuple[Path, Path, Path]:
    agent_dir = paths.tc_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_path = agent_dir / "bootstrap_prompt.md"
    workflow_path = agent_dir / "workflow.md"
    intents_path = agent_dir / "intents.json"
    session_template_path = agent_dir / "session_context_template.md"
    bootstrap_path.write_text(_bootstrap_prompt(paths, sync_payload) + "\n", encoding="utf-8")
    workflow_path.write_text(
        "\n".join(
            [
                "# TeamContext Agent Workflow",
                "",
                "Use these intent->command mappings in vibe coding sessions:",
                "",
                "Policy:",
                "- Human runs only `tc init`.",
                "- All other TeamContext commands must be run by the agent.",
                "",
                '- User says: "save recent context to tc"',
                "- Summarize this session into semantic flags, then run:",
                "  `tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent \"<intent-delta>\" --decisions \"<decisions>\" --rationale \"<why>\" --impact-scope \"<scope>\" --next-step \"<owner+command>\"`",
                "- TeamContext will write `.tc/state/session_context.md` from provided flags.",
                "- Required behavior: auto-generate all semantic fields from current project discussion.",
                "- Do not ask user to provide `--intent`/`--decisions` values.",
                "",
                '- User says: "save context"',
                "- Treat as alias of `save recent context to tc` and run the same `tc agent save ...` command with semantic flags.",
                "",
                '- User says: "sync latest context"',
                "- Run: `tc agent run \"sync latest context\"`",
                "",
                '- User says: "sync context"',
                "- Treat as alias of `sync latest context` and run: `tc agent run \"sync latest context\"`",
                "",
                "Strict intent router compatibility:",
                '- `tc agent run "save recent context to tc"` only works if mapped command already includes semantic fields or a valid context file.',
                '- If placeholders are present (e.g. `<intent-delta>`), replace with generated values before execution.',
                "",
                "Execution rule:",
                "- Execute mapped commands immediately; do not only print command text.",
                "- Do not switch to unrelated tools/commands when intent is context save/sync.",
                "- Only return command text without execution if user explicitly asks for command-only output.",
                "",
                "Post-execution response contract:",
                "- Include exact command, exit code, and key results from stdout.",
                "- Use this strict format:",
                "  - tc_command: <exact command>",
                "  - exit_code: <code>",
                "  - result: <key output>",
                "Then summarize key deltas for the user.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    intents_payload = {
        "version": 1,
        "default_mode": "execute",
        "rules": [
            {
                "intent": "save recent context to tc",
                "command": [
                    "tc",
                    "agent",
                    "save",
                    "--changes-source",
                    "uncommitted",
                    "--auto-bootstrap-if-empty",
                    "--intent",
                    "<intent-delta>",
                    "--decisions",
                    "<decisions>",
                ],
                "execute_immediately": True,
            },
            {
                "intent": "save context",
                "command": [
                    "tc",
                    "agent",
                    "save",
                    "--changes-source",
                    "uncommitted",
                    "--auto-bootstrap-if-empty",
                    "--intent",
                    "<intent-delta>",
                    "--decisions",
                    "<decisions>",
                ],
                "execute_immediately": True,
            },
            {
                "intent": "sync latest context",
                "command": ["tc", "sync", "--json"],
                "execute_immediately": True,
            },
            {
                "intent": "sync context",
                "command": ["tc", "sync", "--json"],
                "execute_immediately": True,
            },
        ],
        "command_only_opt_out": "Only skip execution when user explicitly asks for command-only output.",
    }
    intents_path.write_text(json.dumps(intents_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    session_template_path.write_text(
        "\n".join(
            [
                "# Session Context",
                "",
                "## User Goal",
                "- What the user wants to achieve in this session",
                "",
                "## User Intent (Delta)",
                "- For this check-in, what changed in user intent since last save",
                "",
                "## Decisions Made",
                "- Decisions made in this check-in",
                "",
                "## Decision Rationale",
                "- Why these decisions were made",
                "",
                "## Non-code Context (LLM Discussion)",
                "- Important conversation context not visible in code diff",
                "",
                "## Action Items",
                "- Follow-up tasks from this check-in",
                "",
                "## Decision Status",
                "- approved | proposed | blocked",
                "",
                "## Impact Scope",
                "- Modules/files/behaviors affected by this check-in",
                "",
                "## Validation/Outcome",
                "- What was validated and current result",
                "",
                "## Next Step (Owner+Command)",
                "- owner: <name>; command: <exact command>; done_when: <exit criteria>",
                "",
                "## Open Questions",
                "- Unresolved questions or risks",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return bootstrap_path, workflow_path, intents_path


def _run_sync(paths: TcPaths, root: Path) -> dict[str, Any]:
    shared_files = _collect_shared_files(paths.shared_dir)
    before = _load_sync_state(paths)
    after: dict[str, float] = {}
    changed = 0
    changed_paths: list[str] = []

    for p in shared_files:
        rel = str(p.relative_to(root))
        mtime = p.stat().st_mtime
        after[rel] = mtime
        if rel not in before or before[rel] != mtime:
            changed += 1
            changed_paths.append(rel)

    removed_paths = sorted(set(before) - set(after))
    removed = len(removed_paths)
    _write_sync_state(paths, after)
    engine = OpenVikingEngine(paths.vendor_openviking)
    engine_result = engine.index_shared_docs(shared_files, root, paths.index_dir / "index.txt")

    summary_path = paths.state_dir / "sync_summary.txt"
    summary_path.write_text(
        "\n".join(
            [
                f"time: {datetime.utcnow().isoformat(timespec='seconds')}Z",
                f"shared_files: {len(shared_files)}",
                f"changed_files: {changed}",
                f"removed_files: {removed}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = {
        "ok": True,
        "shared_files_scanned": len(shared_files),
        "changed_files": changed,
        "removed_files": removed,
        "changed_paths": changed_paths,
        "removed_paths": removed_paths,
        "index_file": str(paths.index_dir / "index.txt"),
        "engine_message": engine_result.message,
    }
    payload["bootstrap_prompt"] = _bootstrap_prompt(paths, payload)
    (paths.state_dir / "last_sync.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                **payload,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return payload


def _tracked_workspace_files(root: Path) -> dict[str, dict[str, int]]:
    excluded_dirs = {
        ".git",
        ".tc",
        ".viking/index",
        ".viking/agfs/shared",
        ".viking/agfs/sessions",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "node_modules",
    }
    excluded_suffixes = {
        ".pyc",
        ".pyo",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".mp4",
        ".mov",
        ".sqlite",
    }

    files: dict[str, dict[str, int]] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        rel_str = str(rel)
        if any(rel_str == d or rel_str.startswith(f"{d}/") for d in excluded_dirs):
            continue
        if path.suffix.lower() in excluded_suffixes:
            continue
        stat = path.stat()
        files[rel_str] = {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}
    return files


def _is_teamcontext_meta_path(path: str) -> bool:
    return (
        path == "TEAMCONTEXT_AGENT_RULES.md"
        or path.startswith(".tc/")
        or path.startswith(".viking/")
    )


def _git_uncommitted_diff(root: Path) -> tuple[list[str], list[str], list[str]] | None:
    if not (root / ".git").exists():
        return None
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None

    added: set[str] = set()
    modified: set[str] = set()
    deleted: set[str] = set()
    for raw in proc.stdout.splitlines():
        if not raw:
            continue
        status = raw[:2]
        path_part = raw[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        path = path_part.strip('"')
        if not path or _is_teamcontext_meta_path(path):
            continue

        if status == "??":
            added.add(path)
            continue
        if "D" in status:
            deleted.add(path)
            continue
        if "A" in status:
            added.add(path)
            continue
        modified.add(path)

    return sorted(added), sorted(modified), sorted(deleted)


def _workspace_diff(
    before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]
) -> tuple[list[str], list[str], list[str]]:
    added = sorted([k for k in after if k not in before])
    deleted = sorted([k for k in before if k not in after])
    modified = sorted([k for k in after if k in before and after[k] != before[k]])
    return added, modified, deleted


def _auto_topic_from_changes(changes: list[str]) -> str:
    if not changes:
        return "workspace-update"
    stems: list[str] = []
    for item in changes[:5]:
        stem = item.split("/", 1)[0]
        stems.append(_slugify(stem))
    return f"auto-update-{'-'.join(stems)}"


def _auto_summary(added: list[str], modified: list[str], deleted: list[str]) -> str:
    total = len(added) + len(modified) + len(deleted)
    return (
        f"Auto-saved recent workspace progress: {total} changed files "
        f"({len(added)} added, {len(modified)} modified, {len(deleted)} deleted)."
    )


def _semantic_alignment_error(session_ctx: dict[str, str], changed: list[str]) -> str | None:
    if not changed:
        return None
    non_meta_changed = [p for p in changed if not _is_teamcontext_meta_path(p)]
    if not non_meta_changed:
        return None

    impact = _ctx_value(session_ctx, "Impact Scope", "Impact", "impact_scope")
    if not impact:
        if len(non_meta_changed) >= 10:
            return (
                "impact scope missing while non-TeamContext files changed significantly; "
                "include affected modules/files from THIS project discussion."
            )
        return None

    impact_lower = impact.lower()
    contradictory_phrases = [
        "no runtime or application code changes",
        "no application code changes",
        "teamcontext metadata and save workflow only",
        "teamcontext workflow only",
    ]
    if any(phrase in impact_lower for phrase in contradictory_phrases):
        return (
            "impact scope conflicts with detected non-TeamContext file changes; "
            "describe actual impacted project modules/files."
        )

    if len(non_meta_changed) >= 10:
        roots = sorted({p.split("/", 1)[0].lower() for p in non_meta_changed if "/" in p})
        if roots:
            if not any(root in impact_lower for root in roots[:8]):
                return (
                    "impact scope is too generic for this change set; "
                    "mention at least one changed project area (e.g. apps/, packages/, services/)."
                )
    return None


def _session_context_from_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    sections: dict[str, str] = {}
    current = "General"
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if lines:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
            continue
        if line.startswith("# "):
            continue
        lines.append(line)
    if lines:
        sections[current] = "\n".join(lines).strip()
    cleaned = {k: v for k, v in sections.items() if v and v != "-"}
    return cleaned


def _session_context_from_args(args: argparse.Namespace) -> dict[str, str]:
    mapping: list[tuple[str, str]] = [
        ("goal", "User Goal"),
        ("intent", "User Intent (Delta)"),
        ("decisions", "Decisions Made"),
        ("decision_status", "Decision Status"),
        ("rationale", "Decision Rationale"),
        ("impact_scope", "Impact Scope"),
        ("non_code_context", "Non-code Context (LLM Discussion)"),
        ("validation_outcome", "Validation/Outcome"),
        ("next_step", "Next Step (Owner+Command)"),
        ("action_items", "Action Items"),
        ("open_questions", "Open Questions"),
    ]
    ctx: dict[str, str] = {}
    for attr, key in mapping:
        value = getattr(args, attr, None)
        if isinstance(value, str) and value.strip():
            ctx[key] = value.strip()
    return ctx


def _write_session_context_file(path: Path, ctx: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [
        "User Goal",
        "User Intent (Delta)",
        "Decisions Made",
        "Decision Status",
        "Decision Rationale",
        "Impact Scope",
        "Non-code Context (LLM Discussion)",
        "Validation/Outcome",
        "Next Step (Owner+Command)",
        "Action Items",
        "Open Questions",
    ]
    lines = ["# Session Context", ""]
    for key in ordered:
        value = ctx.get(key)
        if value:
            lines.append(f"## {key}")
            lines.append(value)
            lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _summary_from_session_context(ctx: dict[str, str]) -> str:
    ordered = [
        "User Intent (Delta)",
        "Decisions Made",
        "Decision Status",
        "Decision Rationale",
        "Impact Scope",
        "Non-code Context (LLM Discussion)",
        "Validation/Outcome",
        "Next Step (Owner+Command)",
        "Action Items",
        "Open Questions",
        "User Goal",
        "Discussion Summary",
        "Decisions",
        "General",
    ]
    chunks: list[str] = []
    for key in ordered:
        value = ctx.get(key)
        if value:
            chunks.append(f"{key}: {value}")
    return "\n\n".join(chunks)


def _ctx_value(ctx: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = _normalize_ctx_value(ctx.get(key))
        if value:
            return value
    return ""


def _session_context_field_status(ctx: dict[str, str]) -> dict[str, str]:
    return {
        "intent_delta": "yes" if _ctx_value(ctx, "User Intent (Delta)", "User Goal") else "no",
        "decisions": "yes" if _ctx_value(ctx, "Decisions Made", "Decisions") else "no",
        "decision_status": "yes" if _ctx_value(ctx, "Decision Status") else "no",
        "rationale": "yes" if _ctx_value(ctx, "Decision Rationale") else "no",
        "impact_scope": "yes" if _ctx_value(ctx, "Impact Scope") else "no",
        "non_code_context": "yes" if _ctx_value(ctx, "Non-code Context (LLM Discussion)", "Discussion Summary") else "no",
        "validation_outcome": "yes" if _ctx_value(ctx, "Validation/Outcome") else "no",
        "next_step": "yes" if _ctx_value(ctx, "Next Step (Owner+Command)") else "no",
        "open_questions": "yes" if _normalize_ctx_value(ctx.get("Open Questions")) else "no",
    }


def cmd_sync(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

    payload = _run_sync(paths, root)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("Sync complete")
    print(f"- shared files scanned: {payload['shared_files_scanned']}")
    print(f"- changed files: {payload['changed_files']}")
    print(f"- removed files: {payload['removed_files']}")
    print(f"- local index: {payload['index_file']}")
    print(f"- engine: {payload['engine_message']}")
    print("Bootstrap prompt (paste into Codex/Claude):")
    print(payload["bootstrap_prompt"])
    return 0


def _slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return value or "update"


def _detect_secrets(text: str) -> list[str]:
    findings: list[str] = []
    rules = {
        "aws_access_key": r"\bAKIA[0-9A-Z]{16}\b",
        "private_key": r"-----BEGIN (?:RSA|EC|OPENSSH|PGP) PRIVATE KEY-----",
        "generic_api_key": r"(?i)\b(?:api[-_ ]?key|token|secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}[\"']?",
    }
    for name, pattern in rules.items():
        if re.search(pattern, text):
            findings.append(name)
    return findings


def _normalize_ctx_value(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    if text.startswith("- "):
        text = text[2:].strip()
    return text


def _write_candidate(
    paths: TcPaths,
    kind: str,
    topic: str,
    summary: str,
    user: str,
    day: date,
    session_ctx: dict[str, str] | None = None,
) -> Path:
    slug = _slugify(topic)
    out = paths.shared_dir / "candidates" / f"{day.isoformat()}-{user}-{kind}-{slug}.md"
    ctx = session_ctx or {}
    intent_delta = _ctx_value(ctx, "User Intent (Delta)", "User Goal")
    discussion = _ctx_value(ctx, "Non-code Context (LLM Discussion)", "Discussion Summary")
    decisions = _ctx_value(ctx, "Decisions Made", "Decisions")
    decision_status = _ctx_value(ctx, "Decision Status")
    rationale = _ctx_value(ctx, "Decision Rationale")
    impact_scope = _ctx_value(ctx, "Impact Scope")
    actions = _ctx_value(ctx, "Action Items")
    validation_outcome = _ctx_value(ctx, "Validation/Outcome")
    next_step = _ctx_value(ctx, "Next Step (Owner+Command)")
    open_questions = _normalize_ctx_value(ctx.get("Open Questions"))
    out.write_text(
        "\n".join(
            [
                f"# Candidate: {kind}",
                "",
                f"- date: {day.isoformat()}",
                f"- author: {user}",
                f"- topic: {topic}",
                "",
                "## Summary",
                summary,
                "",
                "## Team Session Context",
                f"- user intent (delta): {intent_delta or 'n/a'}",
                f"- decisions: {decisions or 'n/a'}",
                f"- decision status: {decision_status or 'n/a'}",
                f"- decision rationale: {rationale or 'n/a'}",
                f"- impact scope: {impact_scope or 'n/a'}",
                f"- non-code context: {discussion or 'n/a'}",
                f"- validation/outcome: {validation_outcome or 'n/a'}",
                f"- next step (owner+command): {next_step or 'n/a'}",
                f"- action items: {actions or 'n/a'}",
                f"- open questions: {open_questions or 'n/a'}",
                "",
                "## Review Notes",
                "- pending review",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def _write_commit_artifacts(
    *,
    paths: TcPaths,
    root: Path,
    topic: str,
    summary: str,
    user: str,
    kind: str,
    day: date,
    session_ctx: dict[str, str] | None = None,
    changed_count: int | None = None,
) -> tuple[Path, Path]:
    changelog_name = f"{day.isoformat()}-{user}-{_slugify(topic)}.md"
    changelog_path = paths.shared_dir / "changelog" / changelog_name
    candidate_path = _write_candidate(paths, kind, topic, summary, user, day, session_ctx=session_ctx)
    ctx = session_ctx or {}
    intent_delta = _ctx_value(ctx, "User Intent (Delta)", "User Goal")
    discussion = _ctx_value(ctx, "Non-code Context (LLM Discussion)", "Discussion Summary")
    decisions = _ctx_value(ctx, "Decisions Made", "Decisions")
    decision_status = _ctx_value(ctx, "Decision Status")
    rationale = _ctx_value(ctx, "Decision Rationale")
    impact_scope = _ctx_value(ctx, "Impact Scope")
    actions = _ctx_value(ctx, "Action Items")
    validation_outcome = _ctx_value(ctx, "Validation/Outcome")
    next_step = _ctx_value(ctx, "Next Step (Owner+Command)")
    open_questions = _normalize_ctx_value(ctx.get("Open Questions"))
    changed_line = f"- changed files: {changed_count}" if changed_count is not None else None
    body = [
        f"# Changelog: {topic}",
        "",
        f"- date: {day.isoformat()}",
        f"- author: {user}",
    ]
    if changed_line:
        body.append(changed_line)
    body.extend(
        [
            "",
            "## What changed",
            summary,
            "",
            "## Team Session Context",
            f"- user intent (delta): {intent_delta or 'n/a'}",
            f"- decisions: {decisions or 'n/a'}",
            f"- decision status: {decision_status or 'n/a'}",
            f"- decision rationale: {rationale or 'n/a'}",
            f"- impact scope: {impact_scope or 'n/a'}",
            f"- non-code context: {discussion or 'n/a'}",
            f"- validation/outcome: {validation_outcome or 'n/a'}",
            f"- next step (owner+command): {next_step or 'n/a'}",
            f"- action items: {actions or 'n/a'}",
            f"- open questions: {open_questions or 'n/a'}",
            "",
            "## Candidate generated",
            str(candidate_path.relative_to(root)),
        ]
    )
    changelog_path.write_text(
        "\n".join(body) + "\n",
        encoding="utf-8",
    )
    return changelog_path, candidate_path


def cmd_commit(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

    config = _load_config(paths.config_path)
    security_cfg = config.get("security", {}) if isinstance(config, dict) else {}
    secret_scan_enabled = bool(security_cfg.get("secret_scan", True))
    block_on_findings = bool(security_cfg.get("block_on_findings", True))

    user = _slugify(args.user or getpass.getuser())
    topic = args.topic.strip() if args.topic else "general-update"
    summary = args.summary.strip() if args.summary else "No summary provided"
    day = date.today()

    changelog_path, candidate_path = _write_commit_artifacts(
        paths=paths,
        root=root,
        topic=topic,
        summary=summary,
        user=user,
        kind=args.kind,
        day=day,
    )

    findings = _detect_secrets(summary) if secret_scan_enabled else []
    if findings:
        print("Secret/PII scan findings detected:")
        for finding in findings:
            print(f"- {finding}")
        if block_on_findings and not args.allow_findings:
            print("Commit artifacts were generated, but blocking due to findings.")
            print("Re-run with --allow-findings only if this is a false positive.")
            return 2

    print("Commit artifacts generated")
    print(f"- changelog: {changelog_path}")
    print(f"- candidate: {candidate_path}")
    print("Next steps:")
    print("- git status")
    print("- git add .viking/agfs/shared .tc")
    print("- git commit -m 'teamcontext: publish context'")
    print("- git push")
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

    config = _load_config(paths.config_path)
    security_cfg = config.get("security", {}) if isinstance(config, dict) else {}
    secret_scan_enabled = bool(security_cfg.get("secret_scan", True))
    block_on_findings = bool(security_cfg.get("block_on_findings", True))

    user = _slugify(args.user or getpass.getuser())
    context_path = Path(args.context_file) if args.context_file else (paths.state_dir / "session_context.md")
    if not context_path.is_absolute():
        context_path = (root / context_path).resolve()
    file_ctx = _session_context_from_file(context_path)
    arg_ctx = _session_context_from_args(args)
    session_ctx = {**file_ctx, **arg_ctx}
    if not session_ctx:
        print("Session context is required but missing or empty.")
        print(f"- context file: {context_path}")
        print("Provide semantic fields from current discussion, e.g. --intent and --decisions.")
        print("Or use: `tc agent save --intent \"...\" --decisions \"...\"`")
        return 2
    if arg_ctx:
        _write_session_context_file(context_path, session_ctx)

    if not _ctx_value(session_ctx, "User Intent (Delta)", "User Goal") or not _ctx_value(
        session_ctx, "Decisions Made", "Decisions"
    ):
        print("Session context is incomplete for check-in sync.")
        print("- required: User Intent (Delta) and Decisions Made")
        print(f"- context file: {context_path}")
        print("Provide semantic fields from current discussion, then re-run save.")
        return 2

    auto_bootstrap = False
    after = _tracked_workspace_files(root)
    source = args.changes_source
    if source == "auto":
        source = "uncommitted" if (root / ".git").exists() else "workspace"
    if source == "uncommitted":
        diff = _git_uncommitted_diff(root)
        if diff is None:
            print("Unable to compute uncommitted change set.")
            print("- reason: current path is not a git repository or git status failed")
            print("Re-run inside a git repo, or use `--changes-source workspace`.")
            return 2
        added, modified, deleted = diff
    else:
        before = {} if args.bootstrap else _load_save_state(paths)
        added, modified, deleted = _workspace_diff(before, after)
    changed = added + modified + deleted
    if not changed:
        if source == "workspace" and args.auto_bootstrap_if_empty and not _has_shared_history(paths):
            auto_bootstrap = True
            added, modified, deleted = _workspace_diff({}, after)
            changed = added + modified + deleted
        else:
            if source == "uncommitted":
                print("No uncommitted workspace changes to save.")
            else:
                print("No new workspace changes since last save.")
            if not _has_shared_history(paths):
                print("Hint: if this is first-time capture for an existing project, run:")
                print("`tc save --bootstrap`")
            return 0

    effective_bootstrap = args.bootstrap or auto_bootstrap
    if effective_bootstrap and len(changed) > args.large_save_threshold and not args.force_large_save:
        print("Bootstrap save blocked: change set exceeds safety threshold.")
        print(f"- changed files: {len(changed)}")
        print(f"- threshold: {args.large_save_threshold}")
        print("If this is intentional, re-run with:")
        print("`tc save --bootstrap --force-large-save`")
        return 3

    alignment_error = _semantic_alignment_error(session_ctx, changed)
    if alignment_error:
        print("Session context conflicts with detected workspace changes.")
        print(f"- reason: {alignment_error}")
        print("Regenerate intent/decisions/impact from THIS project's actual recent work, then re-run save.")
        return 2

    if args.topic:
        topic = args.topic.strip()
    elif _ctx_value(session_ctx, "User Intent (Delta)", "User Goal"):
        topic = _slugify(_ctx_value(session_ctx, "User Intent (Delta)", "User Goal"))[:80]
    elif _ctx_value(session_ctx, "Decisions Made", "Decisions"):
        topic = _slugify(_ctx_value(session_ctx, "Decisions Made", "Decisions"))[:80]
    else:
        topic = _auto_topic_from_changes(changed)

    if args.summary:
        summary = args.summary.strip()
    elif session_ctx:
        summary = _summary_from_session_context(session_ctx)
    else:
        summary = _auto_summary(added, modified, deleted)
    day = date.today()
    changelog_path, candidate_path = _write_commit_artifacts(
        paths=paths,
        root=root,
        topic=topic,
        summary=summary,
        user=user,
        kind=args.kind,
        day=day,
        session_ctx=session_ctx if session_ctx else None,
        changed_count=len(changed),
    )

    findings = _detect_secrets(summary) if secret_scan_enabled else []
    if findings:
        print("Secret/PII scan findings detected:")
        for finding in findings:
            print(f"- {finding}")
        if block_on_findings and not args.allow_findings:
            print("Auto-save artifacts were generated, but blocking due to findings.")
            print("Re-run with --allow-findings only if this is a false positive.")
            return 2

    _write_save_state(paths, after)
    print("Auto context save complete")
    if args.bootstrap:
        print("- mode: bootstrap (captured baseline context)")
    elif auto_bootstrap:
        print("- mode: auto-bootstrap (captured baseline context)")
    else:
        print("- mode: incremental")
    print(f"- topic: {topic}")
    print(f"- changed files: {len(changed)}")
    print(f"- changes source: {source}")
    if session_ctx:
        print(f"- context source: {context_path}")
        status = _session_context_field_status(session_ctx)
        print(
            "- context fields: "
            f"intent_delta={status['intent_delta']}, "
            f"decisions={status['decisions']}, "
            f"decision_status={status['decision_status']}, "
            f"rationale={status['rationale']}, "
            f"impact_scope={status['impact_scope']}, "
            f"non_code_context={status['non_code_context']}, "
            f"validation_outcome={status['validation_outcome']}, "
            f"next_step={status['next_step']}, "
            f"open_questions={status['open_questions']}"
        )
    print(f"- changelog: {changelog_path}")
    print(f"- candidate: {candidate_path}")
    print("Agent usage:")
    print('- before push, you can say: "save recent context to tc"')
    print("- recommended command shape:")
    print(
        '  tc agent save --changes-source uncommitted --auto-bootstrap-if-empty --intent "<intent-delta>" '
        '--decisions "<decisions>" --rationale "<why>" --impact-scope "<scope>" '
        '--next-step "<owner+command>"'
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    findings: list[tuple[str, bool, str]] = []

    findings.append(("config", paths.config_path.exists(), str(paths.config_path)))
    findings.append(("lock", paths.lock_path.exists(), str(paths.lock_path)))

    for d in [paths.shared_dir, paths.sessions_dir, paths.index_dir, paths.state_dir]:
        findings.append((f"dir:{d.name}", d.exists(), str(d)))

    lock = _load_lock(paths.lock_path) if paths.lock_path.exists() else {}
    vendor_ok, vendor_msg = _vendor_health(lock, paths.vendor_openviking)
    findings.append(("vendor", vendor_ok, vendor_msg))
    engine_result = OpenVikingEngine(paths.vendor_openviking).health()
    findings.append(("engine", engine_result.ok, engine_result.message))

    writable_checks = [paths.tc_dir, paths.viking_dir, paths.index_dir]
    for d in writable_checks:
        if not d.exists():
            findings.append((f"writable:{d.name}", False, "path missing"))
            continue
        try:
            probe = d / ".tc_write_probe"
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink()
            findings.append((f"writable:{d.name}", True, "ok"))
        except OSError as exc:
            findings.append((f"writable:{d.name}", False, str(exc)))

    failures = [f for f in findings if not f[1]]
    if not args.quiet:
        print("Doctor report")
        for key, ok, detail in findings:
            status = "OK" if ok else "FAIL"
            print(f"- {status:<4} {key}: {detail}")
        print(f"- summary: {len(findings) - len(failures)} ok, {len(failures)} fail")

    return 0 if not failures else 1


def cmd_vendor_upgrade(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    lock = _load_lock(paths.lock_path)
    if not lock:
        print("error: lock file missing; run `tc init` first", file=sys.stderr)
        return 2

    ok, message = _checkout_vendor_ref(paths, lock, args.ref)
    if not ok:
        print(f"Vendor upgrade failed: {message}")
        return 1

    print("Vendor upgrade complete")
    print(f"- vendor: {message}")
    print(f"- lock: {paths.lock_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

    counts = _category_counts(paths)
    last_sync, synced_files = _sync_snapshot(paths)
    shared_files = _collect_shared_files(paths.shared_dir)

    print("TeamContext status")
    print(f"- root: {root}")
    print(f"- shared files: {len(shared_files)}")
    for category in ["decisions", "patterns", "runbooks", "candidates", "changelog"]:
        print(f"- {category}: {counts[category]}")
    print(f"- local index file: {paths.index_dir / 'index.txt'}")
    print(f"- last sync: {last_sync or 'never'}")
    print(f"- synced file entries: {synced_files}")
    return 0


def _latest_changelog_files(paths: TcPaths, latest: int) -> list[Path]:
    base = paths.shared_dir / "changelog"
    if not base.exists():
        return []
    files = sorted([p for p in base.rglob("*.md") if p.is_file()])
    if latest <= 0:
        return files
    return files[-latest:]


def cmd_verify_context(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

    files = _latest_changelog_files(paths, args.latest)
    if not files:
        print("No changelog files found to verify.")
        return 1

    required = [
        "- user intent (delta):",
        "- decisions:",
        "- decision rationale:",
        "- non-code context:",
    ]
    failures: list[str] = []
    checked = 0
    for p in files:
        checked += 1
        text = p.read_text(encoding="utf-8")
        lowered = text.lower()
        for marker in required:
            if marker not in lowered:
                failures.append(f"{p}: missing `{marker}`")
                continue
            if f"{marker} n/a" in lowered:
                failures.append(f"{p}: `{marker}` is n/a")

    print(f"Context verification checked: {checked} changelog file(s)")
    if failures:
        print("Context verification FAILED")
        for item in failures:
            print(f"- {item}")
        return 1
    print("Context verification OK")
    return 0


def cmd_agent_run(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

    intent = " ".join(args.intent).strip()
    intents_path = paths.tc_dir / "agent" / "intents.json"
    if not intents_path.exists():
        print(f"error: missing intents file: {intents_path}", file=sys.stderr)
        return 2

    payload = json.loads(intents_path.read_text(encoding="utf-8"))
    rules = payload.get("rules", [])
    if not isinstance(rules, list):
        print("error: invalid intents.json format", file=sys.stderr)
        return 2

    match = None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("intent") == intent:
            match = rule
            break
    if not match:
        print(f"error: no mapped command for intent: {intent}", file=sys.stderr)
        return 2

    command = match.get("command")
    if not isinstance(command, list) or not command:
        print("error: invalid mapped command in intents.json", file=sys.stderr)
        return 2

    if command[0] != "tc":
        print(f"error: unsupported mapped executable: {command[0]}", file=sys.stderr)
        return 2

    command_to_run = list(command)
    if len(command_to_run) >= 2 and command_to_run[1] == "save":
        has_context_file = "--context-file" in command_to_run
        has_intent = "--intent" in command_to_run
        has_decisions = "--decisions" in command_to_run
        intent_value = ""
        decisions_value = ""
        if has_intent:
            idx = command_to_run.index("--intent")
            if idx + 1 < len(command_to_run):
                intent_value = command_to_run[idx + 1].strip()
        if has_decisions:
            idx = command_to_run.index("--decisions")
            if idx + 1 < len(command_to_run):
                decisions_value = command_to_run[idx + 1].strip()
        placeholder_like = (
            intent_value.startswith("<")
            or decisions_value.startswith("<")
            or intent_value in {"", "intent-delta", "<intent-delta>"}
            or decisions_value in {"", "decisions", "<decisions>"}
        )
        if not has_context_file and not (has_intent and has_decisions):
            print("error: save intent requires semantic context (intent + decisions).", file=sys.stderr)
            print(
                "hint: agent must call structured command, e.g. "
                '`tc agent save --changes-source uncommitted --intent "<intent-delta>" --decisions "<decisions>"`',
                file=sys.stderr,
            )
            return 2
        if placeholder_like:
            print("error: save intent received placeholder semantic fields.", file=sys.stderr)
            print(
                "hint: replace placeholders with real summary from this project's recent LLM discussion, "
                'then run `tc agent save --changes-source uncommitted --intent "..." --decisions "..."`',
                file=sys.stderr,
            )
            return 2
    if len(command_to_run) >= 3 and command_to_run[1] == "agent" and command_to_run[2] == "save":
        has_intent = "--intent" in command_to_run
        has_decisions = "--decisions" in command_to_run
        intent_value = ""
        decisions_value = ""
        if has_intent:
            idx = command_to_run.index("--intent")
            if idx + 1 < len(command_to_run):
                intent_value = command_to_run[idx + 1].strip()
        if has_decisions:
            idx = command_to_run.index("--decisions")
            if idx + 1 < len(command_to_run):
                decisions_value = command_to_run[idx + 1].strip()
        placeholder_like = (
            intent_value.startswith("<")
            or decisions_value.startswith("<")
            or intent_value in {"", "intent-delta", "<intent-delta>"}
            or decisions_value in {"", "decisions", "<decisions>"}
        )
        if not (has_intent and has_decisions):
            print("error: save intent requires semantic context (intent + decisions).", file=sys.stderr)
            print(
                "hint: agent must call structured command, e.g. "
                '`tc agent save --changes-source uncommitted --intent "<intent-delta>" --decisions "<decisions>"`',
                file=sys.stderr,
            )
            return 2
        if placeholder_like:
            print("error: save intent received placeholder semantic fields.", file=sys.stderr)
            print(
                "hint: replace placeholders with real summary from this project's recent LLM discussion, "
                'then run `tc agent save --changes-source uncommitted --intent "..." --decisions "..."`',
                file=sys.stderr,
            )
            return 2

    return main(["--project-root", str(root), *command_to_run[1:]])


def cmd_agent_save(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    argv = [
        "--project-root",
        str(root),
        "save",
        "--intent",
        args.intent,
        "--decisions",
        args.decisions,
    ]
    if args.goal:
        argv.extend(["--goal", args.goal])
    if args.decision_status:
        argv.extend(["--decision-status", args.decision_status])
    if args.rationale:
        argv.extend(["--rationale", args.rationale])
    if args.impact_scope:
        argv.extend(["--impact-scope", args.impact_scope])
    if args.non_code_context:
        argv.extend(["--non-code-context", args.non_code_context])
    if args.validation_outcome:
        argv.extend(["--validation-outcome", args.validation_outcome])
    if args.next_step:
        argv.extend(["--next-step", args.next_step])
    if args.action_items:
        argv.extend(["--action-items", args.action_items])
    if args.open_questions:
        argv.extend(["--open-questions", args.open_questions])
    if args.topic:
        argv.extend(["--topic", args.topic])
    if args.summary:
        argv.extend(["--summary", args.summary])
    if args.kind:
        argv.extend(["--kind", args.kind])
    if args.user:
        argv.extend(["--user", args.user])
    if args.context_file:
        argv.extend(["--context-file", args.context_file])
    if args.changes_source:
        argv.extend(["--changes-source", args.changes_source])
    if args.bootstrap:
        argv.append("--bootstrap")
    if args.auto_bootstrap_if_empty:
        argv.append("--auto-bootstrap-if-empty")
    if args.force_large_save:
        argv.append("--force-large-save")
    if args.large_save_threshold is not None:
        argv.extend(["--large-save-threshold", str(args.large_save_threshold)])
    if args.allow_findings:
        argv.append("--allow-findings")
    return main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tc", description="TeamContext CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__display_version__}")
    parser.add_argument("--project-root", help="Project root (default: current directory)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize TeamContext layout and lock")
    p_init.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_init.add_argument("--vendor-ref", help="OpenViking git ref to pin (default: main)")
    p_init.set_defaults(func=cmd_init)

    p_sync = sub.add_parser("sync", help="Refresh local sync state and index")
    p_sync.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_sync.add_argument("--json", action="store_true", help="Print machine-readable JSON output")
    p_sync.set_defaults(func=cmd_sync)

    p_save = sub.add_parser("save", help="Auto-save recent workspace context for agents")
    p_save.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_save.add_argument("--kind", choices=["decision", "pattern", "runbook"], default="pattern")
    p_save.add_argument("--topic", help="Optional topic override")
    p_save.add_argument("--summary", help="Optional summary override")
    p_save.add_argument("--user", help="Override author id")
    p_save.add_argument("--goal", help="User goal for this session/check-in")
    p_save.add_argument("--intent", help="User intent delta for this check-in")
    p_save.add_argument("--decisions", help="Decisions made in this check-in")
    p_save.add_argument("--decision-status", dest="decision_status", help="Decision status (approved/proposed/blocked)")
    p_save.add_argument("--rationale", help="Decision rationale")
    p_save.add_argument("--impact-scope", dest="impact_scope", help="Affected files/modules/behaviors")
    p_save.add_argument("--non-code-context", dest="non_code_context", help="Important LLM discussion context")
    p_save.add_argument("--validation-outcome", dest="validation_outcome", help="Validation result/outcome")
    p_save.add_argument("--next-step", dest="next_step", help="Next step with owner/command")
    p_save.add_argument("--action-items", dest="action_items", help="Action items")
    p_save.add_argument("--open-questions", dest="open_questions", help="Open questions/risks")
    p_save.add_argument(
        "--context-file",
        help="Session context markdown file (default: .tc/state/session_context.md)",
    )
    p_save.add_argument(
        "--bootstrap",
        action="store_true",
        help="Capture baseline context from the whole current workspace (recommended once for existing projects)",
    )
    p_save.add_argument(
        "--auto-bootstrap-if-empty",
        action="store_true",
        help="If incremental save finds no changes and no shared history exists, auto-run baseline capture",
    )
    p_save.add_argument(
        "--large-save-threshold",
        type=int,
        default=1000,
        help="Safety threshold for file-count in bootstrap mode (default: 1000)",
    )
    p_save.add_argument(
        "--force-large-save",
        action="store_true",
        help="Allow bootstrap save even when it exceeds the safety threshold",
    )
    p_save.add_argument("--allow-findings", action="store_true", help="Allow secret scan findings")
    p_save.add_argument(
        "--changes-source",
        choices=["workspace", "uncommitted", "auto"],
        default="workspace",
        help="Change detection source: workspace snapshot delta, git uncommitted files, or auto",
    )
    p_save.set_defaults(func=cmd_save)

    p_commit = sub.add_parser("commit", help="Generate changelog + candidate artifacts")
    p_commit.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_commit.add_argument("--topic", required=True, help="Topic slug/title for this publication")
    p_commit.add_argument("--summary", required=True, help="Short summary of changes and rationale")
    p_commit.add_argument("--kind", choices=["decision", "pattern", "runbook"], default="decision")
    p_commit.add_argument("--user", help="Override author id")
    p_commit.add_argument("--allow-findings", action="store_true", help="Allow secret scan findings")
    p_commit.set_defaults(func=cmd_commit)

    p_doctor = sub.add_parser("doctor", help="Diagnose setup and environment")
    p_doctor.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_doctor.add_argument("--quiet", action="store_true", help="Suppress detail output")
    p_doctor.set_defaults(func=cmd_doctor)

    p_status = sub.add_parser("status", help="Show TeamContext content and sync status")
    p_status.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify-context", help="Verify recent changelog files contain usable semantic context")
    p_verify.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_verify.add_argument("--latest", type=int, default=1, help="Number of most recent changelog files to verify")
    p_verify.set_defaults(func=cmd_verify_context)

    p_agent = sub.add_parser("agent", help="Agent-oriented intent execution")
    agent_sub = p_agent.add_subparsers(dest="agent_command", required=True)
    p_agent_run = agent_sub.add_parser("run", help="Execute mapped command for an intent")
    p_agent_run.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_agent_run.add_argument("intent", nargs="+", help='Intent text, e.g. "sync latest context"')
    p_agent_run.set_defaults(func=cmd_agent_run)
    p_agent_save = agent_sub.add_parser("save", help="Structured save with required semantic context")
    p_agent_save.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_agent_save.add_argument("--intent", required=True, help="User intent delta for this check-in")
    p_agent_save.add_argument("--decisions", required=True, help="Decisions made in this check-in")
    p_agent_save.add_argument("--goal", help="User goal for this session/check-in")
    p_agent_save.add_argument("--decision-status", dest="decision_status", help="Decision status (approved/proposed/blocked)")
    p_agent_save.add_argument("--rationale", help="Decision rationale")
    p_agent_save.add_argument("--impact-scope", dest="impact_scope", help="Affected files/modules/behaviors")
    p_agent_save.add_argument("--non-code-context", dest="non_code_context", help="Important LLM discussion context")
    p_agent_save.add_argument("--validation-outcome", dest="validation_outcome", help="Validation result/outcome")
    p_agent_save.add_argument("--next-step", dest="next_step", help="Next step with owner/command")
    p_agent_save.add_argument("--action-items", dest="action_items", help="Action items")
    p_agent_save.add_argument("--open-questions", dest="open_questions", help="Open questions/risks")
    p_agent_save.add_argument("--kind", choices=["decision", "pattern", "runbook"], default="pattern")
    p_agent_save.add_argument("--topic", help="Optional topic override")
    p_agent_save.add_argument("--summary", help="Optional summary override")
    p_agent_save.add_argument("--user", help="Override author id")
    p_agent_save.add_argument("--context-file", help="Session context markdown file")
    p_agent_save.add_argument(
        "--changes-source",
        choices=["workspace", "uncommitted", "auto"],
        default="auto",
        help="Change detection source (default: auto -> uncommitted in git repo, else workspace)",
    )
    p_agent_save.add_argument("--bootstrap", action="store_true", help="Capture baseline context from full workspace")
    p_agent_save.add_argument(
        "--auto-bootstrap-if-empty",
        action="store_true",
        help="If no incremental changes and no history exists, auto-run baseline capture",
    )
    p_agent_save.add_argument("--force-large-save", action="store_true", help="Allow bootstrap save above threshold")
    p_agent_save.add_argument("--large-save-threshold", type=int, default=1000, help="Bootstrap safety threshold")
    p_agent_save.add_argument("--allow-findings", action="store_true", help="Allow secret scan findings")
    p_agent_save.set_defaults(func=cmd_agent_save)

    p_vendor = sub.add_parser("vendor", help="Vendor management commands")
    vendor_sub = p_vendor.add_subparsers(dest="vendor_command", required=True)
    p_vendor_upgrade = vendor_sub.add_parser("upgrade", help="Upgrade pinned OpenViking ref")
    p_vendor_upgrade.add_argument("--project-root", dest="project_root_local", help="Project root (default: current directory)")
    p_vendor_upgrade.add_argument("--ref", required=True, help="Tag, branch, or commit to checkout")
    p_vendor_upgrade.set_defaults(func=cmd_vendor_upgrade)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
