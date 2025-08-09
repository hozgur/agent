from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List


def sanitize_filename(name: str, max_len: int = 120) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return cleaned[:max_len] or "artifact"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def chunk_text(text: str, chunk_size: int = 8000, overlap: int = 200) -> List[str]:
    if chunk_size <= 0:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def ensure_within_workspace(path: Path, workspace_dir: Path) -> Path:
    resolved = path.resolve()
    workspace_dir = workspace_dir.resolve()
    if workspace_dir not in resolved.parents and resolved != workspace_dir:
        raise ValueError("Operation outside workspace is not allowed")
    return resolved


