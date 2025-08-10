from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult


class PythonExecTool(BaseTool):
    def run_script(self, code: str, tmp_dir: Path, dry_run: bool = False) -> ToolResult:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        script_path = tmp_dir / f"script_{timestamp}.py"
        stdout_path = self.logs_dir / f"py_stdout_{timestamp}.log"
        stderr_path = self.logs_dir / f"py_stderr_{timestamp}.log"

        if dry_run:
            return ToolResult(ok=True, stdout="", stderr="", exit_code=0, extra={"planned_script": str(script_path)})

        tmp_dir.mkdir(parents=True, exist_ok=True)
        script_path.write_text(code, encoding="utf-8")

        proc = subprocess.Popen(
            ["python3", str(script_path)],
            cwd=str(self.workspace_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate()
        exit_code = proc.returncode
        stdout_path.write_text(out or "", encoding="utf-8")
        stderr_path.write_text(err or "", encoding="utf-8")
        return ToolResult(ok=exit_code == 0, stdout=out or "", stderr=err or "", exit_code=exit_code, extra={
            "script_path": str(script_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        })


