"""Command line entry point.

  resolve   detect which ATS backs each of the 100 companies
  discover  pull live postings, score them, fill the queue
  queue     show what's waiting
  prep      fill application forms (stops before Submit)
  review    look at filled forms and approve submissions
  status    counts + recent activity
  export    dump everything to CSV
  manual    links for portals with no usable API
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from .db import Tracker
from .discover import discover, manual_links, resolve_companies, save_jobs
from .models import STATUS_FILLED, STATUS_QUEUED

console = Console()
ROOT = Path(__file__).resolve().parent.parent


def load_cfg(path: str) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    return cfg


def load_profile(cfg: dict) -> dict:
    return json.loads(Path(cfg["sources"]["profile"]).read_text())


def _warn_todos(profile: dict) -> list[str]:
    """Find TODO_CONFIRM placeholders so they're flagged before a real submit."""
    todos: list[str] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and node.strip().upper().startswith("TODO_CONFIRM"):
            todos.append(path)

    walk(profile)
    return todos


# --- commands -------------------------------------------------------------

def cmd_resolve(args, cfg, profile):
    console.print("[bold]Detecting ATS for each company…[/bold]")
    companies = resolve_companies(cfg, refresh=args.refresh)

    by_ats: dict[str, list[str]] = {}
    for c in companies:
        by_ats.setdefault(c.ats, []).append(c.name)

    t = Table(title=f"{len(companies)} companies")
    t.add_column("ATS", style="cyan")
    t.add_column("Count", justify="right")
    t.add_column("Examples", style="dim", max_width=64)
    for ats, names in sorted(by_ats.items(), key=lambda kv: -len(kv[1])):
        t.add_row(ats, str(len(names)), ", ".join(sorted(names)[:5]))
    console.print(t)
    console.print(f"[dim]cached → {cfg['storage']['companies_json']}[/dim]")


def cmd_discover(args, cfg, profile):
    companies = resolve_companies(cfg, refresh=False)
    if args.company:
        companies = [c for c in companies if args.company.lower() in c.name.lower()]
        if not companies:
            console.print(f"[red]No company matching {args.company!r}[/red]")
            return

    console.print(f"[bold]Searching {len(companies)} companies…[/bold]")
    done = {"n": 0}

    def progress(entry):
        done["n"] += 1
        colour = "green" if entry["raw"] else "dim"
        console.print(f"[{colour}]{done['n']:3d}/{len(companies)} "
                      f"{entry['company'][:26]:28s} {entry['ats']:16s} {entry['note'][:52]}[/{colour}]")

    jobs, report = discover(cfg, profile, companies, progress=progress)
    save_jobs(jobs, cfg["storage"]["jobs_json"])

    with Tracker(cfg["storage"]["db"]) as tracker:
        new, updated = tracker.upsert_jobs(jobs)

    reached = sum(1 for r in report if r["raw"] > 0)
    console.print(f"\n[bold green]{len(jobs)} matching jobs[/bold green] "
                  f"({new} new, {updated} refreshed) from {reached}/{len(companies)} reachable portals")

    if jobs:
        t = Table(title="Top matches")
        t.add_column("Score", justify="right", style="bold")
        t.add_column("Company", style="cyan", max_width=20)
        t.add_column("Title", max_width=42)
        t.add_column("Location", style="dim", max_width=24)
        for j in jobs[:args.top]:
            t.add_row(f"{j.score:.2f}", j.company, j.title[:42], j.location[:24])
        console.print(t)


def cmd_queue(args, cfg, profile):
    with Tracker(cfg["storage"]["db"]) as tracker:
        rows = tracker.queue(limit=args.limit, status=args.status,
                             company=args.company, min_score=args.min_score)
        if not rows:
            console.print(f"[dim]Nothing with status={args.status!r}.[/dim]")
            return
        t = Table(title=f"{len(rows)} × {args.status}")
        t.add_column("Key", style="dim", max_width=16)
        t.add_column("Score", justify="right", style="bold")
        t.add_column("Company", style="cyan", max_width=18)
        t.add_column("Title", max_width=40)
        t.add_column("Location", style="dim", max_width=22)
        for r in rows:
            t.add_row(r["key"], f"{r['score']:.2f}", r["company"], r["title"][:40], (r["location"] or "")[:22])
        console.print(t)


