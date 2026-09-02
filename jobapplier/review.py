"""Review and submit.

This is the only module in the project that clicks a Submit button, and it
only does so after you explicitly approve a job whose filled fields you've
seen. Nothing here runs unattended.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .db import Tracker
from .fill import Filler
from .models import STATUS_FAILED, STATUS_SKIPPED, STATUS_SUBMITTED

console = Console()


def show_filled(row) -> None:
    """Print everything that was put into the form, plus what was left blank."""
    fields = json.loads(row["filled_fields"] or "[]")

    console.print(Panel(
        f"[bold]{row['title']}[/bold]\n"
        f"{row['company']}  ·  {row['location'] or 'location n/a'}  ·  score {row['score']:.2f}\n"
        f"[dim]{row['url']}[/dim]",
        title=f"[cyan]{row['key']}[/cyan]", border_style="cyan",
    ))

    if fields:
        t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        t.add_column("Field", style="white", max_width=42)
        t.add_column("Value", style="green", max_width=46)
        t.add_column("From", style="dim", max_width=22)
        for f in fields:
            value = f["value"]
            # Work-auth and EEO answers are the ones worth a second look.
            style = "bold yellow" if f["source"].startswith(("work_auth", "eeo")) else "green"
            t.add_row(f["label"][:42], f"[{style}]{value[:46]}[/{style}]", f["source"])
        console.print(t)

    note = row["status_note"] or ""
    m = re.search(r"(\d+) unanswered", note)
    if m and int(m.group(1)):
        console.print(f"[yellow]⚠ {m.group(1)} field(s) left blank[/yellow] — "
                      f"check the browser before submitting.")
    if row["screenshot"]:
        console.print(f"[dim]screenshot: {row['screenshot']}[/dim]")


def submit_job(filler: Filler, row, tracker: Tracker, dry_run: bool = False) -> bool:
    """Reopen the parked form and click Submit. Only called after approval."""
    page = filler.ctx.new_page()
    try:
        page.goto(row["url"], wait_until="domcontentloaded", timeout=45000)
        filler._open_form(page)
        scope, count = filler._form_frame(page)
        if count == 0:
            page.wait_for_timeout(2500)
            scope, count = filler._form_frame(page)
        filled, unanswered = filler.fill_form(page, scope)

        if not filled:
            tracker.set_status(row["key"], STATUS_FAILED, "re-fill produced no fields on submit pass")
            return False

        el, label = filler._submit_locator(page, scope)
        if el is None:
            tracker.set_status(row["key"], STATUS_FAILED, "no submit button found")
            return False

        if dry_run:
            console.print(f"[yellow]DRY RUN[/yellow] would click: {label!r}")
            tracker.set_status(row["key"], "filled", "dry run - not submitted")
            return False

        el.click(timeout=15000)
        page.wait_for_load_state("networkidle", timeout=30000)
        shot = str(Path(filler.cfg["storage"]["screenshots"]) / f"{row['key']}_submitted.png")
        page.screenshot(path=shot, full_page=True)
        tracker.set_status(row["key"], STATUS_SUBMITTED, f"submitted via {label!r}", screenshot=shot)
        return True
    except Exception as exc:
        tracker.set_status(row["key"], STATUS_FAILED, f"{type(exc).__name__}: {exc}")
        return False
    finally:
        page.close()


def review_loop(tracker: Tracker, filler: Filler, rows, dry_run: bool = False) -> dict[str, int]:
    """Walk the filled queue one job at a time, asking before each submit."""
    tally = {"submitted": 0, "skipped": 0, "failed": 0}

    for row in rows:
        console.rule(f"[bold]{row['company']} — {row['title']}")
        show_filled(row)

        choice = console.input(
            "\n[bold]s[/bold]ubmit / [bold]k[/bold]skip / [bold]o[/bold]pen in browser / "
            "[bold]q[/bold]uit  > "
        ).strip().lower()

        if choice in ("q", "quit"):
            break
        if choice in ("o", "open"):
            page = filler.ctx.new_page()
            page.goto(row["url"])
            console.print("[dim]Opened. Fill anything missing by hand, then re-run review.[/dim]")
            console.input("Press Enter when done looking...")
            choice = console.input("[bold]s[/bold]ubmit now / [bold]k[/bold]skip > ").strip().lower()

        if choice in ("s", "submit", "y", "yes"):
            ok = submit_job(filler, row, tracker, dry_run=dry_run)
            if ok:
                tally["submitted"] += 1
                console.print("[green]✓ submitted[/green]")
            else:
                tally["failed"] += 1
                console.print("[red]✗ submit failed — see status note[/red]")
        else:
            tracker.set_status(row["key"], STATUS_SKIPPED, "skipped at review")
            tally["skipped"] += 1
            console.print("[dim]skipped[/dim]")

    return tally
