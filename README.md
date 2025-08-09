## Natural Language Automation Agent

An automation agent that executes tasks described in natural language. It plans, asks for missing critical details, confirms risky actions (unless --auto-yes), executes with tools (shell, Python, web, DB), validates, and produces Markdown reports.

### Features
- Natural-language CLI: `agent do "..."` and `agent repl`
- Safety: asks up to 3 focused questions for missing critical parameters, confirmation for risky ops unless `--auto-yes`
- Tools: safe shell, Python script runner, package installation, web fetch + summarize, DB query via SQLAlchemy
- Logs: rich console + rotating file logs, stdout/stderr/exit codes recorded
- Reports: Markdown in `reports/`, artifacts in `outputs/`

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
```bash
agent do "<goal>" [--auto-yes] [--dry-run] [--model ...]
agent repl [--model ...]
```

Notes:
- `--dry-run` always skips execution and confirmation while showing planned work.
- Risky commands (apt/pip/rm/system services) require confirmation unless `--auto-yes` or `--dry-run`.

### Examples
- Web:
```bash
agent do "https://docs.python.org/3/ sayfasını indir, ana değişiklikleri 10 maddede özetle" --auto-yes
```
- DB:
```bash
agent do "postgresql+psycopg2://user:pass@host:5432/db'ye bağlanıp son 30 gün siparişlerini gün bazında say, CSV ve rapor üret"
```
- Script:
```bash
agent do "pandas ile basit bir DataFrame oluştur, csv’ye yaz ve raporla"
```
- Shell:
```bash
agent do "git ve jq kur; sürümlerini yazdır; logs ve outputs’a kaydet"
```

### Project Structure
```
src/agent/
  cli.py            # Typer CLI
  config.py         # settings and folders
  logger.py         # rich logger setup
  llm.py            # OpenAI client helpers
  orchestrator.py   # plan -> execute -> verify -> report
  reporter.py       # markdown report generation
  utils.py
  tools/
    base.py
    shell.py
    python_exec.py
    packages.py
    db.py
    web.py
outputs/
reports/
logs/
workspace/tmp/
```

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


