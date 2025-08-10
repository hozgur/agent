from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import json
import logging
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
from .utils import sanitize_filename, chunk_text, write_text


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
        started = datetime.now(timezone.utc)
        steps: List[StepRecord] = []
        artifacts: List[Path] = []

        # URL intent: fetch → extract → summarize → report (per ARCHITECTURE.md)
        try:
            if self.ctx.settings.verbose:
                self.ctx.logger.info(f"[step] detect-intent: scanning goal for URL")
            url_match = re.search(r"https?://\S+", goal)
            if url_match:
                url = url_match.group(0)
                if self.ctx.settings.verbose:
                    self.ctx.logger.info(f"[step] web.fetch: {url}")
                fetch_res = self.web.fetch(url, dry_run=self.ctx.settings.dry_run)
                steps.append(
                    StepRecord(
                        name="web.fetch",
                        command=f"GET {url}",
                        exit_code=fetch_res.exit_code,
                        stdout_path=None,
                        stderr_path=None,
                        success=fetch_res.ok,
                        notes=(fetch_res.stdout or fetch_res.stderr),
                    )
                )
                if not fetch_res.ok:
                    raise RuntimeError(fetch_res.stderr or "Fetch failed")
                if self.ctx.settings.dry_run:
                    finished = datetime.now(timezone.utc)
                    report = generate_markdown_report("Web Fetch (dry run)", goal, steps, artifacts, started, finished)
                    report_path = save_report(self.ctx.settings.reports_dir, "web_fetch_dry_run", report)
                    return True, steps, artifacts + [report_path], "Planned web fetch"

                assert fetch_res.artifact_path is not None
                html_path = Path(fetch_res.artifact_path)
                artifacts.append(html_path)
                if self.ctx.settings.verbose:
                    self.ctx.logger.info(f"[step] web.extract-text: {html_path}")
                html = html_path.read_text(encoding="utf-8", errors="ignore")
                text = self.web.extract_text(html)

                summary: str
                if self.ctx.settings.openai_api_key:
                    if self.ctx.settings.verbose:
                        self.ctx.logger.info("[step] summarize: chunk + LLM summarize")
                    chunks = chunk_text(text, chunk_size=6000, overlap=200)
                    summary = self.ctx.llm.summarize_chunks(
                        "Summarize the fetched page into concise bullet points (10-15 bullets).",
                        chunks,
                        max_tokens=600,
                    )
                else:
                    if self.ctx.settings.verbose:
                        self.ctx.logger.info("[step] summarize: LLM unavailable, writing preview")
                    summary = "LLM not configured. Showing first 1000 characters of extracted text.\n\n" + text[:1000]

                summary_path = self.ctx.settings.outputs_dir / "web_summary.md"
                if self.ctx.settings.verbose:
                    self.ctx.logger.info(f"[step] write: {summary_path}")
                write_text(summary_path, summary)
                artifacts.append(summary_path)

                finished = datetime.now(timezone.utc)
                if self.ctx.settings.verbose:
                    self.ctx.logger.info("[step] report: generate + save")
                report = generate_markdown_report("Web Fetch & Summary", goal, steps, artifacts, started, finished)
                report_path = save_report(self.ctx.settings.reports_dir, "web_fetch_summary", report)
                msg = "Summary written to " + str(summary_path)
                return True, steps, artifacts + [report_path], msg
        except Exception as web_exc:
            self.ctx.logger.warning(f"Web path failed, falling back to codegen: {web_exc}")

        # Zero-heuristics fallback: generate and run a Python script for the goal
        try:
            if self.ctx.settings.verbose:
                self.ctx.logger.info("[step] codegen: request script via function calling")
            # Prefer function calling for structured output; fallback to JSON mode
            fc_system = (
                "You are a Python code generator. Use the provided function to return the script. "
                "Constraints: Python 3.11, stdlib only, no interactive input, print clear output to stdout."
            )
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "generate_python_script",
                        "description": "Generate a complete, runnable Python 3.11 script using stdlib only. Return code and notes.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "notes": {"type": "string"}
                            },
                            "required": ["code"]
                        }
                    }
                }
            ]
            user = f"Task: {goal}"
            _, tool_calls = self.ctx.llm.chat_with_tools(fc_system, user, tools=tools, tool_choice={"type": "function", "function": {"name": "generate_python_script"}}, max_tokens=1800)
            code = None
            if tool_calls:
                try:
                    args = json.loads(tool_calls[0]["function"]["arguments"] or "{}")
                    code = (args.get("code") or "").strip()
                except Exception:
                    code = None
            if not code:
                if self.ctx.settings.verbose:
                    self.ctx.logger.info("[step] codegen: fallback JSON mode")
                system = (
                    "You are a Python code generator. Return JSON with keys: code (string, full runnable Python 3.11 script), "
                    "notes (string). Use only stdlib; no interactive input. Print clear output to stdout."
                )
                user_json = (
                    f'Task: {goal}\n'
                    'Respond ONLY with a JSON object: {"code": <string>, "notes": <string>}'
                )
                obj = self.ctx.llm.complete_json(system, user_json, max_tokens=1800)
                code = (obj.get("code") or "").strip()
            if not code:
                raise ValueError("LLM returned empty code")

            base = sanitize_filename(goal.replace(" ", "_").lower()) or "generated_script"
            if not base.endswith(".py"):
                base += ".py"
            created_path = self.ctx.settings.workspace_dir / base
            if not self.ctx.settings.dry_run:
                if self.ctx.settings.verbose:
                    self.ctx.logger.info(f"[step] write: {created_path}")
                created_path.parent.mkdir(parents=True, exist_ok=True)
                created_path.write_text(code, encoding="utf-8")
            steps.append(StepRecord(name="file.create", command=f"write {created_path}", exit_code=0, stdout_path=None, stderr_path=None, success=True))
            artifacts.append(created_path)

            rel_path = created_path.relative_to(self.ctx.settings.workspace_dir)
            if self.ctx.settings.verbose:
                self.ctx.logger.info(f"[step] run: python3 {rel_path}")
            res = self.shell.run(f"python3 {rel_path}", dry_run=self.ctx.settings.dry_run)
            steps.append(
                StepRecord(
                    name="shell.run",
                    command=f"python3 {rel_path}",
                    exit_code=res.exit_code,
                    stdout_path=Path(res.extra["stdout_path"]) if res.extra and "stdout_path" in res.extra else None,
                    stderr_path=Path(res.extra["stderr_path"]) if res.extra and "stderr_path" in res.extra else None,
                    success=res.ok,
                    notes=res.stdout,
                )
            )
            # Auto-fix and retry on failure or suspicious output (e.g., HTTP errors)
            suspicious_text = f"{res.stdout or ''}\n{res.stderr or ''}".lower()
            needs_fix = (not res.ok) or any(k in suspicious_text for k in [
                "http error",
                "bad request",
                "traceback",
                "exception",
                "failed",
                "error",
                "timeout",
                "timed out",
                "connection error",
                "refused",
                "not found",
                "module not found",
                "nameerror",
                "typeerror",
                "valueerror",
            ])
            if needs_fix and not self.ctx.settings.dry_run:
                try:
                    original_code = created_path.read_text(encoding="utf-8")
                    if self.ctx.settings.verbose:
                        self.ctx.logger.info("[step] fix: request auto-fix + rerun")
                    fixer_system = (
                        "You are a Python code fixer. Return JSON with keys: code (string, full corrected Python 3.11 script), notes (string).\n"
                        "Constraints: stdlib only, add timeouts, set a User-Agent for HTTP, use json.load where appropriate, no interactive input."
                    )
                    fixer_user = (
                        f'Goal: {goal}\n'
                        f'Exit code: {res.exit_code}\n'
                        f'Stdout:\n{res.stdout}\n\n'
                        f'Stderr:\n{res.stderr}\n\n'
                        'Original code:\n' + original_code + '\n'
                        'Respond ONLY with a JSON object: {"code": <string>, "notes": <string>}'
                    )
                    obj = self.ctx.llm.complete_json(fixer_system, fixer_user, max_tokens=1800)
                    fixed_code = (obj.get("code") or "").strip()
                    if fixed_code and fixed_code != original_code:
                        created_path.write_text(fixed_code, encoding="utf-8")
                        steps.append(StepRecord(name="file.update", command=f"write {created_path}", exit_code=0, stdout_path=None, stderr_path=None, success=True))
                        res = self.shell.run(f"python3 {rel_path}", dry_run=self.ctx.settings.dry_run)
                        steps.append(
                            StepRecord(
                                name="shell.run",
                                command=f"python3 {rel_path}",
                                exit_code=res.exit_code,
                                stdout_path=Path(res.extra["stdout_path"]) if res.extra and "stdout_path" in res.extra else None,
                                stderr_path=Path(res.extra["stderr_path"]) if res.extra and "stderr_path" in res.extra else None,
                                success=res.ok,
                                notes=res.stdout,
                            )
                        )
                except Exception:
                    pass
            finished = datetime.now(timezone.utc)
            report = generate_markdown_report("LLM Script Task", goal, steps, artifacts, started, finished)
            report_path = save_report(self.ctx.settings.reports_dir, "llm_script_task", report)
            msg = res.stdout if res.ok and (res.stdout or "").strip() else (res.stderr or "Failed")
            return res.ok, steps, artifacts + [report_path], msg
        except Exception as e:
            self.ctx.logger.exception("Execution error")
            finished = datetime.now(timezone.utc)
            report = generate_markdown_report("Task Failed", goal, steps, artifacts, started, finished)
            report_path = save_report(self.ctx.settings.reports_dir, "task_failed", report)
            return False, steps, artifacts + [report_path], str(e)


