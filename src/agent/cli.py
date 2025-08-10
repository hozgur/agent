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
    auto_yes: bool = typer.Option(True, "--auto-yes/--no-auto-yes", help="Auto-confirm risky actions (default: enabled)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show plan and commands without executing"),
    model: Optional[str] = typer.Option(None, "--model", help="Override OpenAI model"),
    assume_defaults: bool = typer.Option(True, "--assume-defaults/--no-assume-defaults", help="Skip clarifying questions by applying safe defaults"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print one-line step updates while running"),
):
    try:
        settings = load_settings(auto_yes=auto_yes, dry_run=dry_run, model=model, assume_defaults=assume_defaults, verbose=verbose)
    except Exception as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    logger = setup_logger(settings.logs_dir)
    llm = LLMClient(settings.openai_api_key, settings.openai_model, settings.openai_base_url)
    ctx = ExecutionContext(settings=settings, llm=llm, logger=logger)
    orch = Orchestrator(ctx)

    # Show current model
    typer.echo(f"Model: {settings.openai_model}")

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
    active_model = settings.openai_model if model is None else model
    llm = LLMClient(settings.openai_api_key, active_model, settings.openai_base_url)
    ctx = ExecutionContext(settings=settings, llm=llm, logger=logger)
    orch = Orchestrator(ctx)

    typer.echo(f"Interactive REPL (Model: {active_model}). Type your goal. Empty line to exit.")
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


if __name__ == "__main__":
    main()