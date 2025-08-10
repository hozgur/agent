from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .utils import write_text, sanitize_filename


@dataclass
class StepRecord:
    name: str
    command: Optional[str]
    exit_code: Optional[int]
    stdout_path: Optional[Path]
    stderr_path: Optional[Path]
    success: bool
    notes: Optional[str] = None


def generate_markdown_report(
    title: str,
    goal: str,
    steps: List[StepRecord],
    outputs: List[Path],
    started_at: datetime,
    finished_at: datetime,
) -> str:
    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- Goal: {goal}")
    lines.append(f"- Started: {started_at.isoformat()}")
    lines.append(f"- Finished: {finished_at.isoformat()}")
    lines.append("")
    lines.append("## Steps")
    for s in steps:
        lines.append(f"- {s.name} | {'OK' if s.success else 'FAIL'}")
        if s.command:
            lines.append(f"  - Command: `{s.command}`")
        if s.exit_code is not None:
            lines.append(f"  - Exit code: {s.exit_code}")
        if s.stdout_path:
            lines.append(f"  - Stdout: `{s.stdout_path}`")
        if s.stderr_path:
            lines.append(f"  - Stderr: `{s.stderr_path}`")
        if s.notes:
            lines.append(f"  - Notes: {s.notes}")
    lines.append("")
    if outputs:
        lines.append("## Artifacts")
        for p in outputs:
            lines.append(f"- `{p}`")
    return "\n".join(lines)


def save_report(reports_dir: Path, title: str, content: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = sanitize_filename(f"{timestamp}_{title}") + ".md"
    path = reports_dir / fname
    write_text(path, content)
    return path


