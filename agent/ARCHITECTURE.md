## Architecture and Code Map

This document explains the full project structure and the responsibilities of each source file so another developer or LLM can confidently extend and maintain the codebase.

### Purpose
Natural-language driven automation agent that:
- Plans → Executes → Verifies → Reports on tasks described in plain language
- Uses OpenAI Python SDK (chat completions) for planning and summarization
- Executes sandboxed shell, Python scripts, web fetching, and SQL queries
- Produces artifacts under `outputs/`, Markdown reports under `reports/`, logs under `logs/`
- Enforces safety: asks up to 3 short questions for missing critical parameters; confirms risky operations unless `--auto-yes` (or `--dry-run`)

### High-Level Flow
1. CLI receives a natural language goal and flags.
2. Configuration and logger are initialized.
3. Orchestrator
   - Optionally asks up to 3 questions if critical details are missing.
   - Creates a short plan via LLM.
   - If plan contains potentially risky actions, requires `--auto-yes` (or `--dry-run`).
   - Routes to tools (`web`, `db`, `python_exec`, `shell`, `packages`) by simple heuristics.
   - Collects `StepRecord`s and artifacts.
   - Generates a Markdown report and writes it to `reports/`.

### CLI and Entry Points
- `src/agent/cli.py`
  - Typer app with two commands:
    - `do` — non-interactive, runs a single goal
      - Flags: `--auto-yes`, `--dry-run`, `--model`
    - `repl` — interactive mode to iterate on goals and answers
  - Entry point `agent` is defined in `pyproject.toml` and `setup.cfg` as `src.agent.cli:main`.

### Configuration
- `src/agent/config.py`
  - `load_settings(...)`: Reads `.env` via `python-dotenv`, ensures directories exist:
    - `workspace/`, `outputs/`, `reports/`, `logs/`, `workspace/tmp/`
  - Required env vars: `OPENAI_API_KEY`. Optional: `OPENAI_BASE_URL`, `OPENAI_MODEL` (default `gpt-4o-mini`).
  - Returns `AgentSettings` dataclass with computed paths and flags (`auto_yes`, `dry_run`).

### Logging
- `src/agent/logger.py`
  - `setup_logger(logs_dir)`: Configures `rich` console handler and rotating file handler (`logs/agent.log`).
  - All tools and orchestrator depend on this logging setup indirectly via CLI.

### LLM Client
- `src/agent/llm.py`
  - `LLMClient`: Wraps official `openai` SDK. Stores model name.
  - `complete(system, user, ...)`: Sends chat completion.
  - `summarize_chunks(system, chunks, ...)`: Summarizes large content in chunks and merges the results.

### Orchestration
- `src/agent/orchestrator.py`
  - `ExecutionContext`: Holds `AgentSettings`, `LLMClient`, and `logger`.
  - `Orchestrator` methods:
    - `ask_missing_parameters(goal)`: LLM prompt that yields up to 3 critical questions or `none`.
    - `confirm_if_needed(plan, auto_yes, dry_run)`: Requires confirmation for risky actions unless `--auto-yes` or `--dry-run`.
    - `plan(goal)`: LLM prompt to generate a concise plan using available tools.
    - `execute(goal)`: Implements the Plan → Execute → Verify → Report cycle with simple routing heuristics:
      - If goal contains a URL: `web.fetch` → extract text → `LLMClient.summarize_chunks` → write `outputs/web_summary.md`.
      - If goal mentions DBs: parse connection URL and a `SELECT ...` query → `db.query_to_files` → write CSV + Parquet.
      - If goal mentions `pandas`/`DataFrame`: run sample Python script via `python_exec` to generate a CSV.
      - If goal mentions `git`/`jq`/`apt`: plan and ensure packages via `packages.ensure` (apt-oriented), then print versions.
      - Fallback: produce plan-only report and request more specifics.
    - Always writes a Markdown report to `reports/` via `reporter`.
  - Safety:
    - Asks up to 3 missing-parameter questions before planning.
    - Refuses risky operations without `--auto-yes` unless `--dry-run`.
  - Error handling:
    - Catches exceptions, logs stack traces, and writes a failure report.

### Reporting
- `src/agent/reporter.py`
  - `StepRecord`: A single operation with `name`, `command`, `exit_code`, `stdout_path`, `stderr_path`, `success`, `notes`.
  - `generate_markdown_report(title, goal, steps, outputs, started_at, finished_at)`: Renders the report.
  - `save_report(reports_dir, title, content)`: Saves under `reports/<timestamp>_<title>.md`.

### Utilities
- `src/agent/utils.py`
  - `sanitize_filename(name)`: Safe artifact names.
  - `write_text(path, content)`: Ensure parent dirs and write text.
  - `chunk_text(text, chunk_size, overlap)`: Text chunking for large content.
  - `ensure_within_workspace(path, workspace_dir)`: Guard to prevent writes outside workspace.

### Tools Layer (Internal)
Common base types:
- `src/agent/tools/base.py`
  - `ToolResult`: Standard result container with `ok`, `stdout`, `stderr`, `exit_code`, optional `artifact_path`, `extra`.
  - `BaseTool`: Stores `workspace_dir`, `outputs_dir`, `logs_dir`.

