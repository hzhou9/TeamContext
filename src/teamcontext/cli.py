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

from teamcontext.engine import OpenVikingEngine

DEFAULT_VENDOR_REPO = "https://github.com/openviking/openviking.git"
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
    bootstrap_path, workflow_path = _write_agent_files(paths)

    print(f"Initialized TeamContext in {root}")
    print(f"- config: {paths.config_path}")
    print(f"- lock: {paths.lock_path}")
    print(f"- vendor: {clone_msg}")
    print(f"- agent bootstrap: {bootstrap_path}")
    print(f"- agent workflow: {workflow_path}")
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


def _sync_snapshot(paths: TcPaths) -> tuple[str | None, int]:
    state_path = _index_state_path(paths)
    if not state_path.exists():
        return None, 0
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    updated_at = payload.get("updated_at")
    files = payload.get("files", {})
    file_count = len(files) if isinstance(files, dict) else 0
    return updated_at if isinstance(updated_at, str) else None, file_count


def _bootstrap_prompt(paths: TcPaths) -> str:
    return (
        "Read the following TeamContext sources before coding:\n"
        f"- {paths.shared_dir / 'decisions'}\n"
        f"- {paths.shared_dir / 'patterns'}\n"
        f"- {paths.shared_dir / 'runbooks'}\n"
        f"- {paths.index_dir / 'index.txt'}\n"
        "Then do this before writing code:\n"
        "- Summarize the constraints and decisions you will follow.\n"
        "- List exactly which files you read.\n"
        "- If context is missing or conflicting, ask clarifying questions first."
    )


def _write_agent_files(paths: TcPaths) -> tuple[Path, Path]:
    agent_dir = paths.tc_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_path = agent_dir / "bootstrap_prompt.md"
    workflow_path = agent_dir / "workflow.md"
    bootstrap_path.write_text(_bootstrap_prompt(paths) + "\n", encoding="utf-8")
    workflow_path.write_text(
        "\n".join(
            [
                "# TeamContext Agent Workflow",
                "",
                "Use these intent->command mappings in vibe coding sessions:",
                "",
                '- User says: "save recent context to tc"',
                "- Run: `tc save`",
                "",
                '- User says: "sync latest context"',
                "- Run: `tc sync --json`",
                "",
                "Then summarize key deltas from JSON output for the user.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return bootstrap_path, workflow_path


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
    sample = (added + modified + deleted)[:8]
    sample_text = ", ".join(sample) if sample else "none"
    return (
        f"Auto-saved recent workspace progress: {total} changed files "
        f"({len(added)} added, {len(modified)} modified, {len(deleted)} deleted). "
        f"Key files: {sample_text}."
    )


def cmd_sync(args: argparse.Namespace) -> int:
    root = _resolve_root(_project_root_from_args(args))
    paths = TcPaths.for_root(root)
    _ensure_base_dirs(paths)

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
        "bootstrap_prompt": _bootstrap_prompt(paths),
    }
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


def _write_candidate(paths: TcPaths, kind: str, topic: str, summary: str, user: str, day: date) -> Path:
    slug = _slugify(topic)
    out = paths.shared_dir / "candidates" / f"{day.isoformat()}-{user}-{kind}-{slug}.md"
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
                "## Review Notes",
                "- pending review",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def _write_commit_artifacts(
    *, paths: TcPaths, root: Path, topic: str, summary: str, user: str, kind: str, day: date
) -> tuple[Path, Path]:
    changelog_name = f"{day.isoformat()}-{user}-{_slugify(topic)}.md"
    changelog_path = paths.shared_dir / "changelog" / changelog_name
    candidate_path = _write_candidate(paths, kind, topic, summary, user, day)
    changelog_path.write_text(
        "\n".join(
            [
                f"# Changelog: {topic}",
                "",
                f"- date: {day.isoformat()}",
                f"- author: {user}",
                "",
                "## What changed",
                summary,
                "",
                "## Candidate generated",
                str(candidate_path.relative_to(root)),
            ]
        )
        + "\n",
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
        paths=paths, root=root, topic=topic, summary=summary, user=user, kind=args.kind, day=day
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

    before = _load_save_state(paths)
    after = _tracked_workspace_files(root)
    added, modified, deleted = _workspace_diff(before, after)
    changed = added + modified + deleted
    if not changed:
        print("No new workspace changes since last save.")
        return 0

    user = _slugify(args.user or getpass.getuser())
    topic = args.topic.strip() if args.topic else _auto_topic_from_changes(changed)
    summary = args.summary.strip() if args.summary else _auto_summary(added, modified, deleted)
    day = date.today()
    changelog_path, candidate_path = _write_commit_artifacts(
        paths=paths, root=root, topic=topic, summary=summary, user=user, kind=args.kind, day=day
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
    print(f"- topic: {topic}")
    print(f"- changed files: {len(changed)}")
    print(f"- changelog: {changelog_path}")
    print(f"- candidate: {candidate_path}")
    print("Agent usage:")
    print('- before push, you can say: "save recent context to tc"')
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tc", description="TeamContext CLI")
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
    p_save.add_argument("--allow-findings", action="store_true", help="Allow secret scan findings")
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
