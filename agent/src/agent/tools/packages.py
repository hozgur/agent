from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from .base import BaseTool, ToolResult


@dataclass
class PackagePlan:
    apt: List[str]
    pip: List[str]


class PackagesTool(BaseTool):
    def plan(self, apt: Optional[List[str]] = None, pip: Optional[List[str]] = None) -> PackagePlan:
        return PackagePlan(apt=apt or [], pip=pip or [])

    def ensure(self, plan: PackagePlan, auto_yes: bool = False, dry_run: bool = False) -> ToolResult:
        cmds = []
        if plan.apt:
            apt_get = shutil.which("apt-get") or "apt-get"
            cmds.append(f"sudo {apt_get} update -y")
            yes_flag = "-y" if auto_yes else ""
            cmds.append(f"sudo {apt_get} install {yes_flag} " + " ".join(plan.apt))
        if plan.pip:
            pip = shutil.which("pip3") or "pip3"
            yes_flag = "--yes" if auto_yes else ""
            cmds.append(f"{pip} install " + " ".join(plan.pip))

        if dry_run:
            return ToolResult(ok=True, stdout="\n".join(cmds), stderr="", exit_code=0, extra={"planned_commands": cmds})

        stdout_total = []
        stderr_total = []
        for c in cmds:
            proc = subprocess.Popen(c, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, err = proc.communicate()
            stdout_total.append(out or "")
            stderr_total.append(err or "")
            if proc.returncode != 0:
                return ToolResult(ok=False, stdout="\n".join(stdout_total), stderr="\n".join(stderr_total), exit_code=proc.returncode)

        return ToolResult(ok=True, stdout="\n".join(stdout_total), stderr="\n".join(stderr_total), exit_code=0)


