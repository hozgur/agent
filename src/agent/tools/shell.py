from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .base import BaseTool, ToolResult


class ShellTool(BaseTool):
    def run(self, command: str, cwd: Optional[Path] = None, env: Optional[dict] = None, dry_run: bool = False, timeout_sec: Optional[float] = None) -> ToolResult:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        stdout_path = self.logs_dir / f"stdout_{timestamp}.log"
        stderr_path = self.logs_dir / f"stderr_{timestamp}.log"

        if dry_run:
            return ToolResult(ok=True, stdout="", stderr="", exit_code=0, extra={"planned_command": command, "timeout_sec": timeout_sec})

        cwd = cwd or self.workspace_dir
        # Ensure we run inside workspace
        cwd = cwd.resolve()
        if self.workspace_dir.resolve() not in cwd.parents and cwd != self.workspace_dir.resolve():
            return ToolResult(ok=False, stdout="", stderr="Refusing to run outside workspace", exit_code=1)

        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout_sec)
            exit_code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            err = (err or "") + "\n[ShellTool] Timed out"
            exit_code = 124
            timed_out = True
        stdout_path.write_text(out or "", encoding="utf-8")
        stderr_path.write_text(err or "", encoding="utf-8")
        return ToolResult(ok=exit_code == 0, stdout=out or "", stderr=err or "", exit_code=exit_code, extra={
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "timed_out": timed_out,
        })


