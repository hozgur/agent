from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .config import AgentSettings
from .llm import LLMClient
from .reporter import StepRecord, generate_markdown_report, save_report
from .tools.shell import ShellTool
from .tools.python_exec import PythonExecTool
from .tools.packages import PackagesTool
from .tools.db import DBTool, QueryRequest
from .tools.web import WebTool
from .utils import chunk_text


USER_INSTRUCTION_SYSTEM = (
    "You are an automation planner. Given a natural language goal, you will produce a minimal step-by-step plan using available tools: shell, python, web, db. Return concise steps."
)


@dataclass
class ExecutionContext:
    settings: AgentSettings
    llm: LLMClient
    logger: logging.Logger


class Orchestrator:
    def __init__(self, ctx: ExecutionContext):
        self.ctx = ctx
        self.shell = ShellTool(ctx.settings.workspace_dir, ctx.settings.outputs_dir, ctx.settings.logs_dir)
        self.pyexec = PythonExecTool(ctx.settings.workspace_dir, ctx.settings.outputs_dir, ctx.settings.logs_dir)
        self.packages = PackagesTool(ctx.settings.workspace_dir, ctx.settings.outputs_dir, ctx.settings.logs_dir)
        self.db = DBTool(ctx.settings.workspace_dir, ctx.settings.outputs_dir, ctx.settings.logs_dir)
        self.web = WebTool(ctx.settings.workspace_dir, ctx.settings.outputs_dir, ctx.settings.logs_dir)

    def ask_missing_parameters(self, goal: str) -> Optional[List[str]]:
        questions_prompt = (
            "Given the user's goal, list up to 3 short, critical questions needed to safely execute."
            "Respond as numbered lines only, or 'none' if sufficient.\nGoal: "
            + goal
        )
        resp = self.ctx.llm.complete(USER_INSTRUCTION_SYSTEM, questions_prompt)
        lines = [l.strip() for l in resp.strip().splitlines() if l.strip()]
        if any(l.lower().startswith("none") for l in lines):
            return None
        return lines[:3]

    def confirm_if_needed(self, plan_text: str, auto_yes: bool, dry_run: bool) -> bool:
        if auto_yes or dry_run:
            return True
        risky = any(k in plan_text.lower() for k in ["apt", "pip install", "rm -rf", "systemctl", "service "])
        return not risky

    def plan(self, goal: str) -> str:
        prompt = f"Goal: {goal}\nReturn a short plan with steps using shell/python/web/db as needed."
        plan = self.ctx.llm.complete(USER_INSTRUCTION_SYSTEM, prompt, max_tokens=400)
        return plan

    def execute(self, goal: str) -> Tuple[bool, List[StepRecord], List[Path], str]:
        started = datetime.utcnow()
        steps: List[StepRecord] = []
        artifacts: List[Path] = []

        # Ask up to 3 clarifying questions first
        questions = self.ask_missing_parameters(goal)
        if questions:
            q_text = " ".join(f"[{i+1}] {q}" for i, q in enumerate(questions))
            return False, steps, artifacts, f"Missing critical details. Please answer: {q_text}. Use 'agent repl' for interactive flow or include answers in the goal."

        plan_text = self.plan(goal)
        self.ctx.logger.info("Plan:\n%s", plan_text)

        if not self.confirm_if_needed(plan_text, self.ctx.settings.auto_yes, self.ctx.settings.dry_run):
            return False, steps, artifacts, "Confirmation required for potentially risky operations. Re-run with --auto-yes or refine the request."

        # Simple heuristic routing
        lowered = goal.lower()
        try:
            if "http" in lowered:
                # Web fetch + summarize
                url_match = re.search(r"https?://\S+", goal)
                if not url_match:
                    raise ValueError("URL not found in goal.")
                url = url_match.group(0)
                res = self.web.fetch(url, dry_run=self.ctx.settings.dry_run)
                steps.append(StepRecord(name="web.fetch", command=f"GET {url}", exit_code=res.exit_code, stdout_path=Path(res.extra["stdout_path"]) if res.extra and "stdout_path" in res.extra else None, stderr_path=None, success=res.ok))
                if res.artifact_path:
                    artifacts.append(res.artifact_path)
                    if not self.ctx.settings.dry_run:
                        html = res.artifact_path.read_text(encoding="utf-8")
                        text = self.web.extract_text(html)
                        chunks = chunk_text(text, 8000)
                        summary = self.ctx.llm.summarize_chunks(
                            "Summarize the content into concise bullet points with key changes or highlights.",
                            chunks,
                        )
                        out_path = self.ctx.settings.outputs_dir / "web_summary.md"
                        out_path.write_text(summary, encoding="utf-8")
                        artifacts.append(out_path)
                        steps.append(StepRecord(name="llm.summarize", command=None, exit_code=0, stdout_path=None, stderr_path=None, success=True))
                    finished = datetime.utcnow()
                    report = generate_markdown_report("Web Task", goal, steps, artifacts, started, finished)
                    report_path = save_report(self.ctx.settings.reports_dir, "web_task", report)
                    return True, steps, artifacts + [report_path], "Completed"

            if "postgres" in lowered or "mysql" in lowered or "sqlite" in lowered or "mssql" in lowered:
                # Extract URL and basic query intent; fall back to ask questions
                url_match = re.search(r"\b[a-zA-Z0-9+]+://[^\s']+", goal)
                if not url_match:
                    qs = self.ask_missing_parameters(goal)
                    return False, steps, artifacts, "Missing connection URL. " + (" ".join(qs) if qs else "")
                url = url_match.group(0)
                # naive SQL extraction
                sql_match = re.search(r"(?is)\bselect\b[\s\S]+", goal)
                if not sql_match:
                    return False, steps, artifacts, "Missing SQL query. Please provide a SELECT statement."
                sql = sql_match.group(0)
                req = QueryRequest(url=url, sql=sql)
                res = self.db.query_to_files(req, dry_run=self.ctx.settings.dry_run)
                steps.append(StepRecord(name="db.query", command=f"SQL to files", exit_code=res.exit_code, stdout_path=None, stderr_path=None, success=res.ok, notes=res.stdout))
                if res.extra:
                    for k in ("csv", "parquet"):
                        if k in res.extra:
                            artifacts.append(Path(res.extra[k]))
                finished = datetime.utcnow()
                report = generate_markdown_report("DB Task", goal, steps, artifacts, started, finished)
                report_path = save_report(self.ctx.settings.reports_dir, "db_task", report)
                return res.ok, steps, artifacts + [report_path], res.stdout

            if "pandas" in lowered or "dataframe" in lowered:
                code = (
                    "import pandas as pd\n"
                    "df = pd.DataFrame({'a':[1,2,3],'b':[10,20,30]})\n"
                    "df.to_csv('outputs/example_df.csv', index=False)\n"
                    "print(df.head().to_string())\n"
                )
                res = self.pyexec.run_script(code, self.ctx.settings.tmp_dir, dry_run=self.ctx.settings.dry_run)
                steps.append(StepRecord(name="python.exec", command="python3 tmp script", exit_code=res.exit_code, stdout_path=None, stderr_path=None, success=res.ok))
                artifacts.append(self.ctx.settings.outputs_dir / "example_df.csv")
                finished = datetime.utcnow()
                report = generate_markdown_report("Python Script Task", goal, steps, artifacts, started, finished)
                report_path = save_report(self.ctx.settings.reports_dir, "python_task", report)
                return res.ok, steps, artifacts + [report_path], res.stdout

            if "git" in lowered or "jq" in lowered or "apt" in lowered:
                plan = self.packages.plan(
                    apt=[p for p in ["git", "jq"] if p in lowered]
                )
                res = self.packages.ensure(plan, auto_yes=self.ctx.settings.auto_yes, dry_run=self.ctx.settings.dry_run)
                steps.append(StepRecord(name="packages.ensure", command="install packages", exit_code=res.exit_code, stdout_path=None, stderr_path=None, success=res.ok))
                # check versions
                for cmd in ["git --version", "jq --version"]:
                    r = self.shell.run(cmd, dry_run=self.ctx.settings.dry_run)
                    steps.append(StepRecord(name=f"shell: {cmd}", command=cmd, exit_code=r.exit_code, stdout_path=None, stderr_path=None, success=r.ok))
                finished = datetime.utcnow()
                report = generate_markdown_report("Shell Task", goal, steps, artifacts, started, finished)
                report_path = save_report(self.ctx.settings.reports_dir, "shell_task", report)
                return res.ok, steps, artifacts + [report_path], res.stdout

            # Fallback: try to run a safe echo and produce a plan-only report
            finished = datetime.utcnow()
            report = generate_markdown_report("Plan Only", goal, steps, artifacts, started, finished)
            report_path = save_report(self.ctx.settings.reports_dir, "plan_only", report)
            return True, steps, [report_path], "Planned. Provide more specifics to execute."

        except Exception as e:
            self.ctx.logger.exception("Execution error")
            finished = datetime.utcnow()
            report = generate_markdown_report("Task Failed", goal, steps, artifacts, started, finished)
            report_path = save_report(self.ctx.settings.reports_dir, "task_failed", report)
            return False, steps, artifacts + [report_path], str(e)


