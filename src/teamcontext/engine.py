from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EngineResult:
    ok: bool
    message: str


class OpenVikingEngine:
    """Thin integration layer around a vendored OpenViking checkout."""

    def __init__(self, vendor_repo: Path) -> None:
        self.vendor_repo = vendor_repo

    def health(self) -> EngineResult:
        if not self.vendor_repo.exists():
            return EngineResult(False, "vendor repo path missing")
        if not (self.vendor_repo / ".git").exists():
            return EngineResult(False, "vendor repo is not a git checkout")

        try:
            self._import_openviking()
        except Exception as exc:  # pragma: no cover - import behavior is environment-dependent
            return EngineResult(False, f"import failed: {exc}")
        return EngineResult(True, "import ok")

    def index_shared_docs(self, shared_files: list[Path], root: Path, index_file: Path) -> EngineResult:
        api_message = "api unavailable"
        imported = False
        try:
            module = self._import_openviking()
            imported = True
            api_message = self._try_index_with_module(module, shared_files, root, index_file.parent)
        except Exception as exc:
            api_message = f"import failed: {exc}"

        lines = ["# TeamContext Local Index", ""]
        lines.append(f"- engine_imported={imported}")
        lines.append(f"- engine_api={api_message}")
        for p in shared_files:
            rel = p.relative_to(root)
            stat = p.stat()
            lines.append(f"- {rel} | mtime={int(stat.st_mtime)} | bytes={stat.st_size}")
        index_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        if imported and api_message.startswith("called "):
            return EngineResult(True, f"indexed via OpenViking ({api_message})")
        if imported:
            return EngineResult(True, f"indexed with fallback writer ({api_message})")
        return EngineResult(True, "indexed with fallback writer (OpenViking import unavailable)")

    def _try_index_with_module(
        self, module: Any, shared_files: list[Path], root: Path, index_dir: Path
    ) -> str:
        shared_paths = [str(p) for p in shared_files]
        kwargs = {
            "shared_files": shared_files,
            "shared_paths": shared_paths,
            "root": root,
            "root_path": str(root),
            "index_dir": index_dir,
            "index_path": str(index_dir),
        }

        module_fn = getattr(module, "index_shared_docs", None)
        if callable(module_fn):
            self._invoke_callable(module_fn, kwargs)
            return "called module.index_shared_docs"

        for cls_name in ("OpenVikingEngine", "Engine"):
            cls = getattr(module, cls_name, None)
            if cls is None:
                continue
            obj = self._construct_engine_instance(cls, kwargs)
            method = getattr(obj, "index_shared_docs", None)
            if callable(method):
                self._invoke_callable(method, kwargs)
                return f"called {cls_name}.index_shared_docs"

        return "no known index API"

    def _construct_engine_instance(self, cls: Any, kwargs: dict[str, Any]) -> Any:
        for candidate in (
            {"vendor_path": str(self.vendor_repo)},
            {"vendor_repo": self.vendor_repo},
            {"project_root": kwargs["root_path"]},
            {},
        ):
            try:
                return cls(**candidate)
            except TypeError:
                continue
        return cls()

    def _invoke_callable(self, fn: Any, kwargs: dict[str, Any]) -> Any:
        signature = inspect.signature(fn)
        accepts_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        if accepts_var_kwargs:
            return fn(**kwargs)
        filtered = {name: kwargs[name] for name in signature.parameters if name in kwargs}
        return fn(**filtered)

    def _import_openviking(self) -> Any:
        candidates = [
            self.vendor_repo,
            self.vendor_repo / "src",
            self.vendor_repo / "python",
        ]
        for candidate in candidates:
            if candidate.exists():
                candidate_str = str(candidate)
                if candidate_str not in sys.path:
                    sys.path.insert(0, candidate_str)

        # Import module only to verify integration path is valid.
        import openviking  # type: ignore  # noqa: F401

        return openviking
