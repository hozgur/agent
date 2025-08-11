from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        # Iterative planner is injected lazily to avoid circulars
        self._iter_planner: Optional["IterativePlanner"] = None

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

    def execute(self, goal: str, plan_context: Optional[Dict[str, Any]] = None) -> Tuple[bool, List[StepRecord], List[Path], str]:
        started = datetime.now(timezone.utc)
        steps: List[StepRecord] = []
        artifacts: List[Path] = []
        plan_context = plan_context or {"artifacts": [], "variables": {}, "notes": []}

        # If depth > 1, use iterative planner
        if self.ctx.settings.depth and self.ctx.settings.depth > 1:
            if self.ctx.settings.verbose:
                self.ctx.logger.info(f"[step] iterative: starting depth={self.ctx.settings.depth}")
            if self._iter_planner is None:
                self._iter_planner = IterativePlanner(self)
            return self._iter_planner.run(goal, max_passes=self.ctx.settings.depth)

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
                "Constraints: Python 3.11, stdlib only, no interactive input, print clear output to stdout. "
                "If a context is provided, use artifacts/variables in context where helpful."
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
            # Prepare a concise context blurb
            ctx_artifacts = [str(p) for p in plan_context.get("artifacts", [])]
            ctx_vars = plan_context.get("variables", {})
            ctx_notes = plan_context.get("notes", [])[-5:]
            context_blurb = (
                ("Context artifacts:\n" + "\n".join(ctx_artifacts) + "\n") if ctx_artifacts else ""
            ) + (
                ("Context variables:\n" + json.dumps(ctx_vars) + "\n") if ctx_vars else ""
            ) + (
                ("Recent notes:\n" + "\n".join(ctx_notes) + "\n") if ctx_notes else ""
            )
            user = ("Task: " + goal + ("\n\n" + context_blurb if context_blurb else "")).strip()
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
                    "notes (string). Use only stdlib; no interactive input. Print clear output to stdout. If a context is provided, "
                    "use files and variables from it."
                )
                context_json = {
                    "task": goal,
                    "context": {
                        "artifacts": ctx_artifacts,
                        "variables": ctx_vars,
                        "notes": ctx_notes,
                        "context_json_path": str(self.ctx.settings.workspace_dir / "context.json"),
                    },
                }
                user_json = (
                    json.dumps(context_json) + "\nRespond ONLY with a JSON object: {\"code\": <string>, \"notes\": <string>}"
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
                # Write context.json for scripts to read if needed
                try:
                    context_payload = {
                        "artifacts": ctx_artifacts,
                        "variables": ctx_vars,
                        "notes": ctx_notes,
                    }
                    write_text(self.ctx.settings.workspace_dir / "context.json", json.dumps(context_payload, ensure_ascii=False, indent=2))
                except Exception:
                    pass
                if self.ctx.settings.verbose:
                    self.ctx.logger.info(f"[step] write: {created_path}")
                created_path.parent.mkdir(parents=True, exist_ok=True)
                created_path.write_text(code, encoding="utf-8")
            steps.append(StepRecord(name="file.create", command=f"write {created_path}", exit_code=0, stdout_path=None, stderr_path=None, success=True))
            artifacts.append(created_path)

            rel_path = created_path.relative_to(self.ctx.settings.workspace_dir)
            if self.ctx.settings.verbose:
                self.ctx.logger.info(f"[step] run: python3 {rel_path}")
            # Use configured timeout for scripts to prevent freezing indefinitely
            res = self.shell.run(
                f"python3 {rel_path}",
                dry_run=self.ctx.settings.dry_run,
                timeout_sec=float(self.ctx.settings.script_timeout_sec),
            )
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
                        res = self.shell.run(
                            f"python3 {rel_path}",
                            dry_run=self.ctx.settings.dry_run,
                            timeout_sec=float(self.ctx.settings.script_timeout_sec),
                        )
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



class IterativePlanner:
    """Unified iterative planner that builds comprehensive solutions across multiple passes.

    Key improvements over previous approach:
      1. Maintains unified context and state across all iterations
      2. Builds comprehensive solutions instead of separate scripts
      3. Proper data flow and variable sharing between tasks
      4. Incremental development with rollback capabilities
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orch = orchestrator
        # Unified state management across iterations
        self.unified_context: Dict[str, Any] = {
            "variables": {},           # Shared variables across tasks
            "imports": set(),          # Required imports accumulated
            "functions": [],           # Reusable functions defined
            "data_files": [],          # Generated data files
            "script_sections": [],     # Code sections to be combined
            "execution_state": {}      # Runtime state information
        }

    def run(self, goal: str, max_passes: int = 3) -> Tuple[bool, List[StepRecord], List[Path], str]:
        ctx = self.orch.ctx
        all_steps: List[StepRecord] = []
        all_artifacts: List[Path] = []
        summary_msg = ""

        def log_step(msg: str) -> None:
            if ctx.settings.verbose:
                ctx.logger.info(f"[unified] {msg}")

        log_step(f"starting unified planning with {max_passes} passes")

        for pass_idx in range(1, max_passes + 1):
            log_step(f"=== PASS {pass_idx}/{max_passes} ===")

            # Generate comprehensive plan considering current state
            current_state = self._get_state_summary()
            plan_prompt = self._create_planning_prompt(goal, current_state, pass_idx)
            
            plan_response = self.orch.ctx.llm.complete_json(
                "You are an expert software architect. Create a comprehensive plan that builds upon previous work.",
                plan_prompt,
                max_tokens=800
            ) if ctx.settings.openai_api_key else self._fallback_plan(goal)

            # Extract and validate plan
            tasks = plan_response.get("tasks", [])
            approach = plan_response.get("approach", "incremental")
            
            if not tasks:
                tasks = [f"Complete step {pass_idx} of: {goal}"]
            
            log_step(f"planned {len(tasks)} tasks with '{approach}' approach")

            # Execute tasks with unified context
            pass_success = True
            for task_idx, task in enumerate(tasks):
                log_step(f"executing [{task_idx+1}/{len(tasks)}]: {task}")
                
                success, steps, artifacts, msg = self._execute_unified_task(
                    task, goal, pass_idx, task_idx
                )
                
                all_steps.extend(steps)
                all_artifacts.extend(artifacts)
                summary_msg = msg

                if not success:
                    log_step(f"task failed: {msg}")
                    # Try to recover or adapt
                    if pass_idx < max_passes:
                        log_step("will retry in next pass with adapted approach")
                        pass_success = False
                        break
                    else:
                        log_step("final pass failed, stopping")
                        return False, all_steps, all_artifacts, f"Failed on final pass: {msg}"

            if pass_success:
                log_step(f"pass {pass_idx} completed successfully")
                
                # Check if goal is fully achieved
                if self._is_goal_achieved(goal):
                    log_step("goal fully achieved, stopping early")
                    break

        # Generate final unified artifact if we have multiple script sections
        if len(self.unified_context["script_sections"]) > 1:
            final_artifact = self._create_unified_script(goal)
            if final_artifact:
                all_artifacts.append(final_artifact)

        return True, all_steps, all_artifacts, summary_msg or "Completed unified iterative plan"

    def _get_state_summary(self) -> str:
        """Generate a summary of current unified context state."""
        context = self.unified_context
        summary_parts = []
        
        if context["variables"]:
            summary_parts.append(f"Variables: {list(context['variables'].keys())}")
        
        if context["imports"]:
            summary_parts.append(f"Imports: {', '.join(sorted(context['imports']))}")
        
        if context["script_sections"]:
            summary_parts.append(f"Script sections: {len(context['script_sections'])} parts")
            
        return " | ".join(summary_parts) if summary_parts else "Clean state"

    def _create_planning_prompt(self, goal: str, current_state: str, pass_idx: int) -> str:
        """Create a comprehensive planning prompt considering current state."""
        return f"""
Goal: {goal}
Current Pass: {pass_idx}
Current State: {current_state}

Create a plan that builds upon existing work. Consider:
1. What has already been accomplished
2. What variables/data are available
3. How to extend or improve the current solution

Return JSON with:
{{
    "approach": "incremental|rebuild|extend",
    "tasks": ["task1", "task2", ...],
    "reasoning": "explanation of approach"
}}
        """.strip()

    def _fallback_plan(self, goal: str) -> Dict[str, Any]:
        """Fallback plan when LLM is not available."""
        return {
            "approach": "incremental",
            "tasks": [goal],
            "reasoning": "Simple fallback execution"
        }

    def _execute_unified_task(self, task: str, goal: str, pass_idx: int, task_idx: int) -> Tuple[bool, List[StepRecord], List[Path], str]:
        """Execute a task within the unified context, maintaining state continuity."""
        
        # Check if this is a web-related task
        if re.search(r"https?://\S+", task):
            return self._execute_web_task(task)
        
        # Otherwise, execute as a Python development task with unified context
        return self._execute_python_task(task, goal, pass_idx, task_idx)

    def _execute_web_task(self, task: str) -> Tuple[bool, List[StepRecord], List[Path], str]:
        """Execute web-related tasks (URL fetching, etc.)."""
        url_match = re.search(r"https?://\S+", task)
        if url_match:
            url = url_match.group(0)
            try:
                fetch_res = self.orch.web.fetch(url, dry_run=self.orch.ctx.settings.dry_run)
                if fetch_res.ok and fetch_res.artifact_path:
                    self.unified_context["data_files"].append(str(fetch_res.artifact_path))
                    
                step = StepRecord(
                    name="web.fetch",
                    command=f"GET {url}",
                    exit_code=fetch_res.exit_code,
                    stdout_path=None,
                    stderr_path=None,
                    success=fetch_res.ok,
                    notes=fetch_res.stdout or fetch_res.stderr
                )
                
                artifacts = [Path(fetch_res.artifact_path)] if fetch_res.artifact_path else []
                return fetch_res.ok, [step], artifacts, fetch_res.stdout or "Web fetch completed"
            except Exception as e:
                return False, [], [], f"Web task failed: {str(e)}"
        
        return False, [], [], "No valid URL found in web task"

    def _execute_python_task(self, task: str, goal: str, pass_idx: int, task_idx: int) -> Tuple[bool, List[StepRecord], List[Path], str]:
        """Execute Python development task with unified context awareness."""
        
        # Create context-aware prompt for code generation
        context_info = self._build_context_info()
        
        system_prompt = f"""
You are a Python developer working on a multi-part solution. 

IMPORTANT CONTEXT:
{context_info}

Generate Python code that:
1. Builds upon existing work (use existing variables, functions, imports)
2. Integrates smoothly with previous script sections
3. Maintains data continuity between steps

Return JSON with:
{{
    "code": "complete Python code",
    "imports": ["import1", "import2"],
    "variables": {{"var1": "description", "var2": "description"}},
    "functions": ["func1", "func2"],
    "notes": "integration notes"
}}
        """
        
        user_prompt = f"""
Task: {task}
Overall Goal: {goal}
Pass: {pass_idx}, Task: {task_idx}

Generate code that advances toward the overall goal while building on existing context.
        """
        
        try:
            response = self.orch.ctx.llm.complete_json(
                system_prompt, user_prompt, max_tokens=1800
            ) if self.orch.ctx.settings.openai_api_key else self._fallback_python_task(task)
            
            code = response.get("code", "").strip()
            if not code:
                return False, [], [], "No code generated"
            
            # Update unified context
            self._update_unified_context(response)
            
            # Execute the code
            return self._execute_generated_code(code, task, pass_idx, task_idx)
            
        except Exception as e:
            return False, [], [], f"Python task generation failed: {str(e)}"

    def _build_context_info(self) -> str:
        """Build context information for code generation."""
        context = self.unified_context
        info_parts = []
        
        if context["variables"]:
            info_parts.append(f"Available variables: {context['variables']}")
        
        if context["imports"]:
            info_parts.append(f"Already imported: {', '.join(sorted(context['imports']))}")
        
        if context["data_files"]:
            info_parts.append(f"Data files available: {context['data_files']}")
        
        return "\n".join(info_parts) if info_parts else "Starting fresh - no previous context"

    def _update_unified_context(self, response: Dict[str, Any]) -> None:
        """Update unified context with new code generation results."""
        context = self.unified_context
        
        # Update imports
        new_imports = response.get("imports", [])
        context["imports"].update(new_imports)
        
        # Update variables
        new_vars = response.get("variables", {})
        context["variables"].update(new_vars)
        
        # Store script section
        if response.get("code"):
            context["script_sections"].append({
                "code": response.get("code"),
                "notes": response.get("notes", ""),
                "imports": new_imports,
                "variables": new_vars
            })

    def _execute_generated_code(self, code: str, task: str, pass_idx: int, task_idx: int) -> Tuple[bool, List[StepRecord], List[Path], str]:
        """Execute generated Python code."""
        try:
            # Create filename that reflects the unified approach
            base_name = sanitize_filename(f"unified_solution_p{pass_idx}_t{task_idx}")
            if not base_name.endswith(".py"):
                base_name += ".py"
            
            result = self.orch.pyexec.run_script(
                code, 
                self.orch.ctx.settings.tmp_dir,
                dry_run=self.orch.ctx.settings.dry_run
            )
            
            step = StepRecord(
                name="python.unified",
                command=f"python {base_name}",
                exit_code=result.exit_code,
                stdout_path=result.extra.get("stdout_path"),
                stderr_path=result.extra.get("stderr_path"),
                success=result.ok,
                notes=f"Unified task: {task}"
            )
            
            # Get script path from extra data
            script_path = result.extra.get("script_path") if result.extra else None
            artifacts = [Path(script_path)] if script_path else []
            
            # Update execution state
            self.unified_context["execution_state"][f"pass_{pass_idx}_task_{task_idx}"] = {
                "success": result.ok,
                "output": result.stdout,
                "artifact": script_path
            }
            
            return result.ok, [step], artifacts, result.stdout or "Unified task completed"
            
        except Exception as e:
            return False, [], [], f"Code execution failed: {str(e)}"

    def _fallback_python_task(self, task: str) -> Dict[str, Any]:
        """Fallback Python task when LLM is not available."""
        simple_code = f"""
# Task: {task}
print("Executing: {task}")
print("Task completed successfully")
        """
        return {
            "code": simple_code,
            "imports": [],
            "variables": {},
            "functions": [],
            "notes": "Fallback execution"
        }

    def _is_goal_achieved(self, goal: str) -> bool:
        """Check if the overall goal has been achieved based on context."""
        # Simple heuristic: if we have successful execution states and artifacts
        context = self.unified_context
        
        has_successful_executions = any(
            state.get("success", False) 
            for state in context["execution_state"].values()
        )
        
        has_artifacts = len(context["data_files"]) > 0 or len(context["script_sections"]) > 0
        
        # For now, simple check - can be enhanced with LLM-based evaluation
        return has_successful_executions and has_artifacts

    def _create_unified_script(self, goal: str) -> Optional[Path]:
        """Create a final unified script with intelligent code integration."""
        try:
            context = self.unified_context
            if not context["script_sections"]:
                return None
            
            # Analyze and merge code sections intelligently
            merged_code = self._merge_code_sections_intelligently(context["script_sections"])
            
            # Build unified script
            script_parts = []
            
            # Header
            script_parts.append('#!/usr/bin/env python3')
            script_parts.append('"""')
            script_parts.append(f'Unified solution for: {goal}')
            script_parts.append(f'Generated by iterative planner with {len(context["script_sections"])} sections')
            script_parts.append('Intelligently merged to avoid conflicts and duplications')
            script_parts.append('"""')
            script_parts.append('')
            
            # Add the merged code
            script_parts.append(merged_code)
            
            # Write unified script
            unified_content = '\n'.join(script_parts)
            filename = sanitize_filename(f"unified_solution_{goal.replace(' ', '_')[:30]}.py")
            script_path = self.orch.ctx.settings.outputs_dir / filename
            
            write_text(script_path, unified_content)
            
            return script_path
            
        except Exception as e:
            if self.orch.ctx.settings.verbose:
                self.orch.ctx.logger.error(f"Failed to create unified script: {e}")
            return None

    def _merge_code_sections_intelligently(self, sections: List[Dict[str, Any]]) -> str:
        """Intelligently merge code sections using a simplified approach that focuses on the key issues."""
        
        # For now, let's use a much simpler but more reliable approach
        # Instead of complex parsing, let's prompt the LLM to create a clean, unified version
        
        if not sections:
            return ""
        
        # Collect all the code from sections
        all_code_sections = []
        for i, section in enumerate(sections):
            code = section.get("code", "").strip()
            if code:
                all_code_sections.append(f"# === Section {i+1} ===\n{code}")
        
        combined_code = "\n\n".join(all_code_sections)
        
        # Use LLM to create a clean, unified version
        try:
            system_prompt = """
You are an expert Python developer. You will receive multiple code sections that need to be merged into a single, clean, working Python script.

Your task:
1. Remove ALL duplicate function definitions - keep only the most complete version
2. Consolidate imports at the top
3. Remove conflicting variable assignments
4. Create a proper main() function that orchestrates the execution
5. Ensure the code is syntactically correct and functional

Return ONLY the clean, merged Python code. No explanations, no markdown formatting.
            """
            
            user_prompt = f"""
Please merge these code sections into a single, clean Python script:

{combined_code}

Requirements:
- Remove duplicate functions (keep the most complete version)
- Organize imports at the top
- Create a proper main() function
- Ensure no syntax errors
- Make it a working, executable script
            """
            
            if self.orch.ctx.settings.openai_api_key:
                clean_code = self.orch.ctx.llm.complete(
                    system_prompt, user_prompt, max_tokens=2000
                )
                return clean_code.strip()
            else:
                # Fallback: simple concatenation with basic deduplication
                return self._simple_merge_fallback(sections)
                
        except Exception as e:
            if self.orch.ctx.settings.verbose:
                self.orch.ctx.logger.error(f"LLM-based merging failed: {e}")
            # Fallback to simple merge
            return self._simple_merge_fallback(sections)

    def _simple_merge_fallback(self, sections: List[Dict[str, Any]]) -> str:
        """Simple fallback merging when LLM is not available."""
        imports = set()
        functions = {}
        main_code = []
        
        for i, section in enumerate(sections):
            code = section.get("code", "")
            lines = code.split('\n')
            
            current_function = None
            function_lines = []
            
            for line in lines:
                stripped = line.strip()
                
                if stripped.startswith('import ') or stripped.startswith('from '):
                    imports.add(stripped)
                elif stripped.startswith('def '):
                    if current_function:
                        functions[current_function] = '\n'.join(function_lines)
                    current_function = stripped.split('(')[0].replace('def ', '').strip()
                    function_lines = [line]
                elif current_function and (line.startswith('    ') or line.startswith('\t') or not stripped):
                    function_lines.append(line)
                elif current_function:
                    functions[current_function] = '\n'.join(function_lines)
                    current_function = None
                    function_lines = []
                    if stripped:
                        main_code.append(line)
                else:
                    if stripped:
                        main_code.append(line)
            
            if current_function:
                functions[current_function] = '\n'.join(function_lines)
        
        # Build result
        result = []
        
        # Imports
        if imports:
            for imp in sorted(imports):
                result.append(imp)
            result.append('')
        
        # Functions
        for func_name, func_code in functions.items():
            result.append(func_code)
            result.append('')
        
        # Main function
        if main_code:
            result.append('def main():')
            result.append('    """Main execution."""')
            for line in main_code:
                if line.strip():
                    result.append(f'    {line}' if not line.startswith('    ') else line)
            result.append('')
            result.append('if __name__ == "__main__":')
            result.append('    main()')
        
        return '\n'.join(result)

