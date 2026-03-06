"""TeamContext package."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["__version__", "__build_id__", "__display_version__"]
__version__ = "0.1.0"


def _build_id_from_git(repo_root: Path) -> str | None:
    try:
        ts = subprocess.check_output(
            ["git", "-C", str(repo_root), "show", "-s", "--format=%ct", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        short = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short=10", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if not ts or not short:
            return None
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return f"{dt.strftime('%Y%m%d%H%M%S')}-{short}"
    except Exception:
        return None


def _build_id_fallback(module_file: Path) -> str:
    dt = datetime.fromtimestamp(module_file.stat().st_mtime, tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S")


def _compute_build_id() -> str:
    env_build = os.getenv("TEAMCONTEXT_BUILD_ID", "").strip()
    if env_build:
        return env_build

    module_file = Path(__file__).resolve()
    # /repo/src/teamcontext/__init__.py -> /repo
    repo_root = module_file.parents[2]
    git_build = _build_id_from_git(repo_root)
    if git_build:
        return git_build
    return _build_id_fallback(module_file)


__build_id__ = _compute_build_id()
__display_version__ = f"{__version__}+{__build_id__}"
