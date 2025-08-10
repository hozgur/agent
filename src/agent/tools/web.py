from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from .base import BaseTool, ToolResult


class WebTool(BaseTool):
    def fetch(self, url: str, user_agent: Optional[str] = None, timeout: int = 30, dry_run: bool = False) -> ToolResult:
        if dry_run:
            return ToolResult(ok=True, stdout="", stderr="", exit_code=0, extra={"planned_url": url})
        headers = {"User-Agent": user_agent or "natural-agent/0.1"}
        r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = self.outputs_dir / f"download_{timestamp}.html"
        out_path.write_text(r.text, encoding="utf-8")
        return ToolResult(ok=True, stdout=str(out_path), stderr="", exit_code=0, artifact_path=out_path)

    def extract_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(" ")
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())


