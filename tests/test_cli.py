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

    def test_init_creates_expected_layout_and_files(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            rc = cli.main(["--project-root", str(self.root), "init"])
        self.assertEqual(rc, 0)

        self.assertTrue((self.root / ".tc" / "config.yaml").exists())
        self.assertTrue((self.root / ".tc" / "lock.json").exists())
        self.assertTrue((self.root / ".tc" / "agent" / "bootstrap_prompt.md").exists())
        self.assertTrue((self.root / ".tc" / "agent" / "workflow.md").exists())
        self.assertTrue((self.root / ".viking" / "agfs" / "shared" / "changelog").exists())
        self.assertTrue((self.root / ".gitignore").exists())

    def test_sync_creates_state_and_index(self) -> None:
        with mock.patch.object(cli, "_maybe_clone_vendor", return_value=(False, "skipped")):
            cli.main(["--project-root", str(self.root), "init"])

        shared_file = self.root / ".viking" / "agfs" / "shared" / "decisions" / "d1.md"
        shared_file.write_text("# d1\n", encoding="utf-8")

        rc = cli.main(["--project-root", str(self.root), "sync"])
        self.assertEqual(rc, 0)

        state_path = self.root / ".tc" / "state" / "sync_state.json"
        index_path = self.root / ".viking" / "index" / "index.txt"
        self.assertTrue(state_path.exists())
        self.assertTrue(index_path.exists())

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn(".viking/agfs/shared/decisions/d1.md", state["files"])
        index_text = index_path.read_text(encoding="utf-8")
        self.assertIn("engine_imported=", index_text)

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

        rc = cli.main(["--project-root", str(self.root), "save"])
        self.assertEqual(rc, 0)

        changelog_files = list((self.root / ".viking" / "agfs" / "shared" / "changelog").glob("*.md"))
        candidate_files = list((self.root / ".viking" / "agfs" / "shared" / "candidates").glob("*.md"))
        self.assertTrue(changelog_files)
        self.assertTrue(candidate_files)
        self.assertTrue((self.root / ".tc" / "state" / "save_state.json").exists())


if __name__ == "__main__":
    unittest.main()