Individual tools:
- `src/agent/tools/shell.py`
  - `ShellTool.run(command, cwd=None, env=None, dry_run=False)`: Executes shell commands within workspace.
    - Writes `stdout`/`stderr` logs under `logs/` with timestamped filenames.
    - If `dry_run=True`, returns planned command only.
    - Refuses to run outside the workspace directory.

- `src/agent/tools/python_exec.py`
  - `PythonExecTool.run_script(code, tmp_dir, dry_run=False)`: Writes a temporary Python file under `workspace/tmp/` and executes it.
    - Captures `stdout`/`stderr` to `logs/` and returns paths in `extra`.
    - If `dry_run=True`, returns the planned script path only.

- `src/agent/tools/packages.py`
  - `PackagePlan` with fields `apt` and `pip`.
  - `PackagesTool.plan(apt=[...], pip=[...])`: Declares a package plan.
  - `PackagesTool.ensure(plan, auto_yes=False, dry_run=False)`: Installs with `apt-get` and/or `pip`.
    - On Ubuntu, runs `sudo apt-get update -y` then `sudo apt-get install` with `-y` if `auto_yes`.
    - `pip install` for Python deps.
    - If `dry_run=True`, returns planned commands only.
    - Note: apt integration is Linux/Ubuntu-oriented; on Windows prefer `winget` or `choco` externally.

- `src/agent/tools/db.py`
  - `QueryRequest(url, sql, out_base_name="query_result")`
  - `_ensure_driver(url, dry_run)`: Best-effort `pip install` of the required DB driver (`psycopg2-binary`, `pymysql`, `pyodbc`) based on URL dialect+driver.
  - `_engine(url, dry_run)`: Builds a SQLAlchemy engine after driver check.
  - `DBTool.query_to_files(req, dry_run=False)`: Executes `SELECT` queries via SQLAlchemy + pandas, writes CSV and Parquet artifacts to `outputs/`.

- `src/agent/tools/web.py`
  - `WebTool.fetch(url, user_agent=None, timeout=30, dry_run=False)`: Downloads HTML to `outputs/download_<ts>.html`.
    - If `dry_run=True`, returns planned URL only.
  - `WebTool.extract_text(html)`: Strips scripts/styles and returns cleaned textual content.

### Project Structure (Files and Directories)
- Root
  - `pyproject.toml`: Package metadata; console script `agent`; setuptools config.
  - `setup.cfg`: Additional console script entrypoint mapping.
  - `requirements.txt`: Runtime dependencies.
  - `Makefile`: Unix-like convenience targets (`venv`, `install`, `run`, `clean`).
  - `make.bat`: Windows convenience script for the same.
  - `README.md`: Install, usage, examples.
  - `ARCHITECTURE.md`: This document.
  - `LICENSE`: MIT.
  - `env.example`: Template for `.env`.
- Runtime Directories (created automatically as needed)
  - `workspace/tmp/`: Temp scripts and working files.
  - `outputs/`: Data artifacts (CSV, Parquet, HTML, summaries).
  - `reports/`: Markdown reports per task.
  - `logs/`: Rotating logs and captured stdout/stderr.
- Source
  - `src/agent/` and subpackages as described above.

### Safety and Scope
- All filesystem operations are constrained to the repository workspace path (the CWD at runtime). Tools guard against running outside.
- Risky operations (e.g., `apt install`, `pip install`, `rm -rf`, service changes) need explicit `--auto-yes` or `--dry-run`.
- Question policy: If critical parameters are missing, the agent asks up to 3 short questions, then stops. In `repl` mode, the user can answer interactively.

### Extension Points
- Add a new tool:
  1. Create `src/agent/tools/<new_tool>.py` deriving from `BaseTool` and returning `ToolResult`.
  2. Wire routing logic in `Orchestrator.execute(...)` to detect the relevant intent and call the new tool.
  3. Update `README.md` with example usage and any dependency requirements.

- Enhance planning:
  - Adjust `USER_INSTRUCTION_SYSTEM` and prompts in `Orchestrator`.
  - Optionally parse generated plan text into structured steps for deterministic execution.

- Swap/extend LLM provider:
  - `LLMClient` uses `openai` with optional `OPENAI_BASE_URL` for self-hosted compat.
  - To support another SDK, introduce an interface (e.g., `BaseLLM`) and implement a new client, then inject it in CLI.

- Improve OS portability:
  - `packages.py` is apt-oriented. Introduce OS detection and alternate strategies for Windows (`winget`, `choco`) or macOS (`brew`).

### Known Limitations
- Planning is heuristic; complex goals may need more robust parsing/execution planning.
- Package installer is Linux-focused; Windows/macOS require enhancements to be first-class.
- REPL does not persist long conversation context beyond each iteration.
- Limited unit/integration tests currently.

### Conventions
- Python 3.11+, type-annotated public functions, guard clauses, minimal but meaningful comments.
- Directories for artifacts (`outputs/`), reports (`reports/`), logs (`logs/`), temp (`workspace/tmp/`).
- Use `--dry-run` to preview, `--auto-yes` to bypass confirmations.

### Troubleshooting
- Missing `OPENAI_API_KEY`: set it in `.env` or environment.
- `Import ... could not be resolved` warnings: ensure `pip install -r requirements.txt` in the active virtual environment.
- DB driver errors: ensure URL includes a specific driver (e.g., `postgresql+psycopg2://...`) so the tool can install the right package.
- Windows script activation: If blocked, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in PowerShell.


