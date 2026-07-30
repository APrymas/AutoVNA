from __future__ import annotations

import shutil
import sys
from pathlib import Path


def python_command(base_dir: Path) -> list[str] | None:
    if not getattr(sys, "frozen", False):
        return [sys.executable]

    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        candidates += [
            base_dir / "python_runtime" / "python.exe",
            Path(sys.executable).resolve().parent / "python_runtime" / "python.exe",
        ]
    else:
        candidates += [
            base_dir / "python_runtime" / "bin" / "python3",
            base_dir / "python_runtime" / "python3",
            Path(sys.executable).resolve().parent / "python_runtime" / "bin" / "python3",
        ]
    for path in candidates:
        if path.exists():
            return [str(path)]

    if sys.platform.startswith("win"):
        py = shutil.which("py")
        if py:
            return [py, "-3"]
        python = shutil.which("python")
        if python:
            return [python]
    else:
        for name in ("python3", "python"):
            found = shutil.which(name)
            if found:
                return [found]
    return None
