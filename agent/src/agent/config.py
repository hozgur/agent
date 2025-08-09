from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass
class AgentSettings:
    workspace_dir: Path
    outputs_dir: Path
    reports_dir: Path
    logs_dir: Path
    tmp_dir: Path

    openai_api_key: str
    openai_base_url: Optional[str]
    openai_model: str

    auto_yes: bool = False
    dry_run: bool = False


def load_settings(auto_yes: bool = False, dry_run: bool = False, model: Optional[str] = None) -> AgentSettings:
    load_dotenv(override=False)

    root = Path.cwd()
    workspace = root / "workspace"
    outputs = root / "outputs"
    reports = root / "reports"
    logs = root / "logs"
    tmp = workspace / "tmp"

    for d in (workspace, outputs, reports, logs, tmp):
        d.mkdir(parents=True, exist_ok=True)

    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL")
    default_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing. Set it in .env or environment.")

    effective_model = model or default_model

    return AgentSettings(
        workspace_dir=workspace,
        outputs_dir=outputs,
        reports_dir=reports,
        logs_dir=logs,
        tmp_dir=tmp,
        openai_api_key=api_key,
        openai_base_url=base_url,
        openai_model=effective_model,
        auto_yes=auto_yes,
        dry_run=dry_run,
    )