def cmd_prep(args, cfg, profile):
    from .fill import Filler

    todos = _warn_todos(profile)
    if todos:
        console.print(f"[yellow]⚠ {len(todos)} unconfirmed profile field(s):[/yellow] "
                      f"{', '.join(todos[:8])}")
        console.print("[dim]These are left blank on forms rather than guessed. "
                      "Fill them in profile/profile.json when you can.[/dim]\n")

    with Tracker(cfg["storage"]["db"]) as tracker:
        rows = tracker.queue(limit=args.limit, status=STATUS_QUEUED,
                             company=args.company, min_score=args.min_score)
        if not rows:
            console.print("[dim]Queue empty — run `discover` first.[/dim]")
            return

        console.print(f"[bold]Preparing {len(rows)} application(s). "
                      f"Nothing will be submitted.[/bold]\n")

        with Filler(profile, cfg) as filler:
            for i, row in enumerate(rows, 1):
                console.print(f"[cyan]{i}/{len(rows)}[/cyan] {row['company']} — {row['title'][:52]}")
                result = filler.prepare(row["url"], row["key"])
                tracker.set_status(row["key"], result.status, result.note,
                                   filled_fields=result.fields, screenshot=result.screenshot)
                colour = {"filled": "green", "needs_login": "yellow"}.get(result.status, "red")
                console.print(f"   [{colour}]{result.status}[/{colour}] — {result.note}")

        console.print(f"\n[bold]Done.[/bold] Run [cyan]review[/cyan] to approve submissions.")


def cmd_review(args, cfg, profile):
    from .fill import Filler
    from .review import review_loop

    with Tracker(cfg["storage"]["db"]) as tracker:
        rows = tracker.queue(limit=args.limit, status=STATUS_FILLED,
                             company=args.company, min_score=args.min_score)
        if not rows:
            console.print("[dim]Nothing filled and waiting — run `prep` first.[/dim]")
            return
        with Filler(profile, cfg) as filler:
            tally = review_loop(tracker, filler, rows, dry_run=args.dry_run)
        console.print(f"\n[bold]{tally['submitted']} submitted, "
                      f"{tally['skipped']} skipped, {tally['failed']} failed[/bold]")


def cmd_status(args, cfg, profile):
    with Tracker(cfg["storage"]["db"]) as tracker:
        counts = tracker.counts()
        if not counts:
            console.print("[dim]No jobs tracked yet.[/dim]")
            return
        t = Table(title="Pipeline")
        t.add_column("Status", style="cyan")
        t.add_column("Count", justify="right", style="bold")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            t.add_row(k, str(v))
        console.print(t)

        recent = tracker.conn.execute(
            "SELECT e.ts, e.kind, j.company, j.title FROM events e "
            "JOIN jobs j ON j.key = e.job_key ORDER BY e.id DESC LIMIT 12"
        ).fetchall()
        if recent:
            r = Table(title="Recent activity", box=None)
            r.add_column("When", style="dim", max_width=20)
            r.add_column("Event", style="cyan", max_width=14)
            r.add_column("Job", max_width=52)
            for e in recent:
                r.add_row(e["ts"][:19], e["kind"], f"{e['company']} — {e['title'][:34]}")
            console.print(r)


def cmd_export(args, cfg, profile):
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with Tracker(cfg["storage"]["db"]) as tracker:
        rows = tracker.all_jobs()
        if not rows:
            console.print("[dim]Nothing to export.[/dim]")
            return
        cols = ["company", "title", "location", "ats", "score", "status", "status_note", "url", "discovered_at"]
        with out.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
    console.print(f"[green]Exported {len(rows)} rows → {out}[/green]")


def cmd_manual(args, cfg, profile):
    companies = resolve_companies(cfg, refresh=False)
    links = manual_links(companies, cfg)
    if not links:
        console.print("[green]Every portal has a working connector.[/green]")
        return
    out = Path("data/manual_portals.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "sector", "ats", "portal", "search_url"])
        w.writeheader()
        w.writerows(links)
    t = Table(title=f"{len(links)} portals needing a manual click-through")
    t.add_column("Company", style="cyan", max_width=22)
    t.add_column("Search URL", style="dim", max_width=66)
    for l in links[:args.limit]:
        t.add_row(l["company"], l["search_url"][:66])
    console.print(t)
    console.print(f"[dim]full list → {out}[/dim]")


# --- wiring ---------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog="jobapplier", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, limit=25):
        sp.add_argument("--company", help="filter to companies matching this substring")
        sp.add_argument("--limit", type=int, default=limit)
        sp.add_argument("--min-score", type=float, default=0.0, dest="min_score")

    s = sub.add_parser("resolve", help="detect the ATS behind each company")
    s.add_argument("--refresh", action="store_true", help="ignore the cache and re-detect")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("discover", help="pull live postings and score them")
    s.add_argument("--company")
    s.add_argument("--top", type=int, default=25)
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("queue", help="show tracked jobs by status")
    common(s)
    s.add_argument("--status", default=STATUS_QUEUED)
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("prep", help="fill forms and stop before Submit")
    common(s, limit=5)
    s.set_defaults(fn=cmd_prep)

    s = sub.add_parser("review", help="approve and submit filled applications")
    common(s, limit=10)
    s.add_argument("--dry-run", action="store_true", help="walk the flow without clicking Submit")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("status", help="pipeline counts and recent activity")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("export", help="dump all tracked jobs to CSV")
    s.add_argument("--out", default="data/applications.csv")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("manual", help="portals with no API - pre-filtered search links")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_manual)

    args = p.parse_args(argv)
    cfg = load_cfg(args.config)
    profile = load_profile(cfg)
    return args.fn(args, cfg, profile) or 0


if __name__ == "__main__":
    sys.exit(main())
