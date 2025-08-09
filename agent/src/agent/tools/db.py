from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .base import BaseTool, ToolResult


@dataclass
class QueryRequest:
    url: str
    sql: str
    out_base_name: str = "query_result"


class DBTool(BaseTool):
    def _ensure_driver(self, url: str, dry_run: bool = False) -> None:
        lower = url.lower()
        target_pkg = None
        if "+psycopg2" in lower:
            try:
                import psycopg2  # type: ignore
            except Exception:
                target_pkg = "psycopg2-binary"
        elif "+pymysql" in lower:
            try:
                import pymysql  # type: ignore
            except Exception:
                target_pkg = "pymysql"
        elif "+pyodbc" in lower:
            try:
                import pyodbc  # type: ignore
            except Exception:
                target_pkg = "pyodbc"
        if target_pkg and not dry_run:
            import subprocess
            subprocess.run(["pip", "install", target_pkg], check=False)

    def _engine(self, url: str, dry_run: bool = False) -> Engine:
        self._ensure_driver(url, dry_run=dry_run)
        return create_engine(url)

    def query_to_files(self, req: QueryRequest, dry_run: bool = False) -> ToolResult:
        csv_path = self.outputs_dir / f"{req.out_base_name}.csv"
        parquet_path = self.outputs_dir / f"{req.out_base_name}.parquet"
        if dry_run:
            return ToolResult(ok=True, stdout="", stderr="", exit_code=0, extra={
                "planned": {
                    "url": req.url,
                    "sql": req.sql,
                    "csv": str(csv_path),
                    "parquet": str(parquet_path),
                }
            })

        with self._engine(req.url).connect() as conn:
            df = pd.read_sql(text(req.sql), conn)
        df.to_csv(csv_path, index=False)
        df.to_parquet(parquet_path, index=False)
        return ToolResult(ok=True, stdout=f"Rows: {len(df)}", stderr="", exit_code=0, extra={
            "csv": str(csv_path),
            "parquet": str(parquet_path),
            "rows": len(df),
        })


