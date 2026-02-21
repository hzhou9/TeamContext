from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from teamcontext.engine import OpenVikingEngine


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.index_file = self.root / "index.txt"
        self.shared_file = self.root / "a.md"
        self.shared_file.write_text("# a\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_index_uses_module_level_api_when_available(self) -> None:
        calls: list[str] = []

        fake = types.SimpleNamespace()

        def index_shared_docs(shared_paths: list[str], index_path: str, **_: object) -> None:
            calls.extend(shared_paths)
            self.assertEqual(index_path, str(self.root))

        fake.index_shared_docs = index_shared_docs

        engine = OpenVikingEngine(self.root)
        with mock.patch.object(engine, "_import_openviking", return_value=fake):
            result = engine.index_shared_docs([self.shared_file], self.root, self.index_file)

        self.assertTrue(result.ok)
        self.assertIn("called module.index_shared_docs", result.message)
        self.assertEqual(calls, [str(self.shared_file)])
        self.assertIn("engine_api=called module.index_shared_docs", self.index_file.read_text(encoding="utf-8"))

    def test_index_falls_back_when_api_missing(self) -> None:
        fake = types.SimpleNamespace()

        engine = OpenVikingEngine(self.root)
        with mock.patch.object(engine, "_import_openviking", return_value=fake):
            result = engine.index_shared_docs([self.shared_file], self.root, self.index_file)

        self.assertTrue(result.ok)
        self.assertIn("fallback writer", result.message)
        self.assertIn("engine_api=no known index API", self.index_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
