"""output.py - console logging for bot.py (Rich), with section and step lines."""

import logging

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

console = Console(theme=Theme({
    "logging.level.info":    "cyan",
    "logging.level.warning": "yellow bold",
    "logging.level.error":   "red bold",
}))


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(
            console=console,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            show_path=False,
            markup=True,
        )],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def banner() -> None:
    console.print("\n[bold cyan]╔══════════════════════════════════════╗[/]")
    console.print("[bold cyan]║[/]  [bold white]Tornknackarna Scouting Bot[/]          [bold cyan]║[/]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/]\n")


def section(label: str) -> None:
    pad = max(0, 38 - len(label))
    console.print(f"\n[bold white]── {label} [/][dim]{'─' * pad}[/]")


def step(icon: str, msg: str) -> None:
    console.print(f"   {icon} {msg}")
