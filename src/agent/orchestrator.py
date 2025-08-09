from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
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
from .utils import chunk_text, sanitize_filename


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

        # Direct handling: create a python script named X to Y [and run it]
        try:
            m = re.search(r"create\s+a\s+python\s+script\s+named\s+([A-Za-z0-9_./-]+\.py)\s+to\s+(.+)", goal, re.IGNORECASE)
            if m:
                filename = m.group(1)
                # Normalize to be relative to workspace root if user prefixed with 'workspace/'
                try:
                    from pathlib import Path as _P
                    _fp = _P(filename)
                    if _fp.parts and _fp.parts[0] == "workspace":
                        filename = str(_P(*_fp.parts[1:])) or filename
                except Exception:
                    pass
                task_text = m.group(2).strip()
                task_text = re.sub(r"\s+and\s+run\s+it\.?$", "", task_text, flags=re.IGNORECASE).strip()

                code: Optional[str] = None

                # First, ask the LLM to generate the full script using function calling (preferred), fallback to JSON
                try:
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
                    user = f"Task: {task_text}"
                    _, tool_calls = self.ctx.llm.chat_with_tools(fc_system, user, tools=tools, tool_choice={"type": "function", "function": {"name": "generate_python_script"}}, max_tokens=1800)
                    if tool_calls:
                        try:
                            args = json.loads(tool_calls[0]["function"]["arguments"] or "{}")
                            candidate = (args.get("code") or "").strip()
                            if candidate:
                                code = candidate
                        except Exception:
                            pass
                    if code is None:
                        system = (
                            "You are a Python code generator. Return JSON with keys: code (string, full runnable Python 3.11 script), "
                            "notes (string). Use only stdlib; no interactive input. Print clear output to stdout."
                        )
                        user_json = (
                            f'Task: {task_text}\n'
                            'Respond ONLY with a JSON object: {"code": <string>, "notes": <string>}'
                        )
                        obj = self.ctx.llm.complete_json(system, user_json, max_tokens=1800)
                        candidate = (obj.get("code") or "").strip()
                        if candidate:
                            code = candidate
                except Exception:
                    pass

                # If no code yet, retry with a stricter instruction to force code output
                if code is None:
                    try:
                        system_retry = (
                            "You are a Python code generator. Output ONLY runnable Python 3.11 code, no backticks, no explanations. "
                            "Prefer stdlib; do not use third-party packages. Print results to stdout."
                        )
                        user_retry = f"Task: {task_text}\nReturn only code."
                        resp2 = self.ctx.llm.complete(system_retry, user_retry, max_tokens=1500)
                        fence_match2 = re.search(r"```(?:python)?\n([\s\S]*?)```", resp2, flags=re.IGNORECASE)
                        code = fence_match2.group(1).strip() if fence_match2 else resp2.strip()
                        # Reject obviously non-code
                        if not ("import" in code or "def " in code or "print(" in code):
                            code = None
                    except Exception:
                        code = None

                if code is not None:
                    created_path = self.ctx.settings.workspace_dir / filename
                    if not self.ctx.settings.dry_run:
                        created_path.parent.mkdir(parents=True, exist_ok=True)
                        created_path.write_text(code, encoding="utf-8")
                    steps.append(StepRecord(name="file.create", command=f"write {created_path}", exit_code=0, stdout_path=None, stderr_path=None, success=True))
                    artifacts.append(created_path)

                    rel_path = created_path.relative_to(self.ctx.settings.workspace_dir)
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
                    suspicious = (res.stdout or "").lower()
                    needs_fix = (not res.ok) or any(k in suspicious for k in ["http error", "bad request", "traceback", "exception", "failed", "error:"])
                    if needs_fix and not self.ctx.settings.dry_run:
                        try:
                            original_code = created_path.read_text(encoding="utf-8")
                            fixer_system = (
                                "You are a Python code fixer. Return JSON with keys: code (string, full corrected Python 3.11 script), notes (string).\n"
                                "Constraints: stdlib only, add timeouts, set a User-Agent for HTTP, use json.load where appropriate, no interactive input."
                            )
                            fixer_user = (
                                f'Task intent: {task_text}\n'
                                f'Error stderr:\n{res.stderr}\n\n'
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
                    finished = datetime.utcnow()
                    report = generate_markdown_report("Script Task", goal, steps, artifacts, started, finished)
                    report_path = save_report(self.ctx.settings.reports_dir, "script_task", report)
                    msg = res.stdout if res.ok and (res.stdout or "").strip() else (res.stderr or "Failed")
                    return res.ok, steps, artifacts + [report_path], msg
        except Exception as e:
            self.ctx.logger.exception("Direct script task error")
            finished = datetime.utcnow()
            report = generate_markdown_report("Task Failed", goal, steps, artifacts, started, finished)
            report_path = save_report(self.ctx.settings.reports_dir, "task_failed", report)
            return False, steps, artifacts + [report_path], str(e)

        # Zero-heuristics: Always have the LLM generate and run a Python script for the goal
        try:
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
                created_path.parent.mkdir(parents=True, exist_ok=True)
                created_path.write_text(code, encoding="utf-8")
            steps.append(StepRecord(name="file.create", command=f"write {created_path}", exit_code=0, stdout_path=None, stderr_path=None, success=True))
            artifacts.append(created_path)

            rel_path = created_path.relative_to(self.ctx.settings.workspace_dir)
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
            suspicious = (res.stdout or "").lower()
            needs_fix = (not res.ok) or any(k in suspicious for k in ["http error", "bad request", "traceback", "exception", "failed", "error:"])
            if needs_fix and not self.ctx.settings.dry_run:
                try:
                    original_code = created_path.read_text(encoding="utf-8")
                    fixer_system = (
                        "You are a Python code fixer. Return JSON with keys: code (string, full corrected Python 3.11 script), notes (string).\n"
                        "Constraints: stdlib only, add timeouts, set a User-Agent for HTTP, use json.load where appropriate, no interactive input."
                    )
                    fixer_user = (
                        f'Goal: {goal}\n'
                        f'Error stderr:\n{res.stderr}\n\n'
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
            finished = datetime.utcnow()
            report = generate_markdown_report("LLM Script Task", goal, steps, artifacts, started, finished)
            report_path = save_report(self.ctx.settings.reports_dir, "llm_script_task", report)
            msg = res.stdout if res.ok and (res.stdout or "").strip() else (res.stderr or "Failed")
            return res.ok, steps, artifacts + [report_path], msg
        except Exception as e:
            self.ctx.logger.exception("Execution error")
            finished = datetime.utcnow()
            report = generate_markdown_report("Task Failed", goal, steps, artifacts, started, finished)
            report_path = save_report(self.ctx.settings.reports_dir, "task_failed", report)
            return False, steps, artifacts + [report_path], str(e)


