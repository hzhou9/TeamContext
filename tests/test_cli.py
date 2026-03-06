from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock
import contextlib

from teamcontext import cli


class TeamContextCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_min_context(self) -> Path:
        context_path = self.root / ".tc" / "state" / "session_context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(
            "\n".join(
                [
                    "# Session Context",
                    "",
                    "## User Intent (Delta)",
                    "Capture this check-in semantic context.",
                    "",
                    "## Decisions Made",
                    "Persist intent/decision before save.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return context_path

    def test_init_creates_expected_layout_and_files(self) -> None:
        out = StringIO()
        with contextlib.redirect_stdout(out):
            with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
                rc = cli.main(["--project-root", str(self.root), "init"])
        self.assertEqual(rc, 0)
        self.assertIn("- teamcontext version: 0.1.0 (build ", out.getvalue())

        self.assertTrue((self.root / ".tc" / "config.yaml").exists())
        self.assertTrue((self.root / ".tc" / "lock.json").exists())
        bootstrap_path = self.root / ".tc" / "agent" / "bootstrap_prompt.md"
        self.assertTrue(bootstrap_path.exists())
        bootstrap_text = bootstrap_path.read_text(encoding="utf-8")
        self.assertIn("TEAMCONTEXT_AGENT_RULES.md", bootstrap_text)
        self.assertIn("If index.txt is missing, run `tc sync` first.", bootstrap_text)
        self.assertIn('report "no approved team context yet"', bootstrap_text)
        self.assertIn("Do not claim context was saved/synced unless a `tc` command actually ran.", bootstrap_text)
        self.assertTrue((self.root / "TEAMCONTEXT_AGENT_RULES.md").exists())
        self.assertTrue((self.root / ".tc" / "agent" / "workflow.md").exists())
        workflow_text = (self.root / ".tc" / "agent" / "workflow.md").read_text(encoding="utf-8")
        self.assertIn('User says: "save context"', workflow_text)
        self.assertIn('User says: "sync context"', workflow_text)
        self.assertIn("tc_command: <exact command>", workflow_text)
        intents_path = self.root / ".tc" / "agent" / "intents.json"
        self.assertTrue(intents_path.exists())
        intents = json.loads(intents_path.read_text(encoding="utf-8"))
        self.assertEqual(intents["default_mode"], "execute")
        self.assertTrue(
            any(
                r["intent"] == "save recent context to tc"
                and r["command"]
                == [
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
                ]
                for r in intents["rules"]
            )
        )
        self.assertTrue(any(r["intent"] == "sync latest context" for r in intents["rules"]))
        self.assertTrue(any(r["intent"] == "save context" for r in intents["rules"]))
        self.assertTrue(any(r["intent"] == "sync context" for r in intents["rules"]))
        self.assertTrue((self.root / ".viking" / "index" / "index.txt").exists())
        self.assertTrue((self.root / ".viking" / "agfs" / "shared" / "changelog").exists())
        self.assertTrue((self.root / ".gitignore").exists())

    def test_version_flag_prints_version(self) -> None:
        out = StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertRegex(out.getvalue(), r"tc 0\.1\.0\+\S+")

    def test_sync_creates_state_and_index(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])

        shared_file = self.root / ".viking" / "agfs" / "shared" / "decisions" / "d1.md"
        shared_file.write_text("# d1\n", encoding="utf-8")

        rc = cli.main(["--project-root", str(self.root), "sync"])
        self.assertEqual(rc, 0)

        state_path = self.root / ".tc" / "state" / "sync_state.json"
        last_sync_path = self.root / ".tc" / "state" / "last_sync.json"
        index_path = self.root / ".viking" / "index" / "index.txt"
        self.assertTrue(state_path.exists())
        self.assertTrue(last_sync_path.exists())
        self.assertTrue(index_path.exists())

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn(".viking/agfs/shared/decisions/d1.md", state["files"])
        index_text = index_path.read_text(encoding="utf-8")
        self.assertIn("engine_imported=", index_text)

    def test_agent_run_sync_intent_executes_mapped_command(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / ".viking" / "agfs" / "shared" / "decisions" / "d1.md").write_text("# d1\n", encoding="utf-8")

        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--project-root", str(self.root), "agent", "run", "sync", "latest", "context"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["shared_files_scanned"], 1)

    def test_agent_run_save_intent_without_semantics_fails(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("touch to create change\n", encoding="utf-8")
        err = StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["--project-root", str(self.root), "agent", "run", "save", "recent", "context", "to", "tc"])
        self.assertEqual(rc, 2)
        self.assertIn("placeholder semantic fields", err.getvalue())

    def test_agent_save_with_semantic_fields_succeeds(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("touch to create change\n", encoding="utf-8")
        rc = cli.main(
            [
                "--project-root",
                str(self.root),
                "agent",
                "save",
                "--intent",
                "Capture this check-in context.",
                "--decisions",
                "Persist this check-in for team handoff.",
            ]
        )
        self.assertEqual(rc, 0)

    def test_sync_json_outputs_machine_readable_payload(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        shared_file = self.root / ".viking" / "agfs" / "shared" / "decisions" / "d1.md"
        shared_file.write_text("# d1\n", encoding="utf-8")
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--project-root", str(self.root), "sync", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["shared_files_scanned"], 1)
        self.assertIn(".viking/agfs/shared/decisions/d1.md", payload["changed_paths"])
        self.assertIn("bootstrap_prompt", payload)

    def test_commit_blocks_on_secret_findings(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])

        rc = cli.main(
            [
                "--project-root",
                str(self.root),
                "commit",
                "--topic",
                "security",
                "--summary",
                "api_key=1234567890123456",
            ]
        )
        self.assertEqual(rc, 2)

    def test_commit_respects_security_config_disable_scan(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])

        config_path = self.root / ".tc" / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        config_text = config_text.replace("secret_scan: true", "secret_scan: false")
        config_path.write_text(config_text, encoding="utf-8")

        rc = cli.main(
            [
                "--project-root",
                str(self.root),
                "commit",
                "--topic",
                "security",
                "--summary",
                "api_key=1234567890123456",
            ]
        )
        self.assertEqual(rc, 0)

    def test_vendor_upgrade_without_vendor_repo_fails_cleanly(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])

        rc = cli.main(["--project-root", str(self.root), "vendor", "upgrade", "--ref", "main"])
        self.assertEqual(rc, 1)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_vendor_upgrade_updates_lock_on_success(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])

        remote_repo = self.root / "remote.git"
        seed_repo = self.root / "seed"
        vendor_repo = self.root / ".tc" / "vendor" / "openviking"

        subprocess.run(["git", "init", "--bare", str(remote_repo)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "init", str(seed_repo)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "TeamContext Test"], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "tc-test@example.com"], cwd=seed_repo, check=True, capture_output=True, text=True)

        (seed_repo / "README.md").write_text("# seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote_repo)], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed_repo, check=True, capture_output=True, text=True)

        (seed_repo / "CHANGELOG.md").write_text("v0.2.0\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "v0.2.0"], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "tag", "v0.2.0"], cwd=seed_repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "origin", "main", "--tags"], cwd=seed_repo, check=True, capture_output=True, text=True)

        subprocess.run(["git", "clone", str(remote_repo), str(vendor_repo)], check=True, capture_output=True, text=True)

        rc = cli.main(["--project-root", str(self.root), "vendor", "upgrade", "--ref", "v0.2.0"])
        self.assertEqual(rc, 0)

        lock = json.loads((self.root / ".tc" / "lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["openviking"]["ref"], "v0.2.0")

        expected_commit = (
            subprocess.run(
                ["git", "rev-list", "-n", "1", "v0.2.0"],
                cwd=vendor_repo,
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
        )
        self.assertEqual(lock["openviking"]["resolved_commit"], expected_commit)

    def test_status_reports_counts_and_sync_state(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])

        (self.root / ".viking" / "agfs" / "shared" / "decisions" / "d1.md").write_text("# d1\n", encoding="utf-8")
        cli.main(["--project-root", str(self.root), "sync"])

        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--project-root", str(self.root), "status"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("TeamContext status", text)
        self.assertIn("- decisions: 1", text)
        self.assertIn("- last sync:", text)

    def test_save_auto_generates_context_artifacts(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "feature.py").write_text("print('v1')\n", encoding="utf-8")
        context_path = self._write_min_context()

        rc = cli.main(["--project-root", str(self.root), "save", "--context-file", str(context_path)])
        self.assertEqual(rc, 0)

        changelog_files = list((self.root / ".viking" / "agfs" / "shared" / "changelog").glob("*.md"))
        candidate_files = list((self.root / ".viking" / "agfs" / "shared" / "candidates").glob("*.md"))
        self.assertTrue(changelog_files)
        self.assertTrue(candidate_files)
        self.assertTrue((self.root / ".tc" / "state" / "save_state.json").exists())

    def test_save_after_init_without_changes_requires_context(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--project-root", str(self.root), "save"])
        self.assertEqual(rc, 2)
        self.assertIn("Session context is required but missing or empty.", out.getvalue())

    def test_save_after_init_without_changes_is_noop_with_context(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        context_path = self._write_min_context()
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--project-root", str(self.root), "save", "--context-file", str(context_path)])
        self.assertEqual(rc, 0)
        self.assertIn("No new workspace changes since last save.", out.getvalue())

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_save_uncommitted_source_captures_git_working_tree_changes(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True, text=True)
        (self.root / "apps" / "svc").mkdir(parents=True, exist_ok=True)
        (self.root / "apps" / "svc" / "index.ts").write_text("export const v = 1;\n", encoding="utf-8")
        context_path = self._write_min_context()
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--changes-source",
                    "uncommitted",
                    "--context-file",
                    str(context_path),
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn("- changes source: uncommitted", out.getvalue())
        self.assertNotIn("No uncommitted workspace changes to save.", out.getvalue())

    def test_save_bootstrap_captures_baseline_after_init(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("existing project baseline\n", encoding="utf-8")
        context_path = self._write_min_context()
        rc = cli.main(
            [
                "--project-root",
                str(self.root),
                "save",
                "--bootstrap",
                "--context-file",
                str(context_path),
            ]
        )
        self.assertEqual(rc, 0)
        changelog_files = list((self.root / ".viking" / "agfs" / "shared" / "changelog").glob("*.md"))
        candidate_files = list((self.root / ".viking" / "agfs" / "shared" / "candidates").glob("*.md"))
        self.assertTrue(changelog_files)
        self.assertTrue(candidate_files)

    def test_save_bootstrap_blocks_when_over_threshold(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("existing project baseline\n", encoding="utf-8")
        context_path = self._write_min_context()
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--bootstrap",
                    "--large-save-threshold",
                    "0",
                    "--context-file",
                    str(context_path),
                ]
            )
        self.assertEqual(rc, 3)
        self.assertIn("Bootstrap save blocked", out.getvalue())

    def test_save_bootstrap_force_large_save_allows_when_over_threshold(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("existing project baseline\n", encoding="utf-8")
        context_path = self._write_min_context()
        rc = cli.main(
            [
                "--project-root",
                str(self.root),
                "save",
                "--bootstrap",
                "--large-save-threshold",
                "0",
                "--force-large-save",
                "--context-file",
                str(context_path),
            ]
        )
        self.assertEqual(rc, 0)

    def test_save_requires_context_even_with_summary(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("existing project baseline\n", encoding="utf-8")
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--bootstrap",
                    "--summary",
                    "manual summary only",
                ]
            )
        self.assertEqual(rc, 2)
        self.assertIn("Session context is required but missing or empty.", out.getvalue())

    def test_save_auto_bootstrap_if_empty_creates_baseline_when_no_shared_history(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("existing project baseline\n", encoding="utf-8")
        context_path = self._write_min_context()
        rc = cli.main(
            [
                "--project-root",
                str(self.root),
                "save",
                "--auto-bootstrap-if-empty",
                "--context-file",
                str(context_path),
            ]
        )
        self.assertEqual(rc, 0)
        changelog_files = list((self.root / ".viking" / "agfs" / "shared" / "changelog").glob("*.md"))
        candidate_files = list((self.root / ".viking" / "agfs" / "shared" / "candidates").glob("*.md"))
        self.assertTrue(changelog_files)
        self.assertTrue(candidate_files)

    def test_save_with_explicit_context_file_fails_when_missing(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("touch to create change\n", encoding="utf-8")
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--context-file",
                    ".tc/state/session_context.md",
                ]
            )
        self.assertEqual(rc, 2)
        self.assertIn("Session context is required but missing or empty.", out.getvalue())

    def test_save_with_explicit_context_file_fails_when_missing_intent_or_decision(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        context_path = self.root / ".tc" / "state" / "session_context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(
            "\n".join(
                [
                    "# Session Context",
                    "",
                    "## User Intent (Delta)",
                    "Validate output only.",
                    "",
                    "## Open Questions",
                    "Need to decide strictness later.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text("touch to create change\n", encoding="utf-8")
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--context-file",
                    str(context_path),
                ]
            )
        self.assertEqual(rc, 2)
        self.assertIn("Session context is incomplete for check-in sync.", out.getvalue())
        self.assertIn("User Intent (Delta) and Decisions Made", out.getvalue())

    def test_save_fails_when_impact_scope_conflicts_with_changed_files(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        for i in range(12):
            path = self.root / "apps" / "api" / f"file_{i}.ts"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"export const x{i} = {i};\n", encoding="utf-8")

        context_path = self.root / ".tc" / "state" / "session_context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(
            "\n".join(
                [
                    "# Session Context",
                    "",
                    "## User Intent (Delta)",
                    "Capture current checkpoint.",
                    "",
                    "## Decisions Made",
                    "Persist team sync note.",
                    "",
                    "## Impact Scope",
                    "TeamContext metadata and save workflow only; no application code changes.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--bootstrap",
                    "--force-large-save",
                    "--context-file",
                    str(context_path),
                ]
            )
        self.assertEqual(rc, 2)
        self.assertIn("Session context conflicts with detected workspace changes.", out.getvalue())

    def test_save_with_semantic_flags_writes_context_file_and_succeeds(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        (self.root / "README.md").write_text("touch to create change\n", encoding="utf-8")

        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--intent",
                    "Capture user intent/decision deltas for this check-in.",
                    "--decisions",
                    "Use tc save semantic flags instead of manual context-file editing.",
                    "--rationale",
                    "Agent-first automation with no extra human steps.",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn("context fields: intent_delta=yes", out.getvalue())
        self.assertIn("decisions=yes", out.getvalue())
        context_text = (self.root / ".tc" / "state" / "session_context.md").read_text(encoding="utf-8")
        self.assertIn("## User Intent (Delta)", context_text)
        self.assertIn("Capture user intent/decision deltas", context_text)
        self.assertIn("## Decisions Made", context_text)
        self.assertIn("tc save semantic flags", context_text)

    def test_save_uses_context_file_for_semantic_summary(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        context_path = self.root / ".tc" / "state" / "session_context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(
            "\n".join(
                [
                    "# Session Context",
                    "",
                    "## User Goal",
                    "Implement TC-based context sharing for agent workflows.",
                    "",
                    "## User Intent (Delta)",
                    "Capture non-code conversation context per check-in.",
                    "",
                    "## Decisions Made",
                    "Use tc agent run for strict intent->command execution.",
                    "",
                    "## Decision Rationale",
                    "Avoid tool-side command rewrite drift.",
                    "",
                    "## Non-code Context (LLM Discussion)",
                    "Team discussed why file diffs alone are insufficient for context handoff.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text("touch to create change\n", encoding="utf-8")
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "save",
                    "--context-file",
                    str(context_path),
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn("context fields: intent_delta=yes", out.getvalue())
        self.assertIn("decisions=yes", out.getvalue())
        self.assertIn("rationale=yes", out.getvalue())
        self.assertIn("non_code_context=yes", out.getvalue())
        changelog_files = sorted((self.root / ".viking" / "agfs" / "shared" / "changelog").glob("*.md"))
        self.assertTrue(changelog_files)
        latest_text = changelog_files[-1].read_text(encoding="utf-8")
        self.assertIn("User Intent (Delta):", latest_text)
        self.assertIn("Decisions Made:", latest_text)
        self.assertIn("- user intent (delta):", latest_text)
        self.assertIn("- decision rationale:", latest_text)

    def test_verify_context_fails_when_latest_has_na(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        bad = self.root / ".viking" / "agfs" / "shared" / "changelog" / "2026-01-01-joe-bad.md"
        bad.write_text(
            "\n".join(
                [
                    "# Changelog: bad",
                    "",
                    "## Team Session Context",
                    "- user intent (delta): n/a",
                    "- decisions: n/a",
                    "- decision rationale: n/a",
                    "- non-code context: n/a",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--project-root", str(self.root), "verify-context"])
        self.assertEqual(rc, 1)
        self.assertIn("Context verification FAILED", out.getvalue())

    def test_verify_context_passes_with_valid_latest(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])
        good = self.root / ".viking" / "agfs" / "shared" / "changelog" / "2026-01-01-joe-good.md"
        good.write_text(
            "\n".join(
                [
                    "# Changelog: good",
                    "",
                    "## Team Session Context",
                    "- user intent (delta): capture semantic context",
                    "- decisions: keep strict save flow",
                    "- decision rationale: reduce context drift",
                    "- non-code context: aligned on process",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out = StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--project-root", str(self.root), "verify-context"])
        self.assertEqual(rc, 0)
        self.assertIn("Context verification OK", out.getvalue())


if __name__ == "__main__":
    unittest.main()
