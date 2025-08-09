from __future__ import annotations

import sys
from typing import Optional

import typer

from .config import load_settings
from .logger import setup_logger
from .llm import LLMClient
from .orchestrator import ExecutionContext, Orchestrator


app = typer.Typer(add_completion=False, help="Natural language automation agent")


@app.command()
def do(
    goal: str = typer.Argument(..., help="Natural language goal in quotes"),
    auto_yes: bool = typer.Option(False, "--auto-yes", help="Auto-confirm risky actions"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show plan and commands without executing"),
    model: Optional[str] = typer.Option(None, "--model", help="Override OpenAI model"),
):
    try:
        settings = load_settings(auto_yes=auto_yes, dry_run=dry_run, model=model)
    except Exception as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    logger = setup_logger(settings.logs_dir)
    llm = LLMClient(settings.openai_api_key, settings.openai_model, settings.openai_base_url)
    ctx = ExecutionContext(settings=settings, llm=llm, logger=logger)
    orch = Orchestrator(ctx)

    ok, steps, artifacts, msg = orch.execute(goal)
    color = typer.colors.GREEN if ok else typer.colors.RED
    typer.secho(msg, fg=color)
    if artifacts:
        typer.echo("Artifacts:")
        for a in artifacts:
            typer.echo(f" - {a}")
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def repl(model: Optional[str] = typer.Option(None, "--model", help="Override OpenAI model")):
    try:
        settings = load_settings()
    except Exception as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)
    logger = setup_logger(settings.logs_dir)
    llm = LLMClient(settings.openai_api_key, settings.openai_model if model is None else model, settings.openai_base_url)
    ctx = ExecutionContext(settings=settings, llm=llm, logger=logger)
    orch = Orchestrator(ctx)

    typer.echo("Interactive REPL. Type your goal. Empty line to exit.")
    while True:
        try:
            goal = typer.prompt("Goal")
        except typer.Abort:
            break
        if not goal.strip():
            break
        ok, steps, artifacts, msg = orch.execute(goal)
        color = typer.colors.GREEN if ok else typer.colors.RED
        typer.secho(msg, fg=color)
        if artifacts:
            typer.echo("Artifacts:")
            for a in artifacts:
                typer.echo(f" - {a}")


def main():
    app()


