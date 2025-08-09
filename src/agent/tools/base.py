from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class ToolResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: int
    artifact_path: Optional[Path] = None
    extra: Optional[Dict[str, Any]] = None


class BaseTool:
    def __init__(self, workspace_dir: Path, outputs_dir: Path, logs_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.outputs_dir = outputs_dir
        self.logs_dir = logs_dir
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


