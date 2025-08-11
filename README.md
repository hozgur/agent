## Natural Language Automation Agent

An automation agent that executes tasks described in natural language. It plans, asks for missing critical details, confirms risky actions (unless --auto-yes), executes with tools (shell, Python, web, DB), validates, and produces Markdown reports.

### Features
- **Natural-language CLI**: `agent do "..."` and `agent repl` with comprehensive options
- **Advanced Planning**: Multi-depth iterative planning for complex tasks with `--depth`
- **Safety**: Asks up to 3 focused questions for missing critical parameters, confirmation for risky ops unless `--auto-yes`
- **Comprehensive Tools**: Safe shell, Python script runner, package installation, web fetch + summarize, DB query via SQLAlchemy
- **Enhanced Logging**: Rich console + rotating file logs, detailed LLM interaction logs, stdout/stderr/exit codes recorded
- **Flexible Execution**: Verbose mode, configurable timeouts, assume defaults mode
- **Reports**: Markdown in `reports/`, artifacts in `outputs/`

### Requirements
- Ubuntu 22.04+
- Python 3.11+

### Quick Start (Ubuntu)
```bash
sudo apt-get update -y
sudo apt-get install -y python3.11 python3.11-venv git

git clone <this-repo>
cd <this-repo>

python3.11 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

cp .env.example .env
export OPENAI_API_KEY=...  # or edit .env
```

### Environment
Set in `.env`:
- `OPENAI_API_KEY` (required)
- `OPENAI_BASE_URL` (optional)
- `OPENAI_MODEL` default `gpt-4o-mini`

### CLI

#### Main Command
```bash
agent do "<goal>" [OPTIONS]
```

**Options:**
- `--auto-yes / --no-auto-yes` - Auto-confirm risky actions (default: enabled)
- `--dry-run` - Show plan and commands without executing
- `--model TEXT` - Override OpenAI model (e.g., gpt-4, gpt-4o-mini)
- `--assume-defaults / --no-assume-defaults` - Skip clarifying questions by applying safe defaults (default: enabled)
- `--verbose, -v` - Print one-line step updates while running
- `--depth INTEGER` - Plan depth: number of iterative passes for complex tasks (1-25, default: 1)
- `--script-timeout INTEGER` - Timeout in seconds for generated scripts (1-3600, default: 120)

#### Interactive Mode
```bash
agent repl [--model TEXT]
```

**Notes:**
- `--dry-run` always skips execution and confirmation while showing planned work
- Risky commands (apt/pip/rm/system services) require confirmation unless `--auto-yes` or `--dry-run`
- Use `--depth` for complex multi-step tasks that require iterative planning
- Use `--verbose` to see detailed progress during execution

### Examples

#### Basic Tasks
- **Web scraping and summarization:**
```bash
agent do "https://docs.python.org/3/ sayfasını indir, ana değişiklikleri 10 maddede özetle" --auto-yes
```

- **Database analysis:**
```bash
agent do "postgresql+psycopg2://user:pass@host:5432/db'ye bağlanıp son 30 gün siparişlerini gün bazında say, CSV ve rapor üret"
```

- **Data processing:**
```bash
agent do "pandas ile basit bir DataFrame oluştur, csv'ye yaz ve raporla"
```

- **Package installation:**
```bash
agent do "git ve jq kur; sürümlerini yazdır; logs ve outputs'a kaydet"
```

#### Complex Multi-Step Tasks (using --depth)
- **Complex data pipeline:**
```bash
agent do "Create a complete data analysis pipeline: fetch data from multiple APIs, clean and merge datasets, perform statistical analysis, generate visualizations, and create a comprehensive report" --depth 3 --verbose
```

- **Full project setup:**
```bash
agent do "Set up a complete Python web application: create project structure, install dependencies, set up database, create API endpoints, add tests, and generate documentation" --depth 5 --script-timeout 300
```

- **System analysis and optimization:**
```bash
agent do "Analyze system performance, identify bottlenecks, implement optimizations, run benchmarks, and generate performance report" --depth 2 --verbose --auto-yes
```

#### Development and Testing
- **With verbose output:**
```bash
agent do "Create a REST API with FastAPI, add authentication, write tests" --verbose --depth 2
```

- **Dry run for planning:**
```bash
agent do "Migrate legacy database schema to new format" --dry-run --depth 3
```

### Project Structure
```
src/agent/
  cli.py            # Typer CLI with comprehensive options
  config.py         # Settings and folders configuration
  logger.py         # Rich logger setup
  llm.py            # OpenAI client with detailed logging
  orchestrator.py   # Plan -> execute -> verify -> report
  reporter.py       # Markdown report generation
  utils.py          # Utility functions
  tools/
    base.py         # Base tool interface
    shell.py        # Shell command execution
    python_exec.py  # Python script runner
    packages.py     # Package installation (apt/pip)
    db.py           # Database queries via SQLAlchemy
    web.py          # Web scraping and summarization
outputs/            # Generated artifacts (CSV, HTML, etc.)
reports/            # Markdown reports
logs/               # Application and LLM interaction logs
  agent.log         # General application logs
  llm_interactions.log  # Human-readable LLM conversations
  llm_interaction_*.json  # Structured LLM interaction data
workspace/tmp/      # Temporary files and scripts
```

### Logging and Debugging
The agent provides comprehensive logging:
- **General logs**: `logs/agent.log` - Application events and errors
- **LLM interactions**: `logs/llm_interactions.log` - Complete LLM conversations
- **Structured data**: `logs/llm_interaction_*.json` - JSON format for programmatic access
- **Tool outputs**: Individual stdout/stderr files for each command execution

Use `--verbose` to see real-time progress updates during execution.

### Safety & Scope
- All operations are scoped to the repository workspace. No writes outside.
- Risky operations (apt, pip install, rm -rf, service changes) require confirmation unless `--auto-yes`.
- Use `--dry-run` to preview plan/commands.

### Makefile
```bash
make install
make run
```

### License
MIT

### Quick Start (Windows PowerShell)
```powershell
# 1) Python 3.11 kurulu olmalı (Microsoft Store ya da python.org). Gerekirse:
#   winget install Python.Python.3.11

# 2) Depoyu klonlayın
git clone <this-repo>
cd <this-repo>

# 3) Sanal ortam
py -3.11 -m venv .venv
. .\.venv\Scripts\Activate.ps1

# 4) Kurulum
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

# 5) Ortam değişkenleri (.env)
Copy-Item env.example .env   # düzenleyip OPENAI_API_KEY değerini girin

# 6) Doğrulama
agent --help

# Örnek çalıştırma
agent do "https://docs.python.org/3/ sayfasını indir, ana değişiklikleri 10 maddede özetle" --auto-yes
```

Notlar (Windows):
- `apt-get` mevcut değildir; paket kurulum adımları Ubuntu içindir. Windows’ta `winget`/`choco` tercih edin.
- Ajanın shell paket kurulum aracı `apt` odaklıdır; Windows’ta bu kısım çalışmayabilir. Diğer araçlar (web, db, python_exec) Windows’ta çalışır.
- PowerShell’de sanal ortam aktivasyonu için `ExecutionPolicy` kısıtlaması varsa geçici olarak aşağıdaki komutla izin verebilirsiniz:
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

### Windows için make.bat (opsiyonel)
Windows’ta şu komutu kullanarak da kurabilirsiniz:
```powershell
./make.bat install
./make.bat run
```


